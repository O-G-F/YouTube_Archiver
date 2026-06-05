"""Shared yt-dlp command construction.

This is the single source of truth for turning (settings, profile, url) into a
yt-dlp argv. Both the worker (real execution) and the dry-run / build-command
features use it, so a dry-run always matches what would actually run.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings
from app.services import storage
from app.services.profiles import (
    BuildContext,
    ProfileSpec,
    build_ytdlp_args,
    get_profile_spec,
)
from app.services.urls import normalize_url
from app.services.ytdlp import build_command, redact_args


def external_ctx(settings: Settings) -> dict:
    """Cookies / ffmpeg / deno / remote-components context (shared by all jobs).

    ``remote_components`` is always on for YouTube unless explicitly disabled
    (robust to a stale ``.env`` with a blank value).
    """
    cookies = (
        settings.cookies_file
        if settings.cookies_file and Path(settings.cookies_file).is_file()
        else None
    )
    return {
        "cookies_file": cookies,
        "cookies_from_browser": (settings.cookies_from_browser or "").strip() or None,
        "po_token": (settings.youtube_po_token or "").strip() or None,
        "ffmpeg_location": settings.ffmpeg_location or None,
        "deno_path": settings.deno_path or None,
        "remote_components": settings.effective_remote_components,
    }


def download_build_context(
    settings: Settings,
    spec: ProfileSpec,
    *,
    youtube_video_id: str,
    channel_id: str | None,
    no_playlist: bool,
) -> BuildContext:
    """BuildContext for a single-video download (used by the worker AND dry-run)."""
    media_mode = spec.media_mode
    out_tpl = storage.video_output_template(
        settings, media_mode, channel_id, youtube_video_id
    )
    archive = (
        str(storage.download_archive_path(settings))
        if media_mode in ("video", "audio")
        else None
    )
    return BuildContext(
        output_template=out_tpl,
        download_archive=archive,
        no_playlist=no_playlist,
        default_sub_langs=settings.default_sub_langs,
        archive_sub_langs=settings.archive_sub_langs,
        max_comments=settings.ytdlp_max_comments,
        retry_sleep=settings.ytdlp_retry_backoff_seconds,
        **external_ctx(settings),
    )


def dry_run_command(
    session: Session, settings: Settings, profile_name: str, url: str
) -> dict:
    """Build the yt-dlp command that *would* run, without executing it.

    Returns the (cookie/secret-masked) argv and a display string. Raises
    ``UrlError`` for bad URLs and ``KeyError`` for unknown profiles.
    """
    parsed = normalize_url(url)
    spec = get_profile_spec(session, profile_name)
    youtube_video_id = parsed.video_id or "VIDEO_ID"
    no_playlist = parsed.kind == "video"
    ctx = download_build_context(
        settings,
        spec,
        youtube_video_id=youtube_video_id,
        channel_id=None,  # dry-run: real channel_id is resolved at run time
        no_playlist=no_playlist,
    )
    argv = build_ytdlp_args(spec, ctx)
    cmd = build_command(settings, argv, url=parsed.canonical_url)
    masked = redact_args(cmd, mask_cookies=True)
    return {
        "profile": profile_name,
        "url": parsed.canonical_url,
        "kind": parsed.kind,
        "argv": masked,
        "command": shlex.join(masked),
        "note": "channel_id/title in -o are placeholders; resolved at run time.",
    }
