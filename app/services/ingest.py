"""Ingest yt-dlp output into the database.

Two responsibilities:
  1. Upsert a :class:`Video` row from an info.json dict.
  2. Scan a per-video output directory and register every produced file
     (media, thumbnail, info.json, description, subtitles, live chat, links)
     as relative paths under ARCHIVE_ROOT.

Also upserts comments parsed from an info.json ``comments`` array.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Comment, MediaFile, Subtitle, Video
from app.models import utcnow
from app.services import storage

_VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".mov", ".avi", ".ts", ".flv"}
_AUDIO_EXTS = {".flac", ".opus", ".m4a", ".mp3", ".wav", ".ogg", ".aac"}
_THUMB_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_SUB_EXTS = {".vtt", ".srt", ".ass", ".ssa"}
_LINK_EXTS = {".url", ".webloc", ".desktop", ".lnk"}


def _dt_from_unix(ts: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def upsert_video_from_info(
    session: Session,
    info: dict,
    settings: Settings,
    *,
    is_short: bool | None = None,
    is_live: bool | None = None,
) -> Video:
    """Create or update a Video row from a yt-dlp info dict."""
    youtube_video_id = info.get("id")
    if not youtube_video_id:
        raise ValueError("info.json has no 'id'")

    video = session.scalar(
        select(Video).where(Video.youtube_video_id == youtube_video_id)
    )
    if video is None:
        video = Video(youtube_video_id=youtube_video_id, first_seen_at=utcnow())
        session.add(video)

    video.title = info.get("title") or video.title
    video.channel_id = info.get("channel_id") or info.get("uploader_id") or video.channel_id
    video.channel_title = info.get("channel") or info.get("uploader") or video.channel_title
    video.url = info.get("webpage_url") or video.url
    video.duration = info.get("duration") if info.get("duration") is not None else video.duration
    video.upload_date = info.get("upload_date") or video.upload_date
    if info.get("description") is not None:
        video.description = info.get("description")
    video.availability = info.get("availability") or video.availability

    live_status = info.get("live_status")
    resolved_is_live = (
        is_live
        if is_live is not None
        else bool(info.get("is_live") or live_status in {"is_live", "is_upcoming"})
    )
    video.is_live = bool(resolved_is_live)
    if is_short is not None:
        video.is_short = bool(is_short)

    session.flush()
    return video


def register_outputs(
    session: Session,
    video: Video,
    out_dir: Path,
    profile_name: str,
    settings: Settings,
) -> dict[str, int]:
    """Scan ``out_dir`` and (idempotently) register all produced files."""
    counts = {"media": 0, "subtitle": 0, "thumbnail": 0, "info_json": 0, "other": 0}
    if not out_dir.exists():
        return counts

    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        suffixes = path.suffixes  # e.g. ['.en', '.vtt'] or ['.info', '.json']
        rel = storage.to_relative(settings, path)

        # info.json
        if name.endswith(".info.json"):
            _upsert_media(session, video, "info_json", rel, profile_name)
            video.raw_info_json_path = rel
            counts["info_json"] += 1
            continue
        # live chat
        if name.endswith(".live_chat.json"):
            _upsert_media(session, video, "live_chat", rel, profile_name)
            counts["other"] += 1
            continue
        # description
        if path.suffix == ".description":
            _upsert_media(session, video, "description", rel, profile_name)
            counts["other"] += 1
            continue

        ext = path.suffix.lower()
        if ext in _SUB_EXTS:
            lang = suffixes[-2].lstrip(".") if len(suffixes) >= 2 else None
            _upsert_subtitle(session, video, lang, rel, ext.lstrip("."))
            counts["subtitle"] += 1
        elif ext in _THUMB_EXTS:
            mf = _upsert_media(session, video, "thumbnail", rel, profile_name)
            mf.container = ext.lstrip(".")
            if not video.thumbnail_path:
                video.thumbnail_path = rel
            counts["thumbnail"] += 1
        elif ext in _VIDEO_EXTS:
            mf = _upsert_media(session, video, "video", rel, profile_name)
            _apply_media_meta(mf, path, video)
            counts["media"] += 1
        elif ext in _AUDIO_EXTS:
            mf = _upsert_media(session, video, "audio", rel, profile_name)
            mf.container = ext.lstrip(".")
            mf.filesize = path.stat().st_size
            counts["media"] += 1
        elif ext in _LINK_EXTS:
            _upsert_media(session, video, "link", rel, profile_name)
            counts["other"] += 1
        else:
            counts["other"] += 1

    session.flush()
    return counts


def _apply_media_meta(mf: MediaFile, path: Path, video: Video) -> None:
    mf.container = path.suffix.lstrip(".")
    try:
        mf.filesize = path.stat().st_size
    except OSError:
        pass


def _upsert_media(
    session: Session, video: Video, media_type: str, rel_path: str, profile: str
) -> MediaFile:
    mf = session.scalar(
        select(MediaFile).where(
            MediaFile.video_id == video.id, MediaFile.path == rel_path
        )
    )
    if mf is None:
        mf = MediaFile(video_id=video.id, path=rel_path)
        session.add(mf)
    mf.media_type = media_type
    mf.profile = profile
    return mf


def _upsert_subtitle(
    session: Session, video: Video, lang: str | None, rel_path: str, fmt: str
) -> Subtitle:
    sub = session.scalar(
        select(Subtitle).where(
            Subtitle.video_id == video.id, Subtitle.path == rel_path
        )
    )
    if sub is None:
        sub = Subtitle(video_id=video.id, path=rel_path)
        session.add(sub)
    sub.language = lang
    sub.format = fmt
    sub.is_auto = bool(lang and "auto" in (lang or "").lower())
    return sub


def ingest_comments_from_info(
    session: Session,
    video: Video,
    info: dict,
    *,
    source: str = "yt-dlp",
    snapshot_id: int | None = None,
) -> dict[str, int]:
    """Upsert comments from an info.json ``comments`` array.

    Returns counts of fetched/new/updated. Deletion detection is deliberately
    left to Phase 4 (the ``is_deleted_or_missing`` column exists for it).
    """
    comments = info.get("comments") or []
    summary = {"fetched": len(comments), "new": 0, "updated": 0}
    if not comments:
        return summary

    existing = {
        c.comment_id: c
        for c in session.scalars(
            select(Comment).where(Comment.video_id == video.id)
        )
    }
    now = utcnow()
    for c in comments:
        cid = c.get("id")
        if not cid:
            continue
        parent = c.get("parent")
        parent_id = None if parent in (None, "root") else parent
        row = existing.get(cid)
        if row is None:
            row = Comment(video_id=video.id, comment_id=cid)
            session.add(row)
            summary["new"] += 1
        else:
            summary["updated"] += 1
        row.parent_comment_id = parent_id
        row.author_name = c.get("author")
        row.author_channel_id = c.get("author_id")
        row.text = c.get("text")
        row.like_count = c.get("like_count")
        row.published_at = _dt_from_unix(c.get("timestamp"))
        row.fetched_at = now
        row.is_deleted_or_missing = False
        row.source = source
        row.snapshot_id = snapshot_id
        row.raw_json = c

    session.flush()
    return summary
