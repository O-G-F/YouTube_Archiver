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
    # Profile used for a body archive (downloads the video BODY).
    liked_archive_default_profile: str = "video_compressed_1080p"
    # Extra per-job sleep applied to liked-archive download jobs (throttling).
    liked_archive_job_delay_seconds: float = 0.0
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
    def effective_scheduler_liked_archive_profile(self) -> str:
        return (self.scheduler_liked_archive_profile or "").strip() or self.liked_archive_default_profile

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

    # ---- Web UI / CORS (Phase 5A) ----
    # Built frontend directory. Empty -> <repo>/frontend/dist (Docker: /app/frontend/dist).
    web_ui_dir: str = ""
    # Serve the built SPA from FastAPI when present.
    web_ui_enabled: bool = True
    # CORS allow-origins (comma separated). "*" = allow all (this is a local admin
    # tool with no auth/cookies on the API). Set explicit origins to restrict.
    cors_allow_origins: str = "*"

    @property
    def cors_origins_list(self) -> list[str]:
        raw = (self.cors_allow_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

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
