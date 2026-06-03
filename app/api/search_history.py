"""Search-history read endpoints (Phase 3B).

Search queries are highly personal: ``raw_json`` is NOT returned unless
``include_raw=true``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import SearchHistoryEvent
from app.schemas import QueryCount, SearchHistoryEventOut, SearchHistoryStatsOut

router = APIRouter(prefix="/api/search-history", tags=["search-history"])


@router.get("", response_model=list[SearchHistoryEventOut])
def list_search_history(
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, description="filter query text"),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    include_raw: bool = Query(default=False, description="include personal raw_json"),
) -> list[SearchHistoryEventOut]:
    stmt = select(SearchHistoryEvent).order_by(
        SearchHistoryEvent.searched_at.desc(), SearchHistoryEvent.id.desc()
    )
    if q:
        stmt = stmt.where(SearchHistoryEvent.query.ilike(f"%{q}%"))
    rows = list(db.scalars(stmt.limit(limit).offset(offset)))
    out = [SearchHistoryEventOut.model_validate(r) for r in rows]
    if not include_raw:
        for o in out:
            o.raw_json = None
    return out


@router.get("/stats", response_model=SearchHistoryStatsOut)
def search_history_stats(db: Session = Depends(get_db)) -> SearchHistoryStatsOut:
    total = int(db.scalar(select(func.count(SearchHistoryEvent.id))) or 0)
    distinct_queries = int(
        db.scalar(select(func.count(func.distinct(SearchHistoryEvent.query)))) or 0
    )
    earliest = db.scalar(select(func.min(SearchHistoryEvent.searched_at)))
    latest = db.scalar(select(func.max(SearchHistoryEvent.searched_at)))
    top_rows = db.execute(
        select(SearchHistoryEvent.query, func.count(SearchHistoryEvent.id))
        .where(SearchHistoryEvent.query.is_not(None))
        .group_by(SearchHistoryEvent.query)
        .order_by(func.count(SearchHistoryEvent.id).desc())
        .limit(10)
    ).all()
    return SearchHistoryStatsOut(
        total=total,
        distinct_queries=distinct_queries,
        earliest=earliest,
        latest=latest,
        top_queries=[QueryCount(query=q, count=n) for q, n in top_rows],
    )
