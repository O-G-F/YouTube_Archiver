"""Aggregated dashboard + job-stats endpoints (Phase 5A admin UI)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import get_db
from app.config import get_settings
from app.models import (
    Collection,
    Comment,
    Job,
    LiveChatMessage,
    MetadataSnapshot,
    SearchHistoryEvent,
    Video,
    WatchHistoryEvent,
)
from app.schemas import (
    DashboardCounts,
    DashboardOut,
    HealthOut,
    JobOut,
    JobStatsOut,
    SchedulerStatusOut,
)
from app.services import comment_policy
from app.services import scheduler as scheduler_svc
from app.services.ytdlp import ytdlp_version

router = APIRouter(tags=["dashboard"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _count(db: Session, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    for w in where:
        stmt = stmt.where(w)
    return int(db.scalar(stmt) or 0)


def _job_stats(db: Session) -> JobStatsOut:
    by_status = {
        s: int(n)
        for s, n in db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    }
    by_type = {
        t: int(n)
        for t, n in db.execute(select(Job.type, func.count(Job.id)).group_by(Job.type)).all()
    }
    return JobStatsOut(total=sum(by_status.values()), by_status=by_status, by_type=by_type)


@router.get("/api/job-stats", response_model=JobStatsOut)
def job_stats(db: Session = Depends(get_db)) -> JobStatsOut:
    return _job_stats(db)


@router.get("/api/dashboard", response_model=DashboardOut)
def dashboard(db: Session = Depends(get_db)) -> DashboardOut:
    settings = get_settings()

    db_ok = True
    try:
        db.execute(select(1))
    except Exception:  # noqa: BLE001
        db_ok = False
    redis_ok = False
    try:
        from app.worker.queue import get_redis

        get_redis().ping()
        redis_ok = True
    except Exception:  # noqa: BLE001
        redis_ok = False

    health = HealthOut(
        status="ok" if db_ok else "degraded",
        version=__version__,
        ytdlp_version=ytdlp_version(),
        database=db_ok,
        redis=redis_ok,
    )

    now = _now()
    counts = DashboardCounts(
        videos=_count(db, Video),
        collections=_count(db, Collection),
        crawlable_collections=_count(
            db, Collection, Collection.enabled.is_(True), Collection.crawl_policy != "manual"
        ),
        watch_history=_count(db, WatchHistoryEvent),
        search_history=_count(db, SearchHistoryEvent),
        subscriptions=_count(db, Collection, Collection.type == "channel"),
        comments=_count(db, Comment),
        comments_due=len(comment_policy.select_due_videos(db, now)),
        comments_frozen=comment_policy.count_frozen(db),
        live_chat_messages=_count(db, LiveChatMessage),
        live_chat_due=len(comment_policy.select_due_live_chat_videos(db, now)),
        metadata_snapshots=_count(db, MetadataSnapshot),
    )

    latest = list(db.scalars(select(Job).order_by(Job.id.desc()).limit(10)))
    scheduler = SchedulerStatusOut(**scheduler_svc.status(settings))

    return DashboardOut(
        health=health,
        job_stats=_job_stats(db),
        counts=counts,
        scheduler=scheduler,
        latest_jobs=[JobOut.model_validate(j) for j in latest],
    )
