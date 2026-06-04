"""Library summary + hybrid bootstrap (Phase 5B / 6B).

Surfaces the user-library taxonomy and a one-shot hybrid bootstrap that combines
YouTube Takeout + My Activity Takeout (+ optional API) into the same DB.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.models import Collection, LikedVideo, SearchHistoryEvent, WatchHistoryEvent
from app.schemas import (
    LibraryBootstrapOut,
    LibraryBootstrapRequest,
    LibraryCategoryOut,
    LibrarySummaryOut,
)
from app.services import library as library_svc

router = APIRouter(tags=["library"])


def _count(db: Session, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    for w in where:
        stmt = stmt.where(w)
    return int(db.scalar(stmt) or 0)


@router.get("/api/library/summary", response_model=LibrarySummaryOut)
def library_summary(db: Session = Depends(get_db)) -> LibrarySummaryOut:
    liked = _count(db, LikedVideo)
    watch = _count(db, WatchHistoryEvent)
    search = _count(db, SearchHistoryEvent)
    subs = _count(db, Collection, Collection.type == "channel")
    playlists = _count(db, Collection, Collection.type.in_(("playlist", "takeout_playlist")))
    by_source = {
        (src or "unknown"): int(n)
        for src, n in db.execute(
            select(LikedVideo.source, func.count(LikedVideo.id)).group_by(LikedVideo.source)
        ).all()
    }
    return LibrarySummaryOut(
        categories=[
            LibraryCategoryOut(
                key="liked_videos",
                label="Liked videos",
                count=liked,
                available=True,
                note="Takeout (My Activity = full history, YouTube = recent) + optional YouTube Data API.",
            ),
            LibraryCategoryOut(key="watch_history", label="Watch history", count=watch),
            LibraryCategoryOut(key="search_history", label="Search history", count=search),
            LibraryCategoryOut(key="subscriptions", label="Subscriptions", count=subs),
            LibraryCategoryOut(key="playlists", label="Playlists", count=playlists),
        ],
        liked_sources=by_source,
    )


@router.post("/api/library/bootstrap", response_model=LibraryBootstrapOut)
def library_bootstrap(
    req: LibraryBootstrapRequest, db: Session = Depends(get_db)
) -> LibraryBootstrapOut:
    """Hybrid first-time build: YouTube Takeout + My Activity Takeout (+ optional API)."""
    result = library_svc.bootstrap(
        db,
        get_settings(),
        youtube_takeout_path=req.youtube_takeout,
        myactivity_takeout_path=req.myactivity_takeout,
        limit_watch=req.limit_watch,
        limit_search=req.limit_search,
        limit_subscriptions=req.limit_subscriptions,
        limit_playlists=req.limit_playlists,
        limit_items=req.limit_items,
        limit_liked=req.limit_liked,
        use_api=req.use_api,
        dry_run=req.dry_run,
    )
    db.commit()
    return LibraryBootstrapOut(**result)
