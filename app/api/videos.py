"""Video listing / detail endpoints (admin UI + foundation for the player UI)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.models import Collection, CollectionItem, Comment, Job, MediaFile, Subtitle, Video
from app.schemas import (
    CollectionOut,
    JobOut,
    MediaFileOut,
    VideoDetailOut,
    VideoListItemOut,
    VideoOut,
)

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("", response_model=list[VideoListItemOut])
def list_videos(
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, description="search in title/channel/id"),
    comments_state: str | None = Query(default=None),
    live_chat_state: str | None = Query(default=None),
    has_media: bool | None = Query(default=None, description="filter by media presence"),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[VideoListItemOut]:
    stmt = select(Video).order_by(Video.first_seen_at.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Video.title.ilike(like),
                Video.channel_title.ilike(like),
                Video.youtube_video_id.ilike(like),
            )
        )
    if comments_state:
        stmt = stmt.where(Video.comments_state == comments_state)
    if live_chat_state:
        stmt = stmt.where(Video.live_chat_state == live_chat_state)
    stmt = stmt.limit(limit).offset(offset)
    videos = list(db.scalars(stmt))

    # One grouped query for media-BODY counts (video/audio only — info.json /
    # thumbnail / description are metadata, not a downloaded body). This mirrors
    # the detail player's "未保存" state: 0 -> no body archived.
    ids = [v.id for v in videos]
    counts: dict[int, int] = {}
    if ids:
        rows = db.execute(
            select(MediaFile.video_id, func.count(MediaFile.id))
            .where(
                MediaFile.video_id.in_(ids),
                MediaFile.media_type.in_(("video", "audio")),
            )
            .group_by(MediaFile.video_id)
        ).all()
        counts = {vid: int(n) for vid, n in rows}

    out: list[VideoListItemOut] = []
    for v in videos:
        n = counts.get(v.id, 0)
        if has_media is True and n == 0:
            continue
        if has_media is False and n != 0:
            continue
        item = VideoListItemOut.model_validate(v)
        item.media_files_count = n
        out.append(item)
    return out


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
    """Stream a media file that belongs to this video (simple in-browser player).

    The on-disk path comes ONLY from the DB record (no user-supplied path) and is
    hard-guarded to live under ARCHIVE_ROOT — there is no traversal vector.
    """
    mf = db.get(MediaFile, media_file_id)
    if mf is None or mf.video_id != video_id:
        raise HTTPException(status_code=404, detail="media file not found")
    settings = get_settings()
    root = settings.archive_root.resolve()
    abs_path = (root / mf.path).resolve()
    try:
        abs_path.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="media file outside archive root")
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="media file missing on disk")
    return FileResponse(abs_path)


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
    # A video may be linked by FK (video_id) or only by external id.
    coll_ids = set(
        db.scalars(
            select(CollectionItem.collection_id).where(
                or_(
                    CollectionItem.video_id == video.id,
                    CollectionItem.youtube_video_id == video.youtube_video_id,
                )
            )
        )
    )
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
