"""Watch-history read endpoints (Phase 3A).

``raw_json`` is personal data and is NOT returned unless ``include_raw=true``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import WatchHistoryEvent
from app.schemas import ChannelCount, WatchHistoryEventOut, WatchHistoryStatsOut

router = APIRouter(prefix="/api/watch-history", tags=["watch-history"])


@router.get("", response_model=list[WatchHistoryEventOut])
def list_watch_history(
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, description="search title/channel"),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    include_raw: bool = Query(default=False, description="include personal raw_json"),
) -> list[WatchHistoryEventOut]:
    stmt = select(WatchHistoryEvent).order_by(
        WatchHistoryEvent.watched_at.desc(), WatchHistoryEvent.id.desc()
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                WatchHistoryEvent.title.ilike(like),
                WatchHistoryEvent.channel_title.ilike(like),
            )
        )
    rows = list(db.scalars(stmt.limit(limit).offset(offset)))
    out = [WatchHistoryEventOut.model_validate(r) for r in rows]
    if not include_raw:
        for o in out:
            o.raw_json = None
    return out


@router.get("/stats", response_model=WatchHistoryStatsOut)
def watch_history_stats(db: Session = Depends(get_db)) -> WatchHistoryStatsOut:
    total = int(db.scalar(select(func.count(WatchHistoryEvent.id))) or 0)
    with_vid = int(
        db.scalar(
            select(func.count(WatchHistoryEvent.id)).where(
                WatchHistoryEvent.youtube_video_id.is_not(None)
            )
        )
        or 0
    )
    distinct_videos = int(
        db.scalar(select(func.count(func.distinct(WatchHistoryEvent.youtube_video_id))))
        or 0
    )
    distinct_channels = int(
        db.scalar(select(func.count(func.distinct(WatchHistoryEvent.channel_title))))
        or 0
    )
    earliest = db.scalar(select(func.min(WatchHistoryEvent.watched_at)))
    latest = db.scalar(select(func.max(WatchHistoryEvent.watched_at)))
    top_rows = db.execute(
        select(WatchHistoryEvent.channel_title, func.count(WatchHistoryEvent.id))
        .where(WatchHistoryEvent.channel_title.is_not(None))
        .group_by(WatchHistoryEvent.channel_title)
        .order_by(func.count(WatchHistoryEvent.id).desc())
        .limit(10)
    ).all()
    return WatchHistoryStatsOut(
        total=total,
        with_video_id=with_vid,
        distinct_videos=distinct_videos,
        distinct_channels=distinct_channels,
        earliest=earliest,
        latest=latest,
        top_channels=[ChannelCount(channel_title=c, count=n) for c, n in top_rows],
    )
