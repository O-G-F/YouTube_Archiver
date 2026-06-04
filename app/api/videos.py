"""Video listing / detail / media-streaming endpoints (admin + player UI)."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import Settings, get_settings
from app.models import Collection, CollectionItem, Comment, Job, MediaFile, Subtitle, Video
from app.schemas import (
    ChannelOut,
    CollectionOut,
    JobOut,
    MediaFileOut,
    RelatedVideosOut,
    VideoDetailOut,
    VideoListItemOut,
)

router = APIRouter(prefix="/api/videos", tags=["videos"])

_BODY_TYPES = ("video", "audio")

# Content-Type overrides for in-browser playback (mimetypes misses some).
_CT_OVERRIDES = {
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".m4a": "audio/mp4",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

_SORTS = {
    "added_desc": (Video.first_seen_at.desc(), Video.id.desc()),
    "added_asc": (Video.first_seen_at.asc(), Video.id.asc()),
    "upload_desc": (Video.upload_date.desc().nullslast(), Video.id.desc()),
    "upload_asc": (Video.upload_date.asc().nullsfirst(), Video.id.asc()),
    "title": (Video.title.asc().nullslast(), Video.id.asc()),
}


def _media_content_type(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in _CT_OVERRIDES:
        return _CT_OVERRIDES[ext]
    guess, _ = mimetypes.guess_type(str(path))
    return guess or "application/octet-stream"


def _safe_archive_path(settings: Settings, rel: str) -> Path | None:
    """Resolve a DB-stored relative path under ARCHIVE_ROOT (traversal-guarded)."""
    root = settings.archive_root.resolve()
    abs_path = (root / rel).resolve()
    try:
        abs_path.relative_to(root)
    except ValueError:
        return None
    return abs_path if abs_path.is_file() else None


def _decorate(db: Session, videos: list[Video]) -> list[VideoListItemOut]:
    """Attach media-body counts + thumbnail flags to a page of videos."""
    ids = [v.id for v in videos]
    body: dict[int, int] = {}
    thumb: set[int] = set()
    if ids:
        rows = db.execute(
            select(MediaFile.video_id, MediaFile.media_type, func.count(MediaFile.id))
            .where(MediaFile.video_id.in_(ids))
            .group_by(MediaFile.video_id, MediaFile.media_type)
        ).all()
        for vid, mtype, n in rows:
            if mtype in _BODY_TYPES:
                body[vid] = body.get(vid, 0) + int(n)
            elif mtype == "thumbnail":
                thumb.add(vid)
    out: list[VideoListItemOut] = []
    for v in videos:
        item = VideoListItemOut.model_validate(v)
        item.media_files_count = body.get(v.id, 0)
        item.has_thumbnail = v.id in thumb or bool(v.thumbnail_path)
        out.append(item)
    return out


@router.get("", response_model=list[VideoListItemOut])
def list_videos(
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, description="search in title/channel/id"),
    channel_id: str | None = Query(default=None),
    comments_state: str | None = Query(default=None),
    live_chat_state: str | None = Query(default=None),
    has_media: bool | None = Query(default=None, description="filter by media-body presence"),
    sort: str = Query(default="added_desc"),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[VideoListItemOut]:
    stmt = select(Video)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Video.title.ilike(like),
                Video.channel_title.ilike(like),
                Video.youtube_video_id.ilike(like),
            )
        )
    if channel_id:
        stmt = stmt.where(Video.channel_id == channel_id)
    if comments_state:
        stmt = stmt.where(Video.comments_state == comments_state)
    if live_chat_state:
        stmt = stmt.where(Video.live_chat_state == live_chat_state)
    order = _SORTS.get(sort, _SORTS["added_desc"])
    stmt = stmt.order_by(*order).limit(limit).offset(offset)
    videos = list(db.scalars(stmt))
    decorated = _decorate(db, videos)

    if has_media is True:
        decorated = [d for d in decorated if d.media_files_count > 0]
    elif has_media is False:
        decorated = [d for d in decorated if d.media_files_count == 0]
    return decorated


@router.get("/channels", response_model=list[ChannelOut])
def list_channels(db: Session = Depends(get_db)) -> list[ChannelOut]:
    """Distinct channels (for the Videos filter dropdown)."""
    rows = db.execute(
        select(Video.channel_id, Video.channel_title, func.count(Video.id))
        .where(Video.channel_id.is_not(None))
        .group_by(Video.channel_id, Video.channel_title)
        .order_by(func.count(Video.id).desc())
    ).all()
    return [
        ChannelOut(channel_id=cid, channel_title=title, count=int(n))
        for cid, title, n in rows
    ]


@router.get("/{video_id}", response_model=VideoDetailOut)
def get_video(video_id: int, db: Session = Depends(get_db)) -> VideoDetailOut:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    subtitle_count = db.scalar(
        select(func.count(Subtitle.id)).where(Subtitle.video_id == video.id)
    )
    comment_count = db.scalar(
        select(func.count(Comment.id)).where(Comment.video_id == video.id)
    )
    detail = VideoDetailOut.model_validate(video)
    detail.media_files = [MediaFileOut.model_validate(m) for m in video.media_files]
    detail.subtitle_count = int(subtitle_count or 0)
    detail.comment_count = int(comment_count or 0)
    return detail


@router.get("/{video_id}/media/{media_file_id}")
def stream_media_file(
    video_id: int, media_file_id: int, db: Session = Depends(get_db)
) -> FileResponse:
    """Stream a media file that belongs to this video (HTTP Range supported).

    The path comes ONLY from the DB record and is hard-guarded under
    ARCHIVE_ROOT. Starlette's FileResponse honours the ``Range`` header
    (206 + Content-Range + Accept-Ranges) so the browser can seek.
    """
    mf = db.get(MediaFile, media_file_id)
    if mf is None or mf.video_id != video_id:
        raise HTTPException(status_code=404, detail="media file not found")
    abs_path = _safe_archive_path(get_settings(), mf.path)
    if abs_path is None:
        raise HTTPException(status_code=404, detail="media file missing on disk")
    return FileResponse(abs_path, media_type=_media_content_type(abs_path))


@router.get("/{video_id}/thumbnail")
def get_thumbnail(video_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """Serve the video's thumbnail (guarded under ARCHIVE_ROOT). 404 if none."""
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    settings = get_settings()
    # Prefer a thumbnail MediaFile; fall back to video.thumbnail_path.
    mf = db.scalar(
        select(MediaFile).where(
            MediaFile.video_id == video_id, MediaFile.media_type == "thumbnail"
        )
    )
    rel = mf.path if mf is not None else video.thumbnail_path
    if not rel:
        raise HTTPException(status_code=404, detail="no thumbnail")
    abs_path = _safe_archive_path(settings, rel)
    if abs_path is None:
        raise HTTPException(status_code=404, detail="thumbnail missing on disk")
    return FileResponse(abs_path, media_type=_media_content_type(abs_path))


@router.get("/{video_id}/jobs", response_model=list[JobOut])
def list_video_jobs(
    video_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, le=500),
) -> list[Job]:
    if db.get(Video, video_id) is None:
        raise HTTPException(status_code=404, detail="video not found")
    return list(
        db.scalars(
            select(Job)
            .where(Job.video_id == video_id)
            .order_by(Job.id.desc())
            .limit(limit)
        )
    )


@router.get("/{video_id}/collections", response_model=list[CollectionOut])
def list_video_collections(
    video_id: int, db: Session = Depends(get_db)
) -> list[CollectionOut]:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    coll_ids = _collection_ids_for(db, video)
    if not coll_ids:
        return []
    rows = list(
        db.scalars(select(Collection).where(Collection.id.in_(coll_ids)).order_by(Collection.id))
    )
    out: list[CollectionOut] = []
    for c in rows:
        co = CollectionOut.model_validate(c)
        co.item_count = int(
            db.scalar(
                select(func.count(CollectionItem.id)).where(
                    CollectionItem.collection_id == c.id
                )
            )
            or 0
        )
        out.append(co)
    return out


@router.get("/{video_id}/related", response_model=RelatedVideosOut)
def related_videos(
    video_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(default=12, le=50),
) -> RelatedVideosOut:
    """Related videos: same channel + same collection (Phase 5B player sidebar).

    Designed to extend later with watch-history / liked / playlist signals.
    """
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")

    same_channel: list[Video] = []
    if video.channel_id:
        same_channel = list(
            db.scalars(
                select(Video)
                .where(Video.channel_id == video.channel_id, Video.id != video.id)
                .order_by(Video.first_seen_at.desc())
                .limit(limit)
            )
        )

    same_collection: list[Video] = []
    coll_ids = _collection_ids_for(db, video)
    if coll_ids:
        other_ids = [
            vid
            for vid in db.scalars(
                select(CollectionItem.video_id)
                .where(
                    CollectionItem.collection_id.in_(coll_ids),
                    CollectionItem.video_id.is_not(None),
                    CollectionItem.video_id != video.id,
                )
                .limit(200)
            )
            if vid is not None
        ]
        seen = {v.id for v in same_channel}
        uniq = [vid for vid in dict.fromkeys(other_ids) if vid not in seen][:limit]
        if uniq:
            by_id = {v.id: v for v in db.scalars(select(Video).where(Video.id.in_(uniq)))}
            same_collection = [by_id[i] for i in uniq if i in by_id]

    return RelatedVideosOut(
        same_channel=_decorate(db, same_channel),
        same_collection=_decorate(db, same_collection),
    )


def _collection_ids_for(db: Session, video: Video) -> set[int]:
    return set(
        db.scalars(
            select(CollectionItem.collection_id).where(
                or_(
                    CollectionItem.video_id == video.id,
                    CollectionItem.youtube_video_id == video.youtube_video_id,
                )
            )
        )
    )
