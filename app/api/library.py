"""Library summary (Phase 5B).

Surfaces the user-library taxonomy the UI is being designed around. Liked videos
are not yet synced (planned for a later phase using Google Takeout AND/OR the
YouTube Data API), so that category is reported as ``available=false``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Collection, SearchHistoryEvent, WatchHistoryEvent
from app.schemas import LibraryCategoryOut, LibrarySummaryOut

router = APIRouter(tags=["library"])


def _count(db: Session, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    for w in where:
        stmt = stmt.where(w)
    return int(db.scalar(stmt) or 0)


@router.get("/api/library/summary", response_model=LibrarySummaryOut)
def library_summary(db: Session = Depends(get_db)) -> LibrarySummaryOut:
    watch = _count(db, WatchHistoryEvent)
    search = _count(db, SearchHistoryEvent)
    subs = _count(db, Collection, Collection.type == "channel")
    playlists = _count(db, Collection, Collection.type.in_(("playlist", "takeout_playlist")))
    return LibrarySummaryOut(
        categories=[
            LibraryCategoryOut(
                key="liked_videos",
                label="Liked videos",
                count=0,
                available=False,
                note="Planned: Google Takeout import and/or YouTube Data API sync (Phase 6A+).",
            ),
            LibraryCategoryOut(key="watch_history", label="Watch history", count=watch),
            LibraryCategoryOut(key="search_history", label="Search history", count=search),
            LibraryCategoryOut(key="subscriptions", label="Subscriptions", count=subs),
            LibraryCategoryOut(key="playlists", label="Playlists", count=playlists),
        ]
    )
