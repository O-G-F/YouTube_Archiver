"""Phase 9B: production deployment readiness checks.

``production_check`` consolidates preflight + disk guard + persistence +
data-integrity signals into a single PASS/WARN/FAIL report to run BEFORE a
production cutover and AFTER a restore/migration. ``archive_media_check``
verifies that DB ``media_files`` still resolve to real files after an archive
root switch (NAS/external) — the migration guard.

Both return counts + booleans only — NEVER secret values, cookies, raw_json, or
host absolute paths (missing videos are reported by public youtube_video_id).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.logging_setup import get_logger
from app.models import MediaFile, Video

logger = get_logger(__name__)

PASS, WARN, FAIL = "pass", "warn", "fail"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}


def _c(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _overall(checks: list[dict]) -> str:
    o = PASS
    for c in checks:
        if _RANK[c["status"]] > _RANK[o]:
            o = c["status"]
    return o


def _counts(checks: list[dict]) -> dict:
    return {s: sum(1 for c in checks if c["status"] == s) for s in (PASS, WARN, FAIL)}


def _redis_aof_enabled(settings: Settings) -> bool | None:
    """True/False if Redis AOF persistence is on; None if Redis is unreadable."""
    try:
        from app.worker.queue import get_redis

        r = get_redis()
        val = r.config_get("appendonly") or {}
        return str(val.get("appendonly", "no")).lower() == "yes"
    except Exception as exc:  # noqa: BLE001 - Redis down / config unavailable
        logger.warning("production_check: redis aof unreadable: %s", exc)
        return None


def _profile_writes_comments(session: Session, profile_name: str) -> bool:
    try:
        from app.services.profiles import get_profile_spec

        return bool(get_profile_spec(session, profile_name).resolved_flags().get("write_comments"))
    except Exception:  # noqa: BLE001 - unknown profile => treat as not comments
        return False


def _auth_checks(settings: Settings) -> list[dict]:
    """Phase 9C access-control checks (no secret values / paths — booleans only)."""
    prod = settings.is_production
    mode = (settings.auth_mode or "disabled").strip().lower()
    fw = FAIL if prod else WARN  # fail in production, warn in development
    out: list[dict] = [_c("app_env", PASS, f"app_env={settings.app_env}")]

    if mode == "disabled":
        out.append(_c("auth_mode", FAIL if prod else PASS,
                      "auth DISABLED in production — set AUTH_MODE=local/trusted_proxy" if prod
                      else "auth disabled (OK for development; required for production)"))
    else:
        out.append(_c("auth_mode", PASS, f"auth_mode={mode}"))

    if mode == "local":
        out.append(_c("session_secret_readable",
                      PASS if settings.session_secret_configured else fw,
                      "session secret readable" if settings.session_secret_configured
                      else "SESSION_SECRET_FILE missing/unreadable (local mode)"))
        out.append(_c("local_password_hash_readable",
                      PASS if settings.admin_password_hash_configured else fw,
                      "admin password hash readable" if settings.admin_password_hash_configured
                      else "ADMIN_PASSWORD_HASH_FILE missing/unreadable (local mode)"))
        secure = settings.effective_session_cookie_secure
        out.append(_c("secure_cookie", PASS if (secure or not prod) else FAIL,
                      "session cookie Secure" if secure
                      else ("session cookie NOT Secure in production (requires HTTPS)" if prod
                            else "session cookie not Secure (dev/HTTP only)")))
    else:
        out.append(_c("session_secret_readable", PASS, "n/a (not local mode)"))
        out.append(_c("local_password_hash_readable", PASS, "n/a (not local mode)"))
        out.append(_c("secure_cookie", PASS, "n/a (not local mode)"))

    if mode == "trusted_proxy":
        if not settings.trusted_proxy_cidr_list:
            out.append(_c("trusted_proxy_config", FAIL,
                          "trusted_proxy without TRUSTED_PROXY_CIDRS — the auth header could be spoofed"))
        elif not settings.allowed_admin_email_list:
            out.append(_c("trusted_proxy_config", FAIL,
                          "trusted_proxy without ALLOWED_ADMIN_EMAILS (fail-closed => no one can log in)"))
        else:
            out.append(_c("trusted_proxy_config", PASS,
                          f"{len(settings.trusted_proxy_cidr_list)} trusted CIDR(s), "
                          f"{len(settings.allowed_admin_email_list)} allowed admin(s)"))
    else:
        out.append(_c("trusted_proxy_config", PASS, "n/a (not trusted_proxy mode)"))

    if settings.cors_is_wildcard:
        out.append(_c("cors_policy", FAIL if prod else WARN,
                      "CORS_ALLOW_ORIGINS=* not allowed in production" if prod
                      else "CORS_ALLOW_ORIGINS=* (OK for local dev; restrict for production)"))
    else:
        out.append(_c("cors_policy", PASS, "CORS restricted to explicit origins"))

    # Phase 9D: ingress boundary
    hosts = settings.allowed_hosts_list
    if any(h == "*" for h in hosts):
        out.append(_c("allowed_hosts", FAIL, "ALLOWED_HOSTS must not contain '*'"))
    elif hosts:
        out.append(_c("allowed_hosts", PASS, f"{len(hosts)} allowed host(s)"))
    else:
        out.append(_c("allowed_hosts", FAIL if prod else PASS,
                      "ALLOWED_HOSTS empty in production (Host header not validated)" if prod
                      else "ALLOWED_HOSTS empty (any host; OK for dev)"))

    cto = settings.csrf_trusted_origins_list
    if any(o == "*" for o in cto):
        out.append(_c("csrf_trusted_origins", FAIL, "CSRF_TRUSTED_ORIGINS must not contain '*'"))
    elif mode == "local":
        out.append(_c("csrf_trusted_origins",
                      PASS if cto else (FAIL if prod else PASS),
                      f"{len(cto)} trusted origin(s)" if cto
                      else ("CSRF_TRUSTED_ORIGINS empty in production (local cookie auth)" if prod
                            else "CSRF_TRUSTED_ORIGINS empty (same-origin only; OK for dev)")))
    else:
        out.append(_c("csrf_trusted_origins", PASS, "n/a (not local cookie auth)"))

    try:
        from app.services import auth as auth_svc
        rl = auth_svc.rate_limit_backend_status(settings)
        if prod and rl["effective"] != "redis":
            out.append(_c("rate_limit_backend", WARN,
                          f"login rate limit backend '{rl['effective']}' (Redis recommended in production)"))
        else:
            out.append(_c("rate_limit_backend", PASS, f"login rate limit backend: {rl['effective']}"))
    except Exception:  # noqa: BLE001
        out.append(_c("rate_limit_backend", WARN, "rate limit backend unknown"))

    if mode == "disabled":
        out.append(_c("mutating_api_protection", FAIL if prod else PASS,
                      "mutating API UNPROTECTED in production" if prod
                      else "mutating API open (dev; enable auth for production)"))
    else:
        out.append(_c("mutating_api_protection", PASS, "enforced by AuthMiddleware for /api/*"))

    # HTTPS readiness — judged from PUBLIC_BASE_URL + proxy, NOT the HSTS header.
    pub = (settings.public_base_url or "").strip()
    if not prod:
        out.append(_c("https_readiness", PASS, "n/a (development)"))
    elif not pub:
        out.append(_c("https_readiness", WARN, "PUBLIC_BASE_URL not set — external HTTPS not confirmed"))
    elif settings.public_base_url_is_https:
        out.append(_c("https_readiness", PASS, "public base URL is https"))
    else:
        out.append(_c("https_readiness", FAIL, "PUBLIC_BASE_URL is not https in production"))

    # scrypt hash carries cost params -> a future upgrade is detectable/rehashable.
    if mode == "local" and settings.admin_password_hash_configured:
        try:
            from app.services import auth as _auth
            stale = _auth.needs_rehash(settings.admin_password_hash())
        except Exception:  # noqa: BLE001
            stale = False
        out.append(_c("password_hash_algorithm", WARN if stale else PASS,
                      "admin password hash uses non-current scrypt params — regenerate with `auth hash-password`"
                      if stale else "password hash uses current scrypt params"))
    else:
        out.append(_c("password_hash_algorithm", PASS, "n/a (not local mode / no hash)"))

    return out


def _observability_checks(session: Session, settings: Settings) -> list[dict]:
    """Phase 9E/9E.1 audit/observability judgments (no secret values / paths)."""
    prod = settings.is_production
    out: list[dict] = []

    out.append(_c("audit_enabled", PASS if settings.audit_enabled else (FAIL if prod else WARN),
                  "audit trail enabled" if settings.audit_enabled
                  else ("audit trail DISABLED in production" if prod else "audit disabled (dev)")))

    cur_key_id, cur_key = settings.audit_current_signing()
    out.append(_c("audit_signature_scheme", PASS, "hmac_sha256" if cur_key else "sha256_unsigned"))

    if not settings.audit_enabled:
        out.append(_c("audit_current_key_configured", PASS, "n/a (audit disabled)"))
    else:
        out.append(_c("audit_current_key_configured", PASS if cur_key else (FAIL if prod else PASS),
                      "current signing key configured" if cur_key
                      else ("no audit signing key in production" if prod else "unsigned SHA-256 chain (dev)")))

    if cur_key:
        has_id = bool((settings.audit_hmac_key_id or "").strip())
        out.append(_c("audit_current_key_id_configured", PASS if has_id else (FAIL if prod else WARN),
                      "AUDIT_HMAC_KEY_ID set" if has_id else "AUDIT_HMAC_KEY_ID not set"))
    else:
        out.append(_c("audit_current_key_id_configured", PASS, "n/a (unsigned)"))

    err = settings.audit_key_config_error()
    out.append(_c("audit_key_config", FAIL if err else PASS, err or "key registry consistent"))
    out.append(_c("audit_previous_keys_available", PASS,
                  f"{len(settings.audit_previous_keys())} previous verification key(s)"))

    if prod:
        out.append(_c("audit_pseudonym_key", PASS if settings.audit_pseudonym_key_configured else WARN,
                      "pseudonym key configured" if settings.audit_pseudonym_key_configured
                      else "AUDIT_PSEUDONYM_KEY_FILE not set (rotation would change pseudonyms)"))
    else:
        out.append(_c("audit_pseudonym_key", PASS, "n/a (dev)"))

    v = None
    try:
        from app.services import audit
        v = audit.verify_chain(session, settings)
    except Exception:  # noqa: BLE001
        pass
    if v is None:
        for n in ("audit_chain_valid", "audit_signing_boundary_valid", "audit_unsigned_event_count",
                  "audit_missing_verification_keys", "audit_unexpected_regime_changes"):
            out.append(_c(n, WARN, "audit chain verification unavailable"))
    else:
        out.append(_c("audit_chain_valid", PASS if v["valid"] else FAIL,
                      f"chain valid ({v['checked_count']} events, {v['segment_count']} segment(s))" if v["valid"]
                      else f"chain INVALID: {v['failure_reason_code']} at event {v['first_invalid_event_id']}"))
        boundary_ok = v["failure_reason_code"] not in (
            "unexpected_regime_change", "boundary_regime_mismatch", "checkpoint_boundary_mismatch", "tampered_checkpoint")
        out.append(_c("audit_signing_boundary_valid", PASS if boundary_ok else FAIL,
                      "signing boundaries consistent" if boundary_ok else f"boundary issue: {v['failure_reason_code']}"))
        out.append(_c("audit_unexpected_regime_changes",
                      FAIL if v["failure_reason_code"] == "unexpected_regime_change" else PASS,
                      "unvouched signing regime change" if v["failure_reason_code"] == "unexpected_regime_change" else "none"))
        miss = v["missing_verification_keys"]
        out.append(_c("audit_missing_verification_keys", FAIL if miss else PASS,
                      f"missing keys for {miss}" if miss else "all referenced keys available"))
        uc = v["unsigned_event_count"]
        if uc == 0:
            out.append(_c("audit_unsigned_event_count", PASS, "0 unsigned events"))
        elif not prod:
            out.append(_c("audit_unsigned_event_count", WARN, f"{uc} unsigned event(s) (dev)"))
        elif settings.audit_allow_legacy_unsigned_prefix:
            out.append(_c("audit_unsigned_event_count", WARN, f"{uc} legacy unsigned event(s) allowed by policy"))
        else:
            out.append(_c("audit_unsigned_event_count", FAIL,
                          f"{uc} unsigned event(s) in production (AUDIT_ALLOW_LEGACY_UNSIGNED_PREFIX=false)"))

    if prod and (settings.audit_retention_days < 30
                 or settings.audit_security_retention_days < settings.audit_retention_days):
        out.append(_c("audit_retention", WARN,
                      f"retention {settings.audit_retention_days}d / security "
                      f"{settings.audit_security_retention_days}d — review"))
    else:
        out.append(_c("audit_retention", PASS,
                      f"retention {settings.audit_retention_days}d / security {settings.audit_security_retention_days}d"))

    out.append(_c("metrics_protected", FAIL if (prod and not settings.metrics_require_auth) else PASS,
                  "metrics endpoint not auth-protected in production" if (prod and not settings.metrics_require_auth)
                  else "metrics require auth (or non-production)"))
    out.append(_c("structured_logging", PASS,
                  "structured JSON logging on" if settings.structured_logging else "plain logging (structured off)"))
    out.append(_c("readiness_endpoint", PASS, "/health/live + /health/ready available"))
    return out


def production_check(session: Session, settings: Settings | None = None) -> dict:
    """Aggregate production-readiness signals into a PASS/WARN/FAIL report."""
    settings = settings or get_settings()
    from app.services import db_stats as dbs
    from app.services import preflight as pf
    from app.services import queue_health, reconcile, storage

    checks: list[dict] = []

    # ---- reuse preflight for infra checks (build match / schema / cookies / secrets) ----
    _map = {"ok": PASS, "warn": WARN, "fail": FAIL}
    try:
        pre = pf.system_preflight(session, settings)
        pre_by = {c["name"]: c for c in pre["checks"]}
        for key in ("db_connect", "redis_connect", "schema_head", "worker_build_match",
                    "worker_build_id", "cookies_file", "secret_value_exposed"):
            c = pre_by.get(key)
            if c:
                checks.append(_c(key, _map.get(c["status"], WARN), c["detail"]))
        checks.append(_c("preflight_overall", PASS if pre["ok"] else FAIL,
                         "no preflight failures" if pre["ok"] else "preflight has FAIL checks (see above)"))
    except Exception as exc:  # noqa: BLE001 - never crash the readiness report
        checks.append(_c("preflight_overall", FAIL, f"preflight error: {type(exc).__name__}"))

    # ---- archive disk free vs min-free (computed directly so it's testable) ----
    disk = storage.disk_usage(settings)
    min_free = settings.archive_min_free_gb
    if not disk.get("readable"):
        checks.append(_c("archive_disk_free", WARN, "archive volume free space unreadable"))
    elif disk.get("free_gb") is not None and disk["free_gb"] < min_free:
        checks.append(_c("archive_disk_free", FAIL,
                         f"free {disk['free_gb']} GiB below min-free {min_free} GiB — body archive is blocked"))
    else:
        checks.append(_c("archive_disk_free", PASS, f"free {disk.get('free_gb')} GiB (min-free {min_free} GiB)"))

    # ---- Redis AOF persistence (queue survives restart / host sleep) ----
    aof = _redis_aof_enabled(settings)
    if aof is None:
        checks.append(_c("redis_aof_persistence", WARN, "cannot read Redis persistence config (Redis unreachable?)"))
    elif aof:
        checks.append(_c("redis_aof_persistence", PASS, "Redis appendonly=yes (queue survives restart)"))
    else:
        checks.append(_c("redis_aof_persistence", FAIL,
                         "Redis appendonly=no — queued/running jobs would be LOST on restart; enable AOF"))

    # ---- orphan dry-run ----
    try:
        orph = reconcile.reconcile_orphans(session, settings, apply=False)
        if orph.get("rq_unreadable"):
            checks.append(_c("orphan_jobs", WARN, "RQ unreadable — cannot assess orphans"))
        elif orph.get("orphan_found", 0) > 0:
            checks.append(_c("orphan_jobs", FAIL,
                             f"{orph['orphan_found']} orphan download job(s) — run `jobs reconcile-orphans --apply`"))
        else:
            checks.append(_c("orphan_jobs", PASS, "no orphaned download jobs"))
    except Exception:  # noqa: BLE001
        checks.append(_c("orphan_jobs", WARN, "orphan check unavailable"))

    # ---- duplicate video media ----
    dups = reconcile.duplicate_video_media(session)
    checks.append(_c("duplicate_video_media", PASS if not dups else FAIL,
                     "no duplicate video media" if not dups
                     else f"{len(dups)} video(s) with duplicate 'video' media — investigate before cutover"))

    # ---- raw_json stored total (privacy/size invariant) ----
    stats = dbs.db_stats(session)
    rj = int(stats.get("raw_json_stored_total") or 0)
    checks.append(_c("raw_json_stored", PASS if rj == 0 else FAIL,
                     "raw_json stored total = 0" if rj == 0
                     else f"{rj} row(s) store raw_json — clear before production (privacy/size)"))

    # ---- default body profile (comments-light expected) ----
    prof = settings.effective_body_archive_profile
    if _profile_writes_comments(session, prof):
        checks.append(_c("default_body_profile", WARN,
                         f"default body profile '{prof}' writes comments — DB bloats at scale; use a comments-light profile"))
    else:
        checks.append(_c("default_body_profile", PASS, f"default body profile '{prof}' is comments-light"))

    # ---- comments table size (informational) ----
    comments_bytes = int((stats.get("table_sizes_bytes") or {}).get("comments") or 0)
    checks.append(_c("comments_table_size", PASS, f"comments table {comments_bytes} bytes"))

    # ---- active jobs idle (safer cutover) ----
    q = queue_health.queue_status(session)
    active = int(q.get("total_active") or 0)
    checks.append(_c("active_jobs_idle", PASS if active == 0 else WARN,
                     "no active jobs" if active == 0 else f"{active} active job(s) — let them finish before a cutover"))

    # ---- queue health (a worker is running) ----
    wc = q.get("worker_count")
    if wc is None:
        checks.append(_c("queue_health", WARN, "worker count unknown (Redis/RQ unreadable)"))
    elif wc < 1:
        checks.append(_c("queue_health", FAIL, "no RQ worker running"))
    else:
        checks.append(_c("queue_health", PASS,
                         f"{wc} worker(s), queued={q.get('queued', 0)} running={q.get('running', 0)}"))

    # ---- required env present (server DB) ----
    if (settings.database_url or "").startswith("sqlite"):
        checks.append(_c("required_env", WARN, "DATABASE_URL is sqlite (dev DB) — use PostgreSQL in production"))
    else:
        checks.append(_c("required_env", PASS, "DATABASE_URL is a server database"))

    # ---- dangerous / dev-only settings (CORS handled in the auth section) ----
    dangers: list[str] = []
    if settings.archive_min_free_gb <= 0:
        dangers.append("ARCHIVE_MIN_FREE_GB<=0 (disk guard disabled)")
    if (settings.log_level or "").upper() == "DEBUG":
        dangers.append("LOG_LEVEL=DEBUG (verbose exposure)")
    checks.append(_c("dev_only_settings", WARN if dangers else PASS,
                     ("; ".join(dangers) + " — review for production") if dangers
                     else "no dev-only/dangerous settings detected"))

    # ---- Phase 9C: access control / auth / CORS / proxy ----
    checks.extend(_auth_checks(settings))
    # ---- Phase 9E: audit / observability ----
    checks.extend(_observability_checks(session, settings))

    return {
        "overall": _overall(checks),
        "counts": _counts(checks),
        "checks": checks,
        "default_body_profile": prof,
        "app_env": settings.app_env,
        "auth_mode": (settings.auth_mode or "disabled"),
        "disk_min_free_gb": settings.archive_min_free_gb,
        "backup_reminder": (
            "Back up before cutover: PostgreSQL (pg_dump), the Redis AOF volume (redisdata), "
            "the archive directory, and cookies/secrets — see the backup/restore runbook in README."
        ),
    }


def archive_media_check(
    session: Session, settings: Settings | None = None, *, limit: int | None = None
) -> dict:
    """Verify DB video ``media_files`` resolve to real files (archive-root guard).

    Run before/after switching ARCHIVE_HOST_PATH (NAS/external): counts DB video
    media, how many resolve to an existing file, and how many are missing. Missing
    entries are reported by public ``youtube_video_id`` — NEVER by file path.
    """
    settings = settings or get_settings()
    from app.services import reconcile, storage

    rows = session.execute(
        select(MediaFile.id, MediaFile.path, Video.youtube_video_id)
        .join(Video, Video.id == MediaFile.video_id)
        .where(MediaFile.media_type == "video")
        .order_by(MediaFile.id.desc())
    ).all()
    total = len(rows)
    scan = rows[: max(0, limit)] if limit else rows

    checked = existing = missing = 0
    missing_ids: list[str] = []
    for _mid, path, yid in scan:
        checked += 1
        try:
            present = storage.to_absolute(settings, path).is_file()
        except OSError:
            present = False
        if present:
            existing += 1
        else:
            missing += 1
            if len(missing_ids) < 50 and yid:
                missing_ids.append(yid)

    dups = reconcile.duplicate_video_media(session)
    disk = storage.disk_usage(settings)
    return {
        "db_video_media_files": total,
        "checked": checked,
        "existing": existing,
        "missing": missing,
        # public youtube ids only — no host/relative file paths are returned
        "missing_youtube_ids": missing_ids,
        "duplicate_video_media_files": len(dups),
        "disk": {
            "readable": bool(disk.get("readable")),
            "free_gb": disk.get("free_gb"),
            "total_gb": disk.get("total_gb"),
            "used_gb": disk.get("used_gb"),
        },
        "ok": missing == 0 and len(dups) == 0,
    }


def _compose_published_ports(service: dict) -> list[tuple[str, str]]:
    """Return [(published_port, host_ip)] for a compose service's port mappings.
    host_ip "" / 0.0.0.0 / :: means bound to all interfaces (externally reachable)."""
    out: list[tuple[str, str]] = []
    for p in (service or {}).get("ports") or []:
        if isinstance(p, dict):
            published = p.get("published")
            if published:
                out.append((str(published), (p.get("host_ip") or "").strip()))
        elif isinstance(p, str):
            parts = p.split(":")
            if len(parts) == 3:      # host_ip:published:target
                out.append((parts[1], parts[0]))
            elif len(parts) == 2:    # published:target
                out.append((parts[0], ""))
            # single "target" => container-only (not published)
    return out


def compose_static_check(config: dict, *, production: bool = False) -> dict:
    """Statically inspect a `docker compose config` dump (JSON/YAML) for unsafe
    host port publishing (the app can't see host binds from inside the container).
    web on 0.0.0.0 / a published datastore => WARN (dev) or FAIL (production)."""
    services = (config or {}).get("services") or {}
    fw = FAIL if production else WARN
    checks: list[dict] = []

    web_pubs = _compose_published_ports(services.get("web") or {})
    if not web_pubs:
        checks.append(_c("compose_web_bind", PASS, "web has no host port publish (behind reverse proxy/tunnel)"))
    else:
        exposed = [(port, ip) for port, ip in web_pubs if ip in ("", "0.0.0.0", "::")]
        if exposed:
            checks.append(_c("compose_web_bind", fw,
                             "web published on " + ", ".join(f"{ip or '0.0.0.0'}:{port}" for port, ip in exposed)
                             + " — bind to 127.0.0.1 or drop the host publish (use a reverse proxy)"))
        else:
            checks.append(_c("compose_web_bind", PASS,
                             "web bound to loopback: " + ", ".join(f"{ip}:{port}" for port, ip in web_pubs)))

    for svc in ("postgres", "redis"):
        pubs = _compose_published_ports(services.get(svc) or {})
        checks.append(_c(f"compose_{svc}_publish", fw if pubs else PASS,
                         f"{svc} publishes host port(s) {', '.join(p for p, _ in pubs)} — do not expose the datastore"
                         if pubs else f"{svc} not host-published"))

    for svc in ("worker", "scheduler"):
        pubs = _compose_published_ports(services.get(svc) or {})
        checks.append(_c(f"compose_{svc}_publish", WARN if pubs else PASS,
                         f"{svc} publishes a host port (unexpected)" if pubs else f"{svc} not host-published"))

    return {"overall": _overall(checks), "counts": _counts(checks), "checks": checks}


def _backup_freshness_check(settings: Settings) -> dict:
    import time as _t
    from pathlib import Path

    marker = (settings.backup_marker_file or "").strip()
    if not marker:
        return _c("backup_freshness", WARN, "BACKUP_MARKER_FILE not set — backup freshness not verified")
    try:
        p = Path(marker)
        if not p.is_file():
            return _c("backup_freshness", WARN, "backup marker missing (no recorded backup)")
        age_h = (_t.time() - p.stat().st_mtime) / 3600.0
        if age_h > settings.backup_max_age_hours:
            return _c("backup_freshness", WARN,
                      f"last backup ~{int(age_h)}h ago (older than {settings.backup_max_age_hours}h)")
        return _c("backup_freshness", PASS, f"last backup ~{int(age_h)}h ago")
    except OSError:
        return _c("backup_freshness", WARN, "backup marker unreadable")


def _backup_integrity_checks(settings: Settings) -> list[dict]:
    """Phase 9F: backup manifest presence / verification recency / restore
    rehearsal recency. Markers + a small summary JSON are written by the backup
    scripts and CLI; here we only READ them. WARN-level (deploys are not blocked
    by an un-run rehearsal), basenames/ages only — no paths."""
    from app.services import backup_manifest as bm

    out: list[dict] = []

    if not (settings.backup_manifest_summary_file or "").strip():
        out.append(_c("backup_manifest", WARN,
                      "BACKUP_MANIFEST_SUMMARY_FILE not set — backup manifest not tracked"))
    else:
        summary = bm.read_backup_manifest_summary(settings)
        if summary is None:
            out.append(_c("backup_manifest", WARN,
                          "no backup manifest summary recorded (run scripts/backup.sh)"))
        elif not summary.get("sha256") or not summary.get("artifact"):
            out.append(_c("backup_manifest", WARN, "backup manifest summary incomplete"))
        else:
            head = summary.get("schema_head")
            out.append(_c("backup_manifest", PASS,
                          f"manifest for {summary['artifact']}"
                          + (f" (schema_head {head})" if head else "")))

    # Phase 9F.1: the manifest must identify the whole backup SET, not just the dump
    summary = bm.read_backup_manifest_summary(settings) \
        if (settings.backup_manifest_summary_file or "").strip() else None
    if summary is None:
        out.append(_c("backup_set_complete", WARN, "no backup manifest summary to assess"))
    elif (summary.get("manifest_version") or 1) < 2:
        out.append(_c("backup_set_complete", WARN,
                      "manifest is legacy v1 — re-run scripts/backup.sh for a full backup-set manifest"))
    else:
        gaps = []
        if summary.get("completed") is not True:
            gaps.append("completed!=true")
        if not summary.get("schema_head"):
            gaps.append("schema_head missing")
        if not summary.get("audit_head_event_id"):
            gaps.append("audit head missing")
        if not summary.get("archive_manifest_sha256"):
            gaps.append("archive manifest not linked")
        if (summary.get("active_jobs_at_backup") or 0) > 0:
            gaps.append(f"active_jobs={summary['active_jobs_at_backup']} at backup")
        if gaps:
            out.append(_c("backup_set_complete", WARN, "; ".join(gaps)))
        else:
            out.append(_c("backup_set_complete", PASS,
                          f"v2 set {summary.get('backup_id')}: schema+audit head+archive linked, "
                          f"idle at backup, integrity={summary.get('integrity_scheme')}"))

    if not (settings.backup_verified_marker_file or "").strip():
        out.append(_c("backup_verified", WARN,
                      "BACKUP_VERIFIED_MARKER_FILE not set — backup verification not tracked"))
    else:
        age = bm.marker_age_hours(settings.backup_verified_marker_file)
        if age is None:
            out.append(_c("backup_verified", WARN,
                          "backup never verified (run scripts/verify-backup.sh)"))
        elif age > settings.backup_verify_max_age_hours:
            out.append(_c("backup_verified", WARN,
                          f"last backup verify ~{int(age)}h ago "
                          f"(older than {settings.backup_verify_max_age_hours}h)"))
        else:
            out.append(_c("backup_verified", PASS, f"backup verified ~{int(age)}h ago"))

    if not (settings.restore_rehearsal_marker_file or "").strip():
        out.append(_c("restore_rehearsal", WARN,
                      "RESTORE_REHEARSAL_MARKER_FILE not set — rehearsal recency not tracked"))
    else:
        age = bm.marker_age_hours(settings.restore_rehearsal_marker_file)
        max_h = settings.restore_rehearsal_max_age_days * 24
        if age is None:
            out.append(_c("restore_rehearsal", WARN,
                          "no restore rehearsal recorded (run scripts/restore-rehearsal.sh)"))
        elif age > max_h:
            out.append(_c("restore_rehearsal", WARN,
                          f"last restore rehearsal ~{int(age / 24)}d ago "
                          f"(older than {settings.restore_rehearsal_max_age_days}d)"))
        else:
            out.append(_c("restore_rehearsal", PASS,
                          f"restore rehearsal ~{int(age / 24)}d ago"))
    return out


def _release_provenance_checks(settings: Settings) -> list[dict]:
    """Phase 10A: release-candidate provenance / supply-chain judgments. Reads
    the release-manifest summary + the running build identity — counts / basenames
    / statuses only, never paths or secret values."""
    from app.services import build_info as bi
    from app.services import release_manifest as rm

    prod = settings.is_production
    out: list[dict] = []
    v = bi.version_info()

    # git tree clean (dirty prod build must not ship)
    if v["git_tree_clean"] is False:
        out.append(_c("git_tree_clean", FAIL if prod else WARN, "built from a DIRTY source tree"))
    elif v["git_tree_clean"] is True:
        out.append(_c("git_tree_clean", PASS, "clean source tree"))
    else:
        out.append(_c("git_tree_clean", WARN, "tree clean state unknown"))

    # application version valid (prod must not ship the dev placeholder)
    ver = (v["app_version"] or "").strip()
    dev_ver = ver in ("", "0.0.0-dev") or ver.endswith("-dev")
    out.append(_c("application_version", (FAIL if prod else WARN) if dev_ver else PASS,
                  f"app_version={ver or 'unset'}" + (" (development placeholder)" if dev_ver else "")))

    out.append(_c("schema_head_captured", PASS if v["schema_head"] else WARN,
                  f"schema head {v['schema_head']}" if v["schema_head"] else "schema head not resolved"))

    if not (settings.release_manifest_summary_file or "").strip():
        out.append(_c("release_manifest", WARN,
                      "RELEASE_MANIFEST_SUMMARY_FILE not set — release manifest not tracked"))
        return out

    summary = rm.read_release_manifest_summary(settings)
    if summary is None:
        out.append(_c("release_manifest", WARN,
                      "no release manifest recorded (run scripts/build-release.sh)"))
        return out
    if summary.get("completed") is not True or not summary.get("integrity_scheme"):
        out.append(_c("release_manifest", WARN, "release manifest incomplete"))
    else:
        out.append(_c("release_manifest", PASS,
                      f"release {summary.get('release_id')} (integrity {summary.get('integrity_scheme')})"))

    # service image build ids all equal AND match the running build
    build_ids = summary.get("service_build_ids") or []
    if not build_ids:
        out.append(_c("service_image_build_match", WARN, "no service image build ids recorded"))
    elif len(build_ids) > 1:
        out.append(_c("service_image_build_match", FAIL,
                      f"service images differ ({len(build_ids)} distinct build ids)"))
    elif v["build_id"] and build_ids[0] != v["build_id"]:
        out.append(_c("service_image_build_match", WARN if not prod else FAIL,
                      "release manifest build id differs from the running build"))
    else:
        out.append(_c("service_image_build_match", PASS, "all service images share one build id"))

    dc = int(summary.get("image_digests_captured") or 0)
    out.append(_c("image_digests_captured", PASS if dc else WARN,
                  f"{dc} image digest(s) captured" if dc else "no registry image digests (local build)"))

    # SBOM
    if summary.get("sbom_present") and summary.get("sbom_sha256"):
        out.append(_c("sbom_present", PASS, f"SBOM sha256={summary['sbom_sha256'][:12]}…"))
    else:
        out.append(_c("sbom_present", FAIL if prod else WARN, "no SBOM in the release manifest"))

    # vulnerability scan status + policy
    vs = (summary.get("vulnerability_status") or "").lower()
    sev = summary.get("vulnerability_severities") or {}
    crit = int(sev.get("CRITICAL", 0) or 0) if isinstance(sev, dict) else 0
    if vs in ("", "none"):
        out.append(_c("vulnerability_scan", FAIL if prod else WARN, "no vulnerability scan recorded"))
    elif vs == "unavailable":
        pol = (settings.release_scanner_unavailable_policy or "warn").lower()
        st = FAIL if (prod and pol == "fail") else WARN
        out.append(_c("vulnerability_scan", st, "vulnerability scanner unavailable (not a pass)"))
    elif crit > settings.release_max_critical_vulnerabilities:
        out.append(_c("vulnerability_scan", FAIL,
                      f"{crit} critical vulnerabilit(y/ies) exceed policy "
                      f"(max {settings.release_max_critical_vulnerabilities})"))
    else:
        out.append(_c("vulnerability_scan", PASS if vs == "pass" else WARN,
                      f"scan {vs} severities={sev}"))

    out.extend(_release_reproducibility_checks(settings, summary, prod, v))
    return out


def _release_reproducibility_checks(settings, summary: dict, prod: bool, v: dict) -> list[dict]:
    """Phase 10A.1: reproducible-lock + supply-chain gates. Production FAILs when
    unmet; development WARNs. Counts/statuses only — no paths or secret values."""
    from app.services import release_manifest as rm

    out: list[dict] = []

    # ---- Python lock: exact + hashed + installed set matches the lock ----
    ls = rm.python_lock_status()
    if not ls["present"]:
        out.append(_c("python_lock_exact", FAIL if prod else WARN, "requirements.lock not present"))
        out.append(_c("python_lock_hashes_valid", FAIL if prod else WARN, "requirements.lock not present"))
    else:
        out.append(_c("python_lock_exact", PASS if ls["exact"] else (FAIL if prod else WARN),
                      f"{ls['package_count']} packages, all ==" if ls["exact"]
                      else f"unpinned requirements: {ls['unpinned']}"))
        out.append(_c("python_lock_hashes_valid", PASS if ls["hashed"] else (FAIL if prod else WARN),
                      "every requirement carries a sha256 hash" if ls["hashed"]
                      else "requirements missing --hash entries"))

    match = _installed_matches_lock()
    if match is None:
        out.append(_c("installed_packages_match_lock", WARN, "cannot compare installed packages to lock"))
    elif match["mismatches"]:
        out.append(_c("installed_packages_match_lock", FAIL if prod else WARN,
                      f"{len(match['mismatches'])} package(s) differ from the lock "
                      f"(e.g. {match['mismatches'][:3]})"))
    elif match["missing"]:
        out.append(_c("installed_packages_match_lock", FAIL if prod else WARN,
                      f"{len(match['missing'])} locked package(s) not installed"))
    else:
        out.append(_c("installed_packages_match_lock", PASS,
                      f"all {match['checked']} locked packages installed at the locked version"))

    # ---- base image digest pin (from the release manifest) ----
    py_pin = bool(summary.get("base_python_digest_pinned"))
    node_pin = bool(summary.get("base_node_digest_pinned"))
    if py_pin and node_pin:
        out.append(_c("base_images_digest_pinned", PASS, "python + node bases are digest-pinned"))
    else:
        unpinned = [n for n, p in (("python", py_pin), ("node", node_pin)) if not p]
        out.append(_c("base_images_digest_pinned", FAIL if prod else WARN,
                      f"base image(s) not digest-pinned: {unpinned}"))
    # the manifest recorded the resolved digest (verify re-checks it against build)
    out.append(_c("base_images_match_manifest", PASS if (py_pin and node_pin) else WARN,
                  "base digests recorded in the release manifest" if (py_pin and node_pin)
                  else "base digests missing from the manifest"))

    # ---- vulnerability scan completed + DB freshness ----
    completed = vs_completed(summary)
    out.append(_c("vulnerability_scan_completed", PASS if completed else (FAIL if prod else WARN),
                  "vulnerability scan completed" if completed
                  else "vulnerability scan NOT completed (unavailable/pending)"))
    age_days = _vuln_db_age_days(summary)
    max_days = settings.release_vuln_db_max_age_days
    if not completed or age_days is None:
        out.append(_c("vulnerability_db_fresh", WARN, "vulnerability DB age unknown (no completed scan)"))
    elif age_days > max_days:
        out.append(_c("vulnerability_db_fresh", FAIL if prod else WARN,
                      f"vulnerability DB ~{int(age_days)}d old (max {max_days}d)"))
    else:
        out.append(_c("vulnerability_db_fresh", PASS, f"vulnerability DB ~{int(age_days)}d old"))

    out.extend(_vuln_triage_checks(settings, summary, prod))
    out.extend(_decision_dossier_checks(settings, prod))

    # ---- release manifest authenticity (prod requires HMAC signing) ----
    scheme = (summary.get("integrity_scheme") or "").lower()
    if scheme == "hmac_sha256":
        out.append(_c("release_manifest_authenticated", PASS, "manifest HMAC-signed"))
    elif scheme == "sha256":
        out.append(_c("release_manifest_authenticated", FAIL if prod else WARN,
                      "manifest is SHA-256 only (production requires an HMAC-signed manifest)"))
    else:
        out.append(_c("release_manifest_authenticated", FAIL if prod else WARN,
                      "manifest integrity scheme unknown"))
    return out


def _load_decision_dossier(settings) -> dict | None:
    """Read the machine decision dossier JSON (docs/…). None if absent/unreadable."""
    import json as _json
    from pathlib import Path as _P

    path = (settings.vulnerability_decision_dossier_file or "").strip()
    if not path:
        return None
    try:
        from app.services import release_manifest as rm

        fp = _P(path)
        if not fp.is_absolute():
            fp = rm._repo_root() / path
        if fp.is_file():
            return _json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None


def _decision_dossier_checks(settings, prod: bool) -> list[dict]:
    """Phase 10B.3: load the machine decision dossier and return its release-check
    gates (inventory consistency, reachability completeness, dossier validity,
    scanner operator-approval presence, wheel reproducibility). Missing/unreadable
    dossier is handled by vuln_proposals.dossier_checks(None)."""
    from app.services import vuln_proposals as vp

    return vp.dossier_checks(_load_decision_dossier(settings), prod=prod)


def _security_posture(settings, checks: list[dict]) -> dict:
    """Phase 11A: a concise, HONEST security-posture summary for the UI. Surfaces
    the KNOWN accepted-risk (from the decision dossier) even when the live scan for
    THIS build is unavailable, and distinguishes local-accepted-risk from
    production-blocked. Does NOT change any release-check result; contains only
    counts / enum values / repo-relative doc names — no host paths or secrets."""
    dossier = _load_decision_dossier(settings)
    known_critical = exception_candidates = None
    reachability_complete = None
    if dossier:
        val = dossier.get("proposals_validation") or {}
        known_critical = val.get("remaining_critical_count")
        exception_candidates = val.get("exception_candidates")
        reachability_complete = val.get("reachability_complete")
    prod = settings.is_production
    has_fail = any(c.get("status") == FAIL for c in checks)
    open_critical = bool(known_critical and known_critical > 0)
    return {
        "operating_mode": "production" if prod else "local_single_user_dev",
        "known_critical_accepted": known_critical,           # e.g. 7 (from dossier) or None
        "exception_candidates": exception_candidates,
        "active_vulnerability_exceptions": 0,                # empty by policy this phase
        "reachability_assessed": bool(reachability_complete),
        "production_ready": not open_critical,               # honest: open CRITICAL => not prod-ready
        "release_check_passes": not has_fail,
        "risk_acceptance_doc": "docs/decisions/phase-11-local-single-user-risk-acceptance.md",
        "decision_dossier_doc": "docs/vulnerability-decision-dossier.md",
        "note": ("Known CRITICAL OS CVEs (no upstream fix) are accepted as local "
                 "single-user risk and are NOT hidden. This build is not "
                 "production-ready; release-check does not pass in production."),
    }


def _vuln_triage_checks(settings, summary: dict, prod: bool) -> list[dict]:
    """Phase 10B: CRITICAL/HIGH gating, scanner provenance, report integrity, and
    exception validity — read from the enriched scan fields in the manifest
    summary (counts/statuses only, no paths)."""
    out: list[dict] = []
    sev = summary.get("vulnerability_severities") or {}
    crit = int(sev.get("CRITICAL", 0) or 0) if isinstance(sev, dict) else 0
    high = int(sev.get("HIGH", 0) or 0) if isinstance(sev, dict) else 0
    # unapproved CRITICALs = total minus those covered by ACTIVE valid exceptions
    unapproved = summary.get("critical_unapproved")
    if not isinstance(unapproved, int):
        unapproved = crit  # no exception evaluation recorded -> none approved

    # scanner provenance — PRODUCTION ACCEPTS ONLY: `digest_pinned` (a real
    # registry RepoDigest) or `local_image_id_verified` (operator-attested full
    # image id, no registry digest). An `unverified` scanner is NEVER a
    # production PASS (a synthesized/short digest degrades to unverified upstream).
    prov = (summary.get("scanner_provenance_status") or "").lower()
    if prov in ("verified", "digest_pinned"):
        out.append(_c("scanner_provenance_verified", PASS,
                      f"scanner provenance {prov} (registry digest / attested)"))
    elif prov == "local_image_id_verified":
        out.append(_c("scanner_provenance_verified", PASS,
                      "scanner provenance local_image_id_verified "
                      "(operator-attested local image id; no registry RepoDigest)"))
    elif prov in ("recorded_unverified", "unverified", ""):
        st = (FAIL if (prod and settings.release_require_scanner_provenance) else WARN)
        out.append(_c("scanner_provenance_verified", st,
                      "scanner provenance unverified — no real RepoDigest and not "
                      "operator-verified; production requires digest_pinned or "
                      "local_image_id_verified"))
    else:
        out.append(_c("scanner_provenance_verified", WARN, f"scanner provenance {prov}"))

    # report integrity (a completed scan must carry a valid integrity hash)
    ri = summary.get("vulnerability_report_integrity")
    if ri is True:
        out.append(_c("vulnerability_report_integrity", PASS, "scan report integrity hash valid"))
    elif ri is False:
        out.append(_c("vulnerability_report_integrity", FAIL, "scan report integrity MISMATCH"))
    else:
        out.append(_c("vulnerability_report_integrity",
                      WARN if not vs_completed(summary) else (FAIL if prod else WARN),
                      "no scan report integrity recorded"))

    # exceptions validity (expired / invalid exceptions are a hard fail)
    exp = int(summary.get("vulnerability_exceptions_expired", 0) or 0)
    inv = int(summary.get("vulnerability_exceptions_invalid", 0) or 0)
    act = int(summary.get("vulnerability_exceptions_active", 0) or 0)
    if exp or inv:
        out.append(_c("vulnerability_exceptions_valid", FAIL,
                      f"{exp} expired / {inv} invalid exception(s)"))
    else:
        out.append(_c("vulnerability_exceptions_valid", PASS,
                      f"{act} active exception(s), none expired/invalid"))

    # CRITICAL policy: unapproved CRITICAL > 0 => FAIL in production
    if not vs_completed(summary):
        out.append(_c("critical_vulnerabilities", FAIL if prod else WARN,
                      "no completed scan — CRITICAL count unknown"))
    elif unapproved > settings.release_max_critical_vulnerabilities:
        out.append(_c("critical_vulnerabilities", FAIL,
                      f"{unapproved} unapproved CRITICAL (of {crit}; policy max "
                      f"{settings.release_max_critical_vulnerabilities})"))
    else:
        out.append(_c("critical_vulnerabilities", PASS,
                      f"{crit} CRITICAL, {unapproved} unapproved (within policy)"))

    # HIGH policy: warn (know the count) | fail
    hp = (settings.release_high_vuln_policy or "warn").lower()
    if not vs_completed(summary):
        out.append(_c("high_vulnerabilities", WARN, "no completed scan — HIGH count unknown"))
    elif hp == "fail" and high > settings.release_max_high_vulnerabilities:
        out.append(_c("high_vulnerabilities", FAIL,
                      f"{high} HIGH exceed policy (max {settings.release_max_high_vulnerabilities})"))
    else:
        out.append(_c("high_vulnerabilities", PASS if high == 0 else WARN,
                      f"{high} HIGH (policy={hp}; tracked)"))

    # remediation status (informational rollups recorded by the build)
    for name, key in (("base_image_remediation_status", "base_remediation_status"),
                      ("dependency_remediation_status", "dependency_remediation_status")):
        val = summary.get(key)
        out.append(_c(name, PASS if val in ("clean", "remediated", "no_action_needed") else WARN,
                      f"{name}: {val or 'not recorded'}"))

    # Phase 10B.2 apt reproducibility — PRODUCTION POLICY: the apt package set is
    # recorded + sha256-hashed (so a rebuild from the same base digest can be
    # checked for drift), but NOT snapshot-pinned. We NEVER report floating apt as
    # fully reproducible: `pinned` PASSes, `recorded_unpinned` is a WARN (accepted,
    # documented limitation — deps/base fixed, apt transaction not fully
    # reproducible), and a missing record is a WARN. It is not a hard FAIL because
    # it is a reproducibility-audit gap, not a vulnerability.
    apt_status = (summary.get("apt_packages_pinned") or "not_recorded").lower()
    apt_sha = str(summary.get("apt_packages_sha256") or "")
    if apt_status == "pinned":
        out.append(_c("apt_packages_pinned", PASS, "apt packages exact-version pinned (snapshot)"))
    elif apt_status == "recorded_unpinned":
        out.append(_c("apt_packages_pinned", WARN,
                      f"apt set recorded+hashed (sha256={apt_sha[:12]}…) but NOT snapshot-pinned "
                      "— deps/base fixed, apt transaction not fully reproducible"))
    else:
        out.append(_c("apt_packages_pinned", WARN, "apt package set not recorded"))
    return out


def vs_completed(summary: dict) -> bool:
    st = (summary.get("vulnerability_status") or "").lower()
    return st in ("pass", "warn", "fail")


def _vuln_db_age_days(summary: dict):
    from datetime import datetime, timezone

    raw = summary.get("vulnerability_db_updated_at")
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return None


def _installed_matches_lock():
    """Compare importlib.metadata installed versions to requirements.lock pins.
    Returns {checked, mismatches[], missing[]} or None if the lock is absent."""
    import importlib.metadata as im
    from pathlib import Path

    from app.services import release_manifest as rm

    lock = rm._repo_root() / rm.PYTHON_LOCK
    if not lock.is_file():
        return None
    pins: dict[str, str] = {}
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("--hash="):
            continue
        req = line.split("\\", 1)[0].strip()
        if "==" in req:
            name, ver = req.split("==", 1)
            pins[name.strip().lower().replace("_", "-")] = ver.strip()
    checked = mismatches = 0
    mism: list[str] = []
    missing: list[str] = []
    for name, want in pins.items():
        try:
            have = im.version(name)
        except im.PackageNotFoundError:
            missing.append(name)
            continue
        checked += 1
        if have.lower() != want.lower():
            mism.append(f"{name} {have}!={want}")
    return {"checked": checked, "mismatches": mism, "missing": missing}


def _age_seconds_from_ts(raw) -> float | None:
    from datetime import datetime, timezone

    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except (ValueError, TypeError):
        return None


def _age_from_release_id(rid) -> float | None:
    import re
    from datetime import datetime, timezone

    m = re.match(r"rel-(\d{14})-", str(rid or ""))
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        return None


def _runtime_release_status(v: dict, manifest: dict | None) -> dict:
    """Phase 11B: explicitly SEPARATE the running runtime from the last scanned
    release, so a stale manifest is never presented as the current runtime's scan.
    Build ids / short commits / ages only — no host paths or secrets."""
    runtime_build = v.get("build_id")
    manifest_build = (manifest or {}).get("build_id")
    has_manifest = bool(manifest)
    matches = bool(has_manifest and runtime_build and manifest_build
                   and runtime_build == manifest_build)
    if not has_manifest:
        verdict, message = "no_scanned_release", \
            "No scanned release information is available for this runtime."
    elif matches:
        verdict, message = "match", "Current runtime matches the scanned release."
    else:
        verdict, message = "mismatch", (
            "Running development build differs from the last scanned release — the "
            "vulnerability scan below reflects that release, not this running build.")
    return {
        "verdict": verdict,  # match | mismatch | no_scanned_release
        "message": message,
        "status_source": "release_manifest" if has_manifest else "none",
        "manifest_matches_runtime": matches,
        "runtime_build_id": runtime_build,
        "manifest_build_id": manifest_build,
        "runtime_app_version": v.get("app_version"),
        "runtime_git_commit": (str(v.get("git_commit") or "")[:12] or None),
        "runtime_schema_head": v.get("schema_head"),
        "runtime_git_tree_clean": v.get("git_tree_clean"),
        "manifest_app_version": (manifest or {}).get("app_version"),
        "manifest_release_id": (manifest or {}).get("release_id"),
        "manifest_git_commit": (str((manifest or {}).get("git_commit") or "")[:12] or None),
        "manifest_age_seconds": _age_from_release_id((manifest or {}).get("release_id")),
        "scan_age_seconds": _age_seconds_from_ts((manifest or {}).get("vulnerability_db_updated_at")),
    }


def first_run_status(session, settings: Settings | None = None) -> dict:
    """Phase 11B: a fresh-install setup checklist — what is already done and what
    to configure next, with SAFE deep-links (never a dangerous auto-run). Counts /
    booleans / enum values / route paths only — no secrets, no host paths."""
    import os as _os

    settings = settings or get_settings()
    from app.models import Job, LikedVideo, Video

    video_count = session.query(Video).count()
    liked_count = session.query(LikedVideo).count()
    job_count = session.query(Job).count()
    is_fresh = video_count == 0 and job_count == 0

    def _writable(p) -> bool:
        try:
            return _os.path.isdir(str(p)) and _os.access(str(p), _os.W_OK)
        except OSError:
            return False

    auth_mode = str(getattr(settings, "auth_mode", "disabled") or "disabled").lower()
    bind_host = str(getattr(settings, "web_bind_host", "127.0.0.1") or "127.0.0.1").strip()
    bind_all = bool(getattr(settings, "web_bind_is_all_interfaces", False))
    cookies_ok = bool(str(getattr(settings, "cookies_file", "") or "").strip())
    default_profile = str(getattr(settings, "default_profile", "") or "")
    try:
        from app.services import backup_manifest as bm

        backup_ok = bool(bm.read_backup_manifest_summary(settings))
    except Exception:  # noqa: BLE001
        backup_ok = False

    storage_ok = (_writable(settings.archive_root) and _writable(settings.config_root)
                  and _writable(settings.log_root))

    items = [
        {"key": "storage", "label": "Storage directories", "done": storage_ok, "optional": False,
         "link": "/settings",
         "detail": "ARCHIVE_ROOT / CONFIG_ROOT / LOG_ROOT are present and writable."},
        {"key": "auth", "label": "Authentication", "done": auth_mode != "disabled",
         "warn": auth_mode == "disabled", "danger": bind_all and auth_mode == "disabled",
         "optional": False, "link": "/settings",
         "detail": (f"authentication enabled ({auth_mode})." if auth_mode != "disabled"
                    else ("authentication is DISABLED and the web port is bound to ALL interfaces "
                          f"({bind_host}) — the admin console is reachable from your LAN with NO "
                          "login. Enable auth now, or set WEB_BIND_HOST=127.0.0.1." if bind_all
                          else "authentication is DISABLED — fine for a local/loopback single user, but "
                               "enable auth (and avoid a 0.0.0.0 bind) before any LAN or internet exposure."))},
        {"key": "cookies", "label": "YouTube cookies", "done": cookies_ok, "optional": True,
         "link": "/settings",
         "detail": "optional: a cookies file improves fetch reliability and reduces throttling."},
        {"key": "takeout", "label": "Import Google Takeout", "done": liked_count > 0, "optional": True,
         "link": "/takeout",
         "detail": "import your liked videos / watch history to populate the archive."},
        {"key": "metadata", "label": "Fetch metadata", "done": video_count > 0, "optional": True,
         "link": "/liked-videos",
         "detail": "fetch video metadata (info.json / subtitles / thumbnails) — no media body."},
        {"key": "download_policy", "label": "Download profile", "done": bool(default_profile),
         "optional": False, "link": "/settings",
         "detail": f"default save profile: {default_profile or '(unset)'}."},
        {"key": "backup", "label": "Backup destination", "done": backup_ok, "optional": False,
         "link": "/settings",
         "detail": "configure and run backups (scripts/backup.sh) before relying on the archive."},
    ]
    return {
        "is_fresh": is_fresh,
        "video_count": video_count, "liked_count": liked_count, "job_count": job_count,
        "auth_mode": auth_mode,
        "web_bind_host": bind_host,
        "web_bind_all_interfaces": bind_all,
        "exposure_warning": auth_mode == "disabled",
        # none (auth on) | warn (auth off, loopback) | danger (auth off, all interfaces)
        "exposure_level": ("none" if auth_mode != "disabled"
                           else "danger" if bind_all else "warn"),
        "exposure_note": (
            (f"DANGER: auth is disabled AND the web port is bound to all interfaces ({bind_host}). "
             "The admin console is reachable from your LAN with no login. Enable auth or set "
             "WEB_BIND_HOST=127.0.0.1 immediately.") if (bind_all and auth_mode == "disabled")
            else ("Local single-user: bind to 127.0.0.1 and keep auth off only on a trusted host. "
                  "Enable auth before exposing to a LAN or the internet.")),
        "items": items,
        "done_count": sum(1 for i in items if i["done"]),
        "total_count": len(items),
    }


def release_readiness(settings: Settings | None = None) -> dict:
    """Phase 10A/11B: consolidated read-only release/provenance readiness (API/UI).
    Version identity + manifest summary + a runtime-vs-scanned-release comparison +
    supply-chain statuses — no paths/secrets."""
    settings = settings or get_settings()
    from app.services import build_info as bi
    from app.services import release_manifest as rm

    checks = _release_provenance_checks(settings)
    v = bi.version_info()
    manifest = rm.read_release_manifest_summary(settings)
    return {
        "overall": _overall(checks),
        "counts": _counts(checks),
        "checks": checks,
        "version": v,
        "manifest": manifest,
        "security_posture": _security_posture(settings, checks),   # Phase 11A
        "runtime_release": _runtime_release_status(v, manifest),   # Phase 11B
    }


def backup_readiness(settings: Settings | None = None) -> dict:
    """Phase 9F: consolidated read-only backup/DR readiness (API/UI). Ages,
    counts, and artifact basenames only — never paths or secret values."""
    settings = settings or get_settings()
    from app.services import backup_manifest as bm

    checks = [_backup_freshness_check(settings)] + _backup_integrity_checks(settings)
    ba = bm.marker_age_hours(settings.backup_marker_file)
    va = bm.marker_age_hours(settings.backup_verified_marker_file)
    ra = bm.marker_age_hours(settings.restore_rehearsal_marker_file)
    return {
        "overall": _overall(checks),
        "counts": _counts(checks),
        "checks": checks,
        "manifest": bm.read_backup_manifest_summary(settings),
        "backup_age_hours": round(ba, 1) if ba is not None else None,
        "backup_verified_age_hours": round(va, 1) if va is not None else None,
        "restore_rehearsal_age_days": round(ra / 24.0, 1) if ra is not None else None,
    }


def release_check(session: Session, settings: Settings | None = None) -> dict:
    """Phase 9D deploy gate: production_check + archive presence + migration
    status + backup freshness + build version. PASS/WARN/FAIL; no secrets/paths.
    Phase 9F adds backup manifest / verification / restore-rehearsal recency."""
    settings = settings or get_settings()
    from app.services import build_info as bi

    pc = production_check(session, settings)
    checks = list(pc["checks"])

    try:
        am = archive_media_check(session, settings)
        checks.append(_c("archive_media_presence", PASS if am["ok"] else FAIL,
                         f"{am['existing']}/{am['db_video_media_files']} video files present, "
                         f"{am['missing']} missing, {am['duplicate_video_media_files']} duplicate"))
    except Exception:  # noqa: BLE001
        checks.append(_c("archive_media_presence", WARN, "archive media check unavailable"))

    try:
        code_head = bi.code_schema_head()
        db_head = bi.db_schema_head(session)
        if code_head and db_head and code_head == db_head:
            checks.append(_c("migration_status", PASS, f"schema at head {code_head}"))
        elif db_head is None:
            checks.append(_c("migration_status", WARN, "no alembic_version (dev DB via create_all)"))
        else:
            checks.append(_c("migration_status", FAIL,
                             f"DB head {db_head} != code head {code_head} — run migrate"))
    except Exception:  # noqa: BLE001
        checks.append(_c("migration_status", WARN, "migration status unknown"))

    checks.append(_backup_freshness_check(settings))
    checks.extend(_backup_integrity_checks(settings))
    checks.extend(_release_provenance_checks(settings))  # Phase 10A

    info = bi.build_info()
    checks.append(_c("build_version", PASS,
                     f"build_id={info.get('build_id')} app_version={info.get('app_version')}"))

    return {
        "overall": _overall(checks),
        "counts": _counts(checks),
        "checks": checks,
        "app_env": settings.app_env,
        "auth_mode": (settings.auth_mode or "disabled"),
        "default_body_profile": settings.effective_body_archive_profile,
        "disk_min_free_gb": settings.archive_min_free_gb,
        "backup_reminder": pc.get("backup_reminder", ""),
    }
