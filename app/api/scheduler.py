"""Scheduler status / manual trigger endpoints (Phase 2B)."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.schemas import RefreshAllResult, SchedulerStatusOut
from app.services import scheduler as scheduler_svc

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/status", response_model=SchedulerStatusOut)
def scheduler_status() -> SchedulerStatusOut:
    return SchedulerStatusOut(**scheduler_svc.status(get_settings()))


@router.post("/run-once", response_model=RefreshAllResult)
def scheduler_run_once() -> RefreshAllResult:
    """Manually trigger one scheduler pass (runs even if SCHEDULER_ENABLED=false)."""
    summary = scheduler_svc.run_once(get_settings(), reason="manual")
    return RefreshAllResult(
        collections_checked=summary["collections_checked"],
        jobs_created=summary["jobs_created"],
        job_ids=summary["job_ids"],
    )
