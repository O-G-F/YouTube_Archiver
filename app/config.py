"""Application configuration.

Settings are loaded from environment variables (and an optional `.env` file).
An optional YAML overlay (``CONFIG_ROOT/archiver.yaml``) can override
non-secret defaults, matching requirement 4.1 ("DB + YAML/TOML + Web UI").
Secrets (cookies, tokens) are always sourced from env / files, never YAML.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Storage roots (absolute paths inside the container) ----
    archive_root: Path = Path("/archive")
    config_root: Path = Path("/config")
    log_root: Path = Path("/logs")
    takeout_import_root: Path = Path("/takeout_imports")
    obsidian_export_root: Path = Path("/obsidian_export")

    # ---- Takeout import session retention (Phase 6E) — default OFF ----
    # When > 0, `takeout sessions cleanup` (and any future auto-prune) deletes
    # sessions older than N days and/or beyond the most-recent KEEP_LAST. Jobs
    # and imported data are NEVER deleted by session cleanup.
    takeout_import_session_retention_days: int = 0
    takeout_import_session_keep_last: int = 0

    # ---- Takeout import session AUTO cleanup (Phase 6F) — default OFF ----
    # When enabled, the scheduler loop prunes old import SESSION rows (only;
    # never jobs / imported data) at most every INTERVAL_HOURS using the
    # retention/keep_last bounds above. Requires at least one bound to be set.
    takeout_import_session_cleanup_enabled: bool = False
    takeout_import_session_cleanup_interval_hours: int = 24

    # ---- Database ----
    database_url: str = "sqlite:///./data/archiver.sqlite3"

    # ---- Redis / RQ ----
    redis_url: str = "redis://localhost:6379/0"
    rq_queue: str = "archiver"

    # ---- yt-dlp / external tools ----
    ytdlp_binary: str = "yt-dlp"
    ffmpeg_location: str = ""
    deno_path: str = ""
    # Remote components for the YouTube JS challenge (n-sig / signature) solver.
    # Required together with a JS runtime (deno) to avoid throttling/403s.
    # Set to "" to disable.
    ytdlp_remote_components: str = "ejs:github"
    cookies_file: str = ""

    # ---- Defaults ----
    default_profile: str = "video_compressed_1080p"
    # Default subtitle languages for normal profiles. Intentionally NOT "all".
    # IMPORTANT: use EXACT codes, not regex like "en.*": yt-dlp anchors sub-lang
    # patterns, so "en.*" matches every English-sourced AUTO-TRANSLATION
    # (en-de-DE, en-fr, en-en, ...) -> hundreds of tracks -> HTTP 429. Plain
    # "en" matches only the "en" track. Add more exact codes as needed
    # (e.g. "ja,en,en-US,en-orig").
    default_sub_langs: str = "ja,en"
    # Subtitle languages for the full-archive profile (video_best_archive).
    # Set ARCHIVE_SUB_LANGS=all for a complete capture (heavier; 429-prone).
    archive_sub_langs: str = "ja,en"
    # Safety cap when expanding a playlist/channel (0 = unlimited).
    expand_max_items: int = 0
    # Default timeout (seconds) for a single yt-dlp subprocess (0 = no timeout).
    ytdlp_timeout: int = 0
    # Cap total comments fetched per video when --write-comments is active
    # (0 = unlimited; protects against popular videos with millions of comments).
    ytdlp_max_comments: int = 0
    # Cap for comments_refresh jobs (Phase 4A). Default finite for safety;
    # 0 = unlimited. yt-dlp --extractor-args youtube:max_comments.
    comment_refresh_max_comments: int = 200

    # ---- Scheduler (Phase 2B / 4B) ----
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 3600
    # Phase 4B: scheduler also enqueues due comment refreshes when enabled.
    scheduler_comments_enabled: bool = False
    scheduler_comments_limit_per_run: int = 10

    # ---- Comments refresh rate control / retry (Phase 4B) ----
    comments_refresh_job_delay_seconds: float = 0.0
    comments_refresh_retry_backoff_seconds: int = 21600  # 6h backoff after 429
    comments_refresh_max_retry: int = 5

    # ---- Live chat (Phase 4B) ----
    # Cap messages ingested per live_chat refresh (0 = unlimited). Live chats can
    # be very large; a finite cap keeps a single refresh bounded.
    live_chat_max_messages: int = 0
    # Default interval (seconds) before re-refreshing a live chat (default 30d).
    live_chat_refresh_interval_seconds: int = 2592000

    # ---- Rate control / retry (Phase 2B) ----
    # Sleep before each download job starts (spaces out requests on one worker).
    download_job_delay_seconds: float = 0.0
    # Per-job-type delays (Phase 7A). 0 -> fall back to the download delay where
    # relevant. Lets you throttle metadata / subtitle / comment refreshes apart
    # from heavy body downloads.
    metadata_refresh_job_delay_seconds: float = 0.0
    subtitles_refresh_job_delay_seconds: float = 0.0
    # Passed to yt-dlp --retry-sleep (seconds between retries, e.g. on HTTP 429).
    ytdlp_retry_backoff_seconds: int = 0
    # Intended download concurrency. With a single RQ worker this is effectively
    # 1; scale with `docker compose up --scale worker=N`. Surfaced for tooling.
    max_concurrent_download_jobs: int = 1

    # ---- Download retry / backoff (Phase 7A) ----
    # A retryable failure (429 / incomplete data / fragments) schedules a backoff
    # retry up to MAX_ATTEMPTS. Backoff = BACKOFF * MULTIPLIER**attempt (+jitter).
    download_retry_max_attempts: int = 5
    download_retry_backoff_seconds: int = 600  # 10 min base
    download_retry_backoff_multiplier: float = 2.0
    download_retry_jitter_seconds: int = 60
    # Default subtitle languages for subtitles_refresh (falls back to default_sub_langs).
    subtitles_refresh_sub_langs: str = ""

    # ---- Scheduler retry pickup (Phase 7A; optional) ----
    scheduler_retry_enabled: bool = False
    scheduler_retry_limit_per_run: int = 10

    # ---- Liked-videos bulk archive (Phase 7C) ----
    # Safe defaults: archive a SMALL number at a time (start with 10-30).
    liked_archive_default_limit: int = 20
    # Hard cap on how many jobs a single enqueue call may create (safety brake).
    liked_archive_max_enqueue_per_run: int = 50
    # Profile used for a body archive (downloads the video BODY). Kept for
    # backward-compat; the production body default is body_archive_default_profile
    # (Phase 9A) which the plan/enqueue paths actually use.
    liked_archive_default_profile: str = "video_compressed_1080p"
    # Extra per-job sleep applied to liked-archive download jobs (throttling).
    liked_archive_job_delay_seconds: float = 0.0

    # ---- Production body-archive controls (Phase 9A) ----
    # Default body-archive profile for the plan/enqueue paths. The comments-light
    # profile avoids the large `comments` DB table growth seen when bulk-archiving
    # with a comments-enabled profile (video_compressed_1080p). Set this to select
    # a different body profile; the comments-heavy one is NOT recommended at scale.
    body_archive_default_profile: str = "video_compressed_1080p_light"
    # Minimum free space (GiB) the archive volume must retain. A body-archive
    # enqueue that would (or already does) leave less than this is BLOCKED unless
    # explicitly overridden (--allow-low-disk). Guard is skipped only when free
    # space cannot be read (e.g. the archive path is unavailable).
    archive_min_free_gb: float = 500.0
    # Conservative fallback per-video body size estimate (MiB) used by the size
    # estimator when there is not enough saved-media history to estimate from.
    archive_size_estimate_fallback_mb: float = 300.0
    # How many saved 'video' media files must exist before the estimator trusts
    # measured sizes instead of the fixed fallback.
    archive_size_estimate_min_samples: int = 10
    # Optional scheduler pickup of pending liked archives (default OFF).
    scheduler_liked_archive_enabled: bool = False
    scheduler_liked_archive_limit_per_run: int = 2  # body DL is heavy -> tiny

    # ---- Liked metadata-run safety (Phase 7I) ----
    # metadata_only is light, so allow a larger per-batch cap than the body cap.
    liked_metadata_max_enqueue_per_run: int = 200
    # Extra per-job delay for liked metadata jobs (spaces out requests -> fewer 429).
    liked_metadata_job_delay_seconds: float = 0.0
    # Phase 7L: random jitter (0..N s) ADDED to the metadata delay so requests are
    # not perfectly periodic (a fixed cadence is easier for YouTube to rate-limit).
    liked_metadata_job_delay_jitter_seconds: float = 0.0
    # Rate-limit ratio (rate_limited / attempted in a run) thresholds: WARN at/above
    # the warn ratio; STOP a staged/full run at/above the stop ratio.
    liked_metadata_warn_on_rate_limit_ratio: float = 0.5
    liked_metadata_stop_on_rate_limit_ratio: float = 0.8

    # ---- Scheduler liked passes (Phase 7D) — default OFF / small limits ----
    # metadata_only pass (no body DL): safe to run a bit more.
    scheduler_liked_metadata_enabled: bool = False
    scheduler_liked_metadata_limit_per_run: int = 10
    # body archive pass tuning (empty -> liked_archive_default_profile / all sources).
    scheduler_liked_archive_profile: str = ""
    scheduler_liked_archive_source: str = ""
    scheduler_liked_archive_missing_body_only: bool = True
    # retryable liked re-queue pass (respects next_retry_at + attempt cap).
    scheduler_liked_retry_enabled: bool = False
    scheduler_liked_retry_limit_per_run: int = 3
    # Global brake: skip a liked enqueue pass while liked-archive jobs are still
    # queued/running (avoid piling up while a batch is in flight).
    scheduler_liked_suppress_when_active: bool = True

    # ---- Scheduler run retention (Phase 7F) — default OFF (no auto-delete) ----
    # When > 0, the scheduler loop prunes scheduler_runs older than N days /
    # beyond the most-recent KEEP_LAST. 0 disables that bound. Jobs are NEVER
    # deleted by retention. Manual `scheduler runs cleanup` always works.
    scheduler_run_retention_days: int = 0
    scheduler_run_keep_last: int = 0

    @property
    def effective_body_archive_profile(self) -> str:
        """Production body-archive profile (Phase 9A).

        Falls back to ``liked_archive_default_profile`` only if the Phase 9A
        setting is explicitly blanked, so the comments-light default holds.
        """
        return (self.body_archive_default_profile or "").strip() or self.liked_archive_default_profile

    @property
    def effective_scheduler_liked_archive_profile(self) -> str:
        # Phase 9A: the scheduler's body pass defaults to the production body
        # profile (comments-light) unless SCHEDULER_LIKED_ARCHIVE_PROFILE is set.
        return (self.scheduler_liked_archive_profile or "").strip() or self.effective_body_archive_profile

    # ---- YouTube fetch stabilization secrets (Phase 7A / 7B) ----
    # All are SECRETS: never returned by the API / shown in the UI (only a
    # configured yes/no). cookies_file is defined above (Phase 0).
    # Browser to read cookies from (yt-dlp --cookies-from-browser), e.g. "chrome".
    # Accept both COOKIES_FROM_BROWSER and YTDLP_COOKIES_FROM_BROWSER.
    cookies_from_browser: str = Field(
        default="",
        validation_alias=AliasChoices("cookies_from_browser", "ytdlp_cookies_from_browser"),
    )
    # YouTube PO token (yt-dlp --extractor-args youtube:po_token=...). Secret.
    youtube_po_token: str = ""
    # YouTube visitor data (pairs with the PO token). Secret.
    youtube_visitor_data: str = ""
    # Raw extra extractor-args passthrough (e.g. "youtube:player_client=web").
    # May contain non-secret tuning; po_token inside it is still masked in logs.
    ytdlp_extractor_args: str = ""

    @property
    def effective_subtitles_sub_langs(self) -> str:
        return (self.subtitles_refresh_sub_langs or self.default_sub_langs or "ja,en").strip()

    @property
    def cookies_configured(self) -> bool:
        from pathlib import Path as _P

        cf = (self.cookies_file or "").strip()
        return bool((cf and _P(cf).is_file()) or (self.cookies_from_browser or "").strip())

    @property
    def po_token_configured(self) -> bool:
        return bool((self.youtube_po_token or "").strip())

    @property
    def visitor_data_configured(self) -> bool:
        return bool((self.youtube_visitor_data or "").strip())

    @property
    def browser_cookies_configured(self) -> bool:
        return bool((self.cookies_from_browser or "").strip())

    def cookies_file_status(self) -> dict:
        """Cookie-file status for diagnostics — NEVER includes the path/contents.

        ``configured`` means a cookie source is actually USABLE (browser cookies,
        or a cookies file that exists) — consistent with ``cookies_configured``.
        ``file_configured`` only reflects that ``COOKIES_FILE`` is set, so the
        doctor can flag "path set but file missing".
        """
        import os
        from datetime import datetime, timezone
        from pathlib import Path as _P

        cf = (self.cookies_file or "").strip()
        out = {
            "configured": self.browser_cookies_configured,
            "file_configured": bool(cf),
            "file_exists": False,
            "readable": False,
            "last_modified": None,
        }
        if cf:
            p = _P(cf)
            try:
                if p.is_file():
                    out["file_exists"] = True
                    out["readable"] = os.access(cf, os.R_OK)
                    out["configured"] = True
                    out["last_modified"] = (
                        datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                        .replace(tzinfo=None)
                        .isoformat()
                    )
            except OSError:
                pass
        return out

    # ---- App ----
    log_level: str = "INFO"

    # ---- Production access control / auth (Phase 9C) ----
    # Deployment environment. "production" makes production-check enforce secure
    # auth/CORS/cookie settings (FAIL otherwise). Default development => the app
    # runs unauthenticated for local use (auth_mode=disabled).
    app_env: str = "development"  # development | production
    # disabled: no auth (dev only). local: in-app admin login (scrypt hash +
    # signed session cookie). trusted_proxy: trust an authenticating reverse proxy
    # (e.g. Cloudflare Access) header, but ONLY from a trusted proxy IP.
    auth_mode: str = "disabled"  # disabled | local | trusted_proxy
    # Secret files (values are read from disk; NEVER logged / returned by the API).
    session_secret_file: str = ""       # HMAC key for signed sessions
    admin_password_hash_file: str = ""  # scrypt PHC-style hash for local login
    # Session cookie policy. Secure MUST be true in production (HTTPS).
    session_cookie_secure: bool = True
    session_cookie_samesite: str = "strict"  # strict | lax
    session_cookie_name: str = "ytarch_session"
    csrf_cookie_name: str = "ytarch_csrf"
    session_max_age_seconds: int = 28800  # 8h
    # trusted_proxy mode: only accept the auth header from these proxy source CIDRs,
    # and only for the allow-listed admin emails. Direct clients cannot spoof it.
    trusted_proxy_auth_header: str = "CF-Access-Authenticated-User-Email"
    trusted_proxy_cidrs: str = ""   # comma-separated, e.g. "173.245.48.0/20,103.21.244.0/22"
    allowed_admin_emails: str = ""  # comma-separated allow-list
    # Trust X-Forwarded-Proto/For only when the direct peer is a trusted proxy.
    trust_forwarded_headers: bool = False
    # Lightweight login rate limit (per client IP).
    login_rate_limit_max_attempts: int = 5
    login_rate_limit_window_seconds: int = 300

    # ---- Ingress / release hardening (Phase 9D) ----
    # Host header allow-list (comma). Empty = allow any (dev). production-check
    # FAILs if empty in production. No wildcard entries.
    allowed_hosts: str = ""
    # CSRF trusted browser origins (comma, scheme+host) — SEPARATE from CORS
    # origins. Wildcard forbidden. Empty falls back to same-origin only.
    csrf_trusted_origins: str = ""
    # Security response headers (CSP / nosniff / frame / referrer / permissions).
    security_headers_enabled: bool = True
    # HSTS is added ONLY in production (HTTPS is assumed terminated at the proxy);
    # never in development over HTTP.
    hsts_max_age_seconds: int = 31536000
    # Login rate-limit backend. "auto" uses Redis when reachable, else in-memory
    # (dev) / fail-closed (prod). Keys are HMAC-anonymised (no raw IP stored).
    login_rate_limit_backend: str = "auto"  # auto | redis | memory
    # Optional PREVIOUS session secret (verification-only) for zero-downtime
    # session-secret rotation. New sessions are always signed with the current one.
    session_secret_previous_file: str = ""
    # Backup freshness marker (touched by the backup script). release-check WARNs
    # when missing or older than the max age.
    backup_marker_file: str = ""
    backup_max_age_hours: int = 168  # 7 days

    # ---- Backup integrity / DR acceptance (Phase 9F) ----
    # Small JSON summary of the latest backup manifest (basename/sha256/size/
    # schema_head only — no host paths). Written by `archiver backup write-manifest
    # --summary-file`, read by release-check + the backup-readiness API.
    backup_manifest_summary_file: str = ""
    # Marker touched by `archiver backup verify-manifest --write-marker` on a
    # successful integrity verification of the newest backup artifact.
    backup_verified_marker_file: str = ""
    backup_verify_max_age_hours: int = 336  # 14 days
    # Marker touched by scripts/restore-rehearsal.sh after a PASSING isolated
    # restore rehearsal (never by the rehearsal environment itself).
    restore_rehearsal_marker_file: str = ""
    restore_rehearsal_max_age_days: int = 90
    # Optional HMAC key for the backup manifest's canonical integrity hash
    # (Phase 9F.1). Unset -> plain SHA-256 (development). Value never logged.
    backup_manifest_hmac_key_file: str = ""

    # ---- Release candidate / supply chain (Phase 10A) ----
    # Small release-manifest summary (basenames/hashes/counts) written by
    # scripts/build-release.sh, read by release-check + the release-readiness API.
    release_manifest_summary_file: str = ""
    # Optional HMAC key for the release manifest integrity hash (value never
    # logged). Unset -> plain SHA-256 (development).
    release_manifest_hmac_key_file: str = ""
    # Vulnerability policy: WARN or FAIL when the scanner is unavailable / a
    # release build has no scan; production defaults stricter (see release-check).
    release_scanner_unavailable_policy: str = "warn"  # warn | fail
    # Max critical/high vulnerabilities tolerated before release-check FAILs.
    release_max_critical_vulnerabilities: int = 0
    # Max age of the vulnerability DB (days) before release-check flags it stale.
    release_vuln_db_max_age_days: int = 7
    # Phase 10B: vulnerability triage / remediation gating.
    # Repo-tracked exception file (operator-approved, time-bound). Empty template.
    vulnerability_exceptions_file: str = "vulnerability-exceptions.yml"
    # HIGH-severity policy: warn (know the count, reduce fixable) | fail.
    release_high_vuln_policy: str = "warn"  # warn | fail
    release_max_high_vulnerabilities: int = 0  # only enforced when policy == fail
    # Scanner provenance: production requires a verified scanner. When a digest
    # can't be resolved offline, an operator marks it verified out-of-band.
    release_require_scanner_provenance: bool = True
    # Phase 10B.3: advisory exception PROPOSALS (never active) + the machine
    # decision dossier release-check reads. Proposals are NOT enforced; only
    # vulnerability_exceptions_file suppresses findings.
    vulnerability_exception_proposals_file: str = "vulnerability-exception-proposals.yml"
    vulnerability_decision_dossier_file: str = "docs/vulnerability-decision-dossier.json"
    # Public HTTPS URL the admin UI is served on (e.g. https://archiver.example.com).
    # HTTPS readiness is judged from THIS + the proxy config, NOT from the HSTS header.
    public_base_url: str = ""

    # ---- Audit trail / observability (Phase 9E) ----
    audit_enabled: bool = True
    # HMAC key for the tamper-evident audit hash chain (file; value never logged).
    audit_hmac_key_file: str = ""
    # Legacy alias (Phase 9E) kept for compatibility; use audit_pseudonym_key_file.
    audit_pseudonymize_key_file: str = ""
    audit_retention_days: int = 365
    audit_security_retention_days: int = 730
    audit_max_export_events: int = 100000

    # ---- Audit signing lifecycle (Phase 9E.1) ----
    # Short id for the CURRENT signing key (never the key value/path). Required in
    # production when a signing key is set.
    audit_hmac_key_id: str = ""
    # PREVIOUS keys — verification only (never used to sign new events). Comma
    # lists; the file count and id count MUST match; ids must be unique.
    audit_hmac_previous_key_files: str = ""
    audit_hmac_previous_key_ids: str = ""
    # Pseudonymisation key — SEPARATE from the signing key so rotating the signing
    # key does not change actor/client pseudonyms. Value never logged.
    audit_pseudonym_key_file: str = ""
    # Policy: allow a legacy unsigned prefix (bounded by an explicit checkpoint).
    # Production default false.
    audit_allow_legacy_unsigned_prefix: bool = False
    # Metrics endpoint: require auth (never expose publicly in production).
    metrics_require_auth: bool = True
    # Emit structured JSON logs (with a shared redaction filter).
    structured_logging: bool = False

    # ---- Web UI / CORS (Phase 5A) ----
    # Built frontend directory. Empty -> <repo>/frontend/dist (Docker: /app/frontend/dist).
    web_ui_dir: str = ""
    # Serve the built SPA from FastAPI when present.
    web_ui_enabled: bool = True
    # CORS allow-origins (comma separated). "*" = allow all (this is a local admin
    # tool with no auth/cookies on the API). Set explicit origins to restrict.
    cors_allow_origins: str = "*"

    # ---- Host bind (Phase 12A) ----
    # The host interface docker-compose publishes the web port on. Compose reads
    # ${WEB_BIND_HOST:-127.0.0.1}; when set in .env it is also passed into the
    # container (env_file) so the app can warn about its own exposure. The default
    # is loopback — safe for local single-user. Set 0.0.0.0 ONLY with auth enabled.
    web_bind_host: str = "127.0.0.1"
    web_port: int = 8000

    @property
    def web_bind_is_all_interfaces(self) -> bool:
        """True when the web port is (or would be) published on every interface."""
        return (self.web_bind_host or "").strip() in ("", "0.0.0.0", "::")

    @property
    def cors_origins_list(self) -> list[str]:
        raw = (self.cors_allow_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def cors_is_wildcard(self) -> bool:
        return self.cors_origins_list == ["*"]

    # ---- Auth helpers (Phase 9C) — never expose secret VALUES ----
    @property
    def is_production(self) -> bool:
        return (self.app_env or "").strip().lower() == "production"

    @property
    def auth_enabled(self) -> bool:
        return (self.auth_mode or "disabled").strip().lower() in {"local", "trusted_proxy"}

    def _read_secret_file(self, path: str) -> str | None:
        """Return the stripped contents of a secret file, or None. Never logged."""
        from pathlib import Path as _P

        p = (path or "").strip()
        if not p:
            return None
        try:
            fp = _P(p)
            if fp.is_file():
                val = fp.read_text("utf-8").strip()
                return val or None
        except OSError:
            return None
        return None

    def session_secret(self) -> str | None:
        return self._read_secret_file(self.session_secret_file)

    def admin_password_hash(self) -> str | None:
        return self._read_secret_file(self.admin_password_hash_file)

    @property
    def session_secret_configured(self) -> bool:
        return self.session_secret() is not None

    @property
    def admin_password_hash_configured(self) -> bool:
        return self.admin_password_hash() is not None

    @property
    def effective_session_cookie_secure(self) -> bool:
        # Reflect the configured setting (default True). production-check FAILs if
        # this is False in production so an insecure cookie is caught, not hidden.
        return bool(self.session_cookie_secure)

    @property
    def trusted_proxy_cidr_list(self) -> list[str]:
        return [c.strip() for c in (self.trusted_proxy_cidrs or "").split(",") if c.strip()]

    @property
    def allowed_admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in (self.allowed_admin_emails or "").split(",") if e.strip()]

    # ---- Ingress / release helpers (Phase 9D) ----
    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip().lower() for h in (self.allowed_hosts or "").split(",") if h.strip()]

    @property
    def csrf_trusted_origins_list(self) -> list[str]:
        return [o.strip() for o in (self.csrf_trusted_origins or "").split(",") if o.strip()]

    @property
    def effective_hsts(self) -> bool:
        # HSTS only in production (HTTPS assumed at the proxy); never dev/HTTP.
        return bool(self.is_production and self.security_headers_enabled)

    def session_secret_previous(self) -> str | None:
        return self._read_secret_file(self.session_secret_previous_file)

    def session_secrets(self) -> list[str]:
        """Verification secrets: current first, then previous (rotation window)."""
        out: list[str] = []
        cur = self.session_secret()
        if cur:
            out.append(cur)
        prev = self.session_secret_previous()
        if prev and prev != cur:
            out.append(prev)
        return out

    # ---- Audit / observability helpers (Phase 9E) — never expose secret VALUES ----
    @property
    def public_base_url_is_https(self) -> bool:
        return (self.public_base_url or "").strip().lower().startswith("https://")

    def audit_hmac_key(self) -> str | None:
        return self._read_secret_file(self.audit_hmac_key_file)

    def backup_manifest_hmac_key(self) -> str | None:
        """Phase 9F.1: optional key for signing backup manifests (never logged)."""
        return self._read_secret_file(self.backup_manifest_hmac_key_file)

    def release_manifest_hmac_key(self) -> str | None:
        """Phase 10A: optional key for signing release manifests (never logged)."""
        return self._read_secret_file(self.release_manifest_hmac_key_file)

    @property
    def audit_hmac_key_configured(self) -> bool:
        return self.audit_hmac_key() is not None

    def audit_current_signing(self) -> tuple[str | None, str | None]:
        """(key_id, key_value) for the CURRENT signing key, or (None, None)."""
        key = self.audit_hmac_key()
        if not key:
            return (None, None)
        return ((self.audit_hmac_key_id or "").strip() or "unspecified", key)

    def audit_previous_keys(self) -> dict[str, str]:
        """{key_id: key_value} for verification-only previous keys (readable ones)."""
        files = [f.strip() for f in (self.audit_hmac_previous_key_files or "").split(",") if f.strip()]
        ids = [i.strip() for i in (self.audit_hmac_previous_key_ids or "").split(",") if i.strip()]
        out: dict[str, str] = {}
        for kid, f in zip(ids, files):
            v = self._read_secret_file(f)
            if v:
                out[kid] = v
        return out

    def audit_verification_keys(self) -> dict[str, str]:
        """Registry {key_id: key} used to VERIFY (current + previous)."""
        reg = dict(self.audit_previous_keys())
        kid, key = self.audit_current_signing()
        if kid and key:
            reg[kid] = key
        return reg

    def audit_key_config_error(self) -> str | None:
        """Reason string if the key registry is misconfigured, else None."""
        files = [f.strip() for f in (self.audit_hmac_previous_key_files or "").split(",") if f.strip()]
        ids = [i.strip() for i in (self.audit_hmac_previous_key_ids or "").split(",") if i.strip()]
        if len(files) != len(ids):
            return "previous key file/id count mismatch"
        all_ids = ids + ([self.audit_hmac_key_id.strip()] if (self.audit_hmac_key_id or "").strip() else [])
        if len(all_ids) != len(set(all_ids)):
            return "duplicate audit key id"
        return None

    @property
    def audit_pseudonym_key_configured(self) -> bool:
        return self._read_secret_file(self.audit_pseudonym_key_file) is not None

    def audit_pseudonymize_key(self) -> str:
        """Key for pseudonymising actor/client ids — SEPARATE from the signing key
        so rotating the signing key does not change pseudonyms. Falls back to the
        legacy alias, then a fixed dev constant (NEVER the signing key)."""
        return (self._read_secret_file(self.audit_pseudonym_key_file)
                or self._read_secret_file(self.audit_pseudonymize_key_file)
                or "ytarch-pseudonym-dev")

    # ---- YouTube Data API OAuth (Phase 6B) — DEFAULT DISABLED ----
    # When disabled (default) the app starts and runs fully without any OAuth
    # config. Secrets (client_secret / token) live as files under /config or
    # /secrets and are NEVER returned by the API or logged.
    youtube_api_enabled: bool = False
    # OAuth installed-app client secret JSON (downloaded from Google Cloud).
    youtube_oauth_client_secret_file: str = ""
    # Stored OAuth token (created by the authorize flow). Default under CONFIG_ROOT.
    youtube_oauth_token_file: str = ""
    # Preferred liked-fetch method: "videos" (videos.list myRating=like),
    # "playlist" (relatedPlaylists.likes), or "auto" (videos -> playlist fallback).
    youtube_api_liked_method: str = "auto"

    @property
    def youtube_token_path(self) -> Path:
        if self.youtube_oauth_token_file:
            return Path(self.youtube_oauth_token_file)
        return self.config_root / "youtube_oauth_token.json"

    @property
    def youtube_client_secret_path(self) -> Path | None:
        return Path(self.youtube_oauth_client_secret_file) if self.youtube_oauth_client_secret_file else None

    @property
    def youtube_api_configured(self) -> bool:
        """True if OAuth is enabled AND a client secret + token file are present."""
        if not self.youtube_api_enabled:
            return False
        cs = self.youtube_client_secret_path
        return bool(cs and cs.is_file() and self.youtube_token_path.is_file())

    # ----- Derived helpers -----
    @property
    def effective_remote_components(self) -> str | None:
        """YouTube JS-challenge remote components, always on unless explicitly
        disabled. An empty/unset value falls back to the default (requirement 6:
        must always be added), so a stale ``.env`` with ``YTDLP_REMOTE_COMPONENTS=``
        does not silently drop it. Disable with ``none``/``off``/``disabled``.
        """
        rc = (self.ytdlp_remote_components or "").strip()
        if rc.lower() in {"none", "off", "false", "0", "disabled", "no"}:
            return None
        return rc or "ejs:github"

    @property
    def youtube_root(self) -> Path:
        return self.archive_root / "youtube"

    def ensure_dirs(self) -> None:
        """Create the runtime directories if they do not already exist."""
        for p in (
            self.archive_root,
            self.config_root,
            self.log_root,
            self.takeout_import_root,
            self.youtube_root,
            self.youtube_root / "videos",
            self.youtube_root / "audio",
            self.youtube_root / "playlists",
            self.youtube_root / "channels",
            self.youtube_root / "metadata_snapshots",
            self.youtube_root / "archive",
            self.log_root / "jobs",
        ):
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                # Roots may be read-only mounts in some setups; ignore here and
                # let the actual write operation surface a clear error later.
                pass

    def apply_yaml_overlay(self) -> "Settings":
        """Overlay non-secret values from ``CONFIG_ROOT/archiver.yaml`` if present."""
        yaml_path = self.config_root / "archiver.yaml"
        if not yaml_path.is_file():
            return self
        try:
            data: dict[str, Any] = yaml.safe_load(yaml_path.read_text("utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return self
        # Secrets are intentionally excluded from YAML overlay.
        forbidden = {"cookies_file", "database_url", "redis_url"}
        updates = {
            k: v
            for k, v in data.items()
            if k in self.model_fields and k not in forbidden
        }
        if not updates:
            return self
        return self.model_copy(update=updates)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached, fully-resolved settings instance."""
    settings = Settings().apply_yaml_overlay()
    return settings
