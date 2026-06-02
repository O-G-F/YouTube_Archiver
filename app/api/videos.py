"""Video listing endpoints (foundation for the Phase 7 player UI)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Comment, Subtitle, Video
from app.schemas import MediaFileOut, VideoDetailOut, VideoOut

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("", response_model=list[VideoOut])
def list_videos(
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, description="search in title/channel"),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Video]:
    stmt = select(Video).order_by(Video.first_seen_at.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Video.title.ilike(like), Video.channel_title.ilike(like))
        )
    stmt = stmt.limit(limit).offset(offset)
    return list(db.scalars(stmt))


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
