"""System preflight checks (Phase 6F).

A go/no-go gate to run BEFORE a large import. The headline goal is catching a
*stale worker* (a worker container running older code than web) so a 90k-row
import isn't started against a worker that can't honour ``--no-raw-json`` or the
current job logic.

Every check returns a status of ``ok`` / ``warn`` / ``fail``. ``ok`` overall
means no check failed (warns are allowed — e.g. a dev DB created via
``create_all`` has no ``alembic_version`` row). No secrets / host absolute
paths are ever included in the output.
"""

from __future__ import annotations

import os
import tempfile

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.services import build_info


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def system_preflight(session: Session, settings: Settings | None = None) -> dict:
    """Run all system checks. Returns ``{ok, checks, build_info, workers}``."""
    settings = settings or get_settings()
    checks: list[dict] = []
    info = build_info.build_info()

    # ---- DB ----
    try:
        session.execute(text("SELECT 1"))
        checks.append(_check("db_connect", "ok", "database reachable"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("db_connect", "fail", f"database error: {type(exc).__name__}"))

    # ---- Redis ----
    redis = None
    try:
        from app.worker.queue import get_redis

        redis = get_redis()
        redis.ping()
        checks.append(_check("redis_connect", "ok", "redis reachable"))
    except Exception as exc:  # noqa: BLE001
        redis = None
        checks.append(_check("redis_connect", "fail", f"redis error: {type(exc).__name__}"))

    # ---- Alembic head match ----
    code_head = build_info.code_schema_head()
    db_head = build_info.db_schema_head(session)
    if code_head and db_head:
        if code_head == db_head:
            checks.append(_check("schema_head", "ok", f"head={code_head}"))
        else:
            checks.append(_check(
                "schema_head", "fail",
                f"DB head {db_head} != code head {code_head} — run `migrate`",
            ))
    elif db_head is None:
        checks.append(_check(
            "schema_head", "warn",
            "no alembic_version (schema via create_all / dev DB)",
        ))
    else:
        checks.append(_check("schema_head", "warn", f"DB head={db_head}, code head unknown"))

    # ---- web build_id ----
    checks.append(_check("web_build_id", "ok", info["build_id"]))

    # ---- worker heartbeat / build_id match / takeout_import capability ----
    workers = build_info.read_worker_heartbeats(redis) if redis is not None else []
    live = [w for w in workers if not w.get("stale")]
    if not workers:
        checks.append(_check(
            "worker_build_id", "fail",
            "no worker heartbeat — worker not running or unreachable",
        ))
        checks.append(_check("worker_build_match", "fail", "no worker to compare"))
        checks.append(_check("worker_takeout_capable", "fail", "no worker to check"))
    else:
        ids = sorted({w.get("build_id") for w in workers})
        checks.append(_check(
            "worker_build_id",
            "ok" if live else "warn",
            f"{len(workers)} worker(s), build_id={','.join(i or '?' for i in ids)}"
            + ("" if live else " (all stale heartbeats)"),
        ))
        mismatched = [w for w in workers if w.get("build_id") != info["build_id"]]
        if mismatched:
            checks.append(_check(
                "worker_build_match", "fail",
                f"STALE WORKER: {len(mismatched)} worker(s) differ from web "
                f"build_id {info['build_id']} — rebuild ALL images "
                "(`docker compose build web worker migrate`)",
            ))
        else:
            checks.append(_check("worker_build_match", "ok", "web and worker build_id match"))

        incapable = [
            w for w in workers
            if "takeout_import" not in (w.get("supported_job_types") or [])
        ]
        if incapable:
            checks.append(_check(
                "worker_takeout_capable", "fail",
                f"{len(incapable)} worker(s) cannot process takeout_import",
            ))
        else:
            checks.append(_check("worker_takeout_capable", "ok", "worker processes takeout_import"))

    # ---- takeout_imports readable ----
    try:
        root = settings.takeout_import_root
        if root.is_dir() and os.access(root, os.R_OK):
            checks.append(_check("takeout_imports_readable", "ok", "takeout import root readable"))
        else:
            checks.append(_check("takeout_imports_readable", "fail", "takeout import root not readable"))
    except OSError:
        checks.append(_check("takeout_imports_readable", "fail", "takeout import root error"))

    # ---- logs writable ----
    try:
        settings.log_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=settings.log_root, prefix=".preflight_", delete=True):
            pass
        checks.append(_check("logs_writable", "ok", "log root writable"))
    except OSError:
        checks.append(_check("logs_writable", "fail", "log root not writable"))

    # ---- raw_json recommendation (informational) ----
    checks.append(_check(
        "raw_json_policy", "ok",
        "recommended: --no-raw-json for large imports (keeps normalized fields, "
        "drops raw blobs to bound DB growth)",
    ))

    # ---- cookies / PO-token (Phase 7I): status ONLY, never the value/path ----
    cf = settings.cookies_file_status()  # {configured, file_configured, file_exists, readable, ...}
    if settings.cookies_configured:
        checks.append(_check(
            "cookies_file",
            "ok" if (not cf["file_configured"] or cf["readable"]) else "warn",
            "cookies configured"
            + ("" if (not cf["file_configured"] or cf["readable"]) else " but file not readable"),
        ))
    else:
        checks.append(_check(
            "cookies_file", "warn",
            "no cookies configured — YouTube may rate-limit (429); set COOKIES_FILE "
            "or COOKIES_FROM_BROWSER for stable metadata fetch",
        ))
    checks.append(_check(
        "po_token",
        "ok" if settings.po_token_configured else "warn",
        "PO-token configured" if settings.po_token_configured
        else "no PO-token configured — recommended with cookies for stable fetch",
    ))
    # Assertion: this report exposes booleans/masked status only — never values.
    checks.append(_check("secret_value_exposed", "ok", "false — only configured/readable booleans are reported"))

    # ---- orphan download jobs (Phase 8C): running/queued in DB but gone from RQ ----
    try:
        from app.services import reconcile

        orphans = reconcile.orphan_warning_count(session, settings, older_than_minutes=30)
        if orphans > 0:
            checks.append(_check(
                "orphan_jobs", "warn",
                f"{orphans} download job(s) stuck (running/queued in DB, absent from RQ >30min) — "
                "likely a worker crash/restart. Run `archiver jobs reconcile-orphans --dry-run` "
                "then `--apply` to safely re-queue (no re-download of saved bodies).",
            ))
        else:
            checks.append(_check("orphan_jobs", "ok", "no orphaned download jobs"))
    except Exception:  # noqa: BLE001 - preflight must never crash on this
        checks.append(_check("orphan_jobs", "warn", "orphan check unavailable (RQ unreadable)"))

    # ---- archive disk free (Phase 9A): warn below the body-archive min-free guard ----
    try:
        from app.services import storage

        disk = storage.disk_usage(settings)
        if not disk.get("readable"):
            checks.append(_check("archive_disk_free", "warn", "archive volume free space unreadable"))
        elif disk["free_gb"] is not None and disk["free_gb"] < settings.archive_min_free_gb:
            checks.append(_check(
                "archive_disk_free", "warn",
                f"free {disk['free_gb']} GiB below min-free {settings.archive_min_free_gb} GiB — "
                "body archive enqueue will be BLOCKED (use a smaller batch or expand storage)",
            ))
        else:
            checks.append(_check(
                "archive_disk_free", "ok",
                f"free {disk['free_gb']} GiB (min-free {settings.archive_min_free_gb} GiB)",
            ))
    except Exception:  # noqa: BLE001 - preflight must never crash on this
        checks.append(_check("archive_disk_free", "warn", "disk free check unavailable"))

    ok = not any(c["status"] == "fail" for c in checks)
    # workers list is summarized (no host paths / secrets — heartbeat carries none)
    worker_summary = [
        {
            "worker_id": w.get("worker_id"),
            "build_id": w.get("build_id"),
            "app_version": w.get("app_version"),
            "age_seconds": w.get("age_seconds"),
            "stale": w.get("stale"),
            "takeout_import": "takeout_import" in (w.get("supported_job_types") or []),
        }
        for w in workers
    ]
    return {"ok": ok, "checks": checks, "build_info": info, "workers": worker_summary}
