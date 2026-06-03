"""Archive endpoints: register URLs for download (requirement 5.1.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.logging_setup import get_logger
from app.schemas import (
    ArchiveBatchRequest,
    ArchiveUrlRequest,
    BatchItemResult,
    BatchResult,
    ExpandRequest,
    JobOut,
)
from app.services import jobs as jobs_svc
from app.services.profiles import get_profile_spec
from app.services.urls import UrlError, normalize_url

router = APIRouter(prefix="/api/archive", tags=["archive"])
logger = get_logger(__name__)


def _resolve_profile(db: Session, profile: str | None) -> str:
    name = profile or get_settings().default_profile
    try:
        get_profile_spec(db, name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown profile: {name!r}")
    return name


def _create_and_submit(db: Session, url: str, profile: str, priority: int) -> int:
    job = jobs_svc.create_job_for_url(db, url, profile, priority=priority)
    db.commit()  # ensure the worker can see the row before we enqueue it
    try:
        rq_id = jobs_svc.submit_job(job.id)
        job.rq_job_id = rq_id
        db.commit()
    except Exception as exc:  # noqa: BLE001 - Redis may be down; job stays queued
        logger.warning("archive: job %s created but not submitted to RQ: %s", job.id, exc)
    return job.id


@router.post("/url", response_model=JobOut, status_code=201)
def archive_url(req: ArchiveUrlRequest, db: Session = Depends(get_db)) -> JobOut:
    profile = _resolve_profile(db, req.profile)
    try:
        job_id = _create_and_submit(db, req.url, profile, req.priority)
    except UrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JobOut.model_validate(_get_job(db, job_id))


# Browser extension / bookmarklet target (requirement 5.1.4); same as /url.
@router.post("/current-tab", response_model=JobOut, status_code=201)
def archive_current_tab(req: ArchiveUrlRequest, db: Session = Depends(get_db)) -> JobOut:
    return archive_url(req, db)


@router.post("/expand", response_model=JobOut, status_code=201)
def archive_expand(req: ExpandRequest, db: Session = Depends(get_db)) -> JobOut:
    """Create an expand job for a playlist / channel(-tab) URL."""
    profile = _resolve_profile(db, req.profile)
    try:
        parsed = normalize_url(req.url)
    except UrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if parsed.kind not in ("playlist", "channel"):
        raise HTTPException(
            status_code=400, detail="not an expandable (playlist/channel) URL"
        )
    job = jobs_svc.create_and_submit(db, req.url, profile, max_items=req.max_items)
    return JobOut.model_validate(_get_job(db, job.id))


@router.post("/batch", response_model=BatchResult)
def archive_batch(req: ArchiveBatchRequest, db: Session = Depends(get_db)) -> BatchResult:
    profile = _resolve_profile(db, req.profile)
    results: list[BatchItemResult] = []
    created = 0
    for url in req.urls:
        try:
            job_id = _create_and_submit(db, url, profile, req.priority)
            results.append(BatchItemResult(url=url, job_id=job_id))
            created += 1
        except (UrlError, ValueError) as exc:
            db.rollback()
            results.append(BatchItemResult(url=url, error=str(exc)))
    return BatchResult(created=created, failed=len(results) - created, results=results)


def _get_job(db: Session, job_id: int):
    from app.models import Job

    return db.get(Job, job_id)
