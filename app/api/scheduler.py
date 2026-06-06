"""Scheduler status / manual trigger endpoints (Phase 2B / 4B)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.schemas import (
    JobOutClassified,
    JobClassification,
    LikedProgressHistoryOut,
    LikedProgressHistoryPoint,
    RecommendSettingsOut,
    RecommendSettingsRequest,
    SchedulerRunDetailOut,
    SchedulerRunOnceOut,
    SchedulerRunOnceRequest,
    SchedulerRunOut,
    SchedulerStatsOut,
    SchedulerStatusOut,
)
from app.services import scheduler as scheduler_svc
from app.services.job_classify import classify_job

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/status", response_model=SchedulerStatusOut)
def scheduler_status() -> SchedulerStatusOut:
    return SchedulerStatusOut(**scheduler_svc.status(get_settings()))


@router.get("/runs", response_model=list[SchedulerRunOut])
def scheduler_runs(
    db: Session = Depends(get_db),
    run_type: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
) -> list[SchedulerRunOut]:
    return [SchedulerRunOut.model_validate(r) for r in scheduler_svc.list_runs(db, run_type=run_type, limit=limit)]


@router.get("/runs/{run_id}", response_model=SchedulerRunDetailOut)
def scheduler_run_detail(run_id: str, db: Session = Depends(get_db)) -> SchedulerRunDetailOut:
    run = scheduler_svc.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="scheduler run not found")
    return SchedulerRunDetailOut.model_validate(run)


@router.get("/runs/{run_id}/jobs", response_model=list[JobOutClassified])
def scheduler_run_jobs(run_id: str, db: Session = Depends(get_db)) -> list[JobOutClassified]:
    out: list[JobOutClassified] = []
    for j in scheduler_svc.run_jobs(db, run_id):
        item = JobOutClassified.model_validate(j)
        item.classification = JobClassification(**classify_job(j))
        out.append(item)
    return out


@router.get("/stats", response_model=SchedulerStatsOut)
def scheduler_stats(db: Session = Depends(get_db), lookback: int = Query(default=50, le=500)) -> SchedulerStatsOut:
    return SchedulerStatsOut(**scheduler_svc.scheduler_stats(db, lookback=lookback))


@router.post("/recommend-settings", response_model=RecommendSettingsOut)
def scheduler_recommend_settings(
    req: RecommendSettingsRequest | None = None, db: Session = Depends(get_db)
) -> RecommendSettingsOut:
    """Suggest safe archive/retry limits from recent results (does NOT apply them)."""
    req = req or RecommendSettingsRequest()
    return RecommendSettingsOut(**scheduler_svc.recommend_settings(db, get_settings(), lookback=req.lookback))


@router.post("/run-once", response_model=SchedulerRunOnceOut)
def scheduler_run_once(
    req: SchedulerRunOnceRequest | None = None,
) -> SchedulerRunOnceOut:
    """Manually trigger one scheduler pass (runs even if SCHEDULER_ENABLED=false).

    Body selects which parts to run: ``{"collections": true, "comments": true}``
    (both default true). The summary reports collection + comment job counts.
    """
    req = req or SchedulerRunOnceRequest()
    # If any liked pass is explicitly requested, run only the liked passes (avoid
    # surprise collection/comment work from the default-true flags).
    liked_requested = req.liked_metadata or req.liked_archive or req.liked_retry
    summary = scheduler_svc.run_once(
        get_settings(),
        reason="manual",
        max_items=req.max_items,
        do_collections=req.collections and not liked_requested,
        do_comments=req.comments and not liked_requested,
        do_liked_metadata=req.liked_metadata,
        do_liked_archive=req.liked_archive,
        do_liked_retry=req.liked_retry,
    )
    return SchedulerRunOnceOut(**summary)
