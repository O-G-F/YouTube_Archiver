"""Read-only settings endpoint for the admin UI (Phase 5A).

CRITICAL: secrets are NEVER returned. The cookies file path is not exposed (only
whether one is configured); database/redis URLs are credential-masked.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import get_db
from app.config import get_settings
from app.models import DownloadProfile
from app.schemas import ProfileOut, SettingsItem, SettingsOut

router = APIRouter(tags=["settings"])

# Mask the password in "scheme://user:pass@host" (user may be empty, e.g. redis).
_URL_CRED_RE = re.compile(r"(://[^:/@\s]*:)([^@/\s]+)(@)")


def _mask_url(url: str | None) -> str:
    """Mask the password component of a connection URL (user:pass@host)."""
    if not url:
        return ""
    return _URL_CRED_RE.sub(r"\1***\3", url)


@router.get("/api/settings", response_model=SettingsOut)
def get_settings_view(db: Session = Depends(get_db)) -> SettingsOut:
    s = get_settings()
    _cookie_status = s.cookies_file_status()  # configured/file_exists/readable (NO path)
    items: list[SettingsItem] = [
        SettingsItem(key="version", value=__version__),
        SettingsItem(key="default_profile", value=s.default_profile),
        SettingsItem(key="default_sub_langs", value=s.default_sub_langs),
        SettingsItem(key="archive_sub_langs", value=s.archive_sub_langs),
        SettingsItem(
            key="remote_components",
            value=str(s.effective_remote_components),
            note="YouTube JS-challenge solver components (not a secret)",
        ),
        SettingsItem(key="archive_root", value=str(s.archive_root)),
        SettingsItem(key="config_root", value=str(s.config_root)),
        SettingsItem(key="log_root", value=str(s.log_root)),
        SettingsItem(key="takeout_import_root", value=str(s.takeout_import_root)),
        SettingsItem(key="obsidian_export_root", value=str(s.obsidian_export_root)),
        SettingsItem(key="database_url", value=_mask_url(s.database_url), note="credentials masked"),
        SettingsItem(key="redis_url", value=_mask_url(s.redis_url), note="credentials masked"),
        SettingsItem(key="rq_queue", value=s.rq_queue),
        SettingsItem(
            key="cookies_configured",
            value="yes" if s.cookies_configured else "no",
            note="cookies.txt / browser cookies (path hidden)",
        ),
        SettingsItem(
            key="cookies_file_exists",
            value="yes" if _cookie_status["file_exists"] else "no",
            note="COOKIES_FILE found on disk (path hidden)",
        ),
        SettingsItem(
            key="cookies_file_readable",
            value="yes" if _cookie_status["readable"] else "no",
        ),
        SettingsItem(
            key="browser_cookies_configured",
            value="yes" if s.browser_cookies_configured else "no",
            note="--cookies-from-browser (value hidden)",
        ),
        SettingsItem(
            key="po_token_configured",
            value="yes" if s.po_token_configured else "no",
            note="YouTube PO token (value hidden)",
        ),
        SettingsItem(
            key="visitor_data_configured",
            value="yes" if s.visitor_data_configured else "no",
            note="YouTube visitor data (value hidden)",
        ),
        SettingsItem(key="ytdlp_binary", value=s.ytdlp_binary),
        SettingsItem(key="ffmpeg_location", value=s.ffmpeg_location or "(PATH)"),
        SettingsItem(key="deno_path", value=s.deno_path or "(PATH)"),
        SettingsItem(key="ytdlp_max_comments", value=str(s.ytdlp_max_comments)),
        SettingsItem(key="comment_refresh_max_comments", value=str(s.comment_refresh_max_comments)),
        SettingsItem(key="expand_max_items", value=str(s.expand_max_items)),
        SettingsItem(key="ytdlp_timeout", value=str(s.ytdlp_timeout)),
        SettingsItem(key="scheduler_enabled", value=str(s.scheduler_enabled)),
        SettingsItem(key="scheduler_interval_seconds", value=str(s.scheduler_interval_seconds)),
        SettingsItem(key="scheduler_comments_enabled", value=str(s.scheduler_comments_enabled)),
        SettingsItem(
            key="scheduler_comments_limit_per_run", value=str(s.scheduler_comments_limit_per_run)
        ),
        SettingsItem(
            key="comments_refresh_job_delay_seconds",
            value=str(s.comments_refresh_job_delay_seconds),
        ),
        SettingsItem(
            key="comments_refresh_retry_backoff_seconds",
            value=str(s.comments_refresh_retry_backoff_seconds),
        ),
        SettingsItem(key="comments_refresh_max_retry", value=str(s.comments_refresh_max_retry)),
        SettingsItem(key="live_chat_max_messages", value=str(s.live_chat_max_messages)),
        SettingsItem(
            key="live_chat_refresh_interval_seconds",
            value=str(s.live_chat_refresh_interval_seconds),
        ),
        SettingsItem(key="download_job_delay_seconds", value=str(s.download_job_delay_seconds)),
        SettingsItem(key="ytdlp_retry_backoff_seconds", value=str(s.ytdlp_retry_backoff_seconds)),
        SettingsItem(
            key="max_concurrent_download_jobs", value=str(s.max_concurrent_download_jobs)
        ),
        SettingsItem(key="log_level", value=s.log_level),
        # YouTube Data API OAuth (Phase 6B) — never expose paths/tokens.
        SettingsItem(key="youtube_api_enabled", value=str(s.youtube_api_enabled)),
        SettingsItem(
            key="youtube_api_configured",
            value="yes" if s.youtube_api_configured else "no",
            note="OAuth client secret + token present (paths hidden)",
        ),
        SettingsItem(key="youtube_api_liked_method", value=s.youtube_api_liked_method),
    ]
    profiles = [
        ProfileOut.model_validate(p)
        for p in db.scalars(select(DownloadProfile).order_by(DownloadProfile.name))
    ]
    return SettingsOut(items=items, profiles=profiles)
