"""Scheduler status / manual trigger endpoints (Phase 2B / 4B)."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.schemas import (
    SchedulerRunOnceOut,
    SchedulerRunOnceRequest,
    SchedulerStatusOut,
)
from app.services import scheduler as scheduler_svc

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/status", response_model=SchedulerStatusOut)
def scheduler_status() -> SchedulerStatusOut:
    return SchedulerStatusOut(**scheduler_svc.status(get_settings()))


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
