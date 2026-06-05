"""Subtitle-only refresh endpoints (Phase 7A).

Re-fetch subtitles for videos whose subtitle download failed (e.g. a 429 during
a metadata_only/download job) WITHOUT re-downloading the video body.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.logging_setup import get_logger
from app.models import Job, Video
from app.schemas import (
    JobClassification,
    JobOut,
    JobOutClassified,
    SubtitlesRefreshAllOut,
    SubtitlesRefreshRequest,
)
from app.services import jobs as jobs_svc
from app.services.job_classify import classify_job
from app.services.profiles import get_profile_spec

router = APIRouter(prefix="/api/subtitles", tags=["subtitles"])
logger = get_logger(__name__)


def _resolve_profile(db: Session, profile: str | None) -> str:
    name = profile or "subtitles_refresh_only"
    try:
        get_profile_spec(db, name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown profile: {name!r}")
    return name


def _submit(db: Session, job) -> None:
    db.commit()
    try:
        job.rq_job_id = jobs_svc.submit_job(job.id)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("subtitles: job %s not submitted: %s", job.id, exc)


def _subtitles_failed_jobs(db: Session, scan: int = 500):
    """Recent jobs whose classification flags subtitles_failed (and have a video)."""
    rows = db.scalars(
        select(Job)
        .where(Job.status.in_(("failed", "partial_success")), Job.video_id.is_not(None))
        .order_by(Job.id.desc())
        .limit(scan)
    )
    out = []
    for j in rows:
        c = classify_job(j)
        if "subtitles_failed" in c["reasons"] or (j.meta or {}).get("subtitles_failed"):
            out.append((j, c))
    return out


@router.get("/failed", response_model=list[JobOutClassified])
def list_subtitles_failed(
    db: Session = Depends(get_db), limit: int = Query(default=50, le=500)
) -> list[JobOutClassified]:
    out: list[JobOutClassified] = []
    for j, c in _subtitles_failed_jobs(db)[:limit]:
        item = JobOutClassified.model_validate(j)
        item.classification = JobClassification(**c)
        out.append(item)
    return out


@router.post("/refresh", response_model=JobOut, status_code=201)
def refresh_subtitles(req: SubtitlesRefreshRequest, db: Session = Depends(get_db)) -> JobOut:
    if req.has_conflict():
        raise HTTPException(status_code=400, detail="specify only one of 'target' or 'video'")
    target = req.resolved_target()
    if not target:
        raise HTTPException(status_code=400, detail="missing 'target' (a YouTube video id or URL)")
    profile = _resolve_profile(db, req.profile)
    video = jobs_svc.resolve_or_create_video(db, target)
    if video is None:
        raise HTTPException(status_code=400, detail=f"could not resolve video: {target!r}")
    job = jobs_svc.create_subtitles_refresh_job(db, video, profile_name=profile)
    _submit(db, job)
    return JobOut.model_validate(db.get(type(job), job.id))


@router.post("/videos/{video_id}/refresh", response_model=JobOut, status_code=201)
def refresh_video_subtitles(
    video_id: int, db: Session = Depends(get_db), profile: str | None = Query(default=None)
) -> JobOut:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    name = _resolve_profile(db, profile)
    job = jobs_svc.create_subtitles_refresh_job(db, video, profile_name=name)
    _submit(db, job)
    return JobOut.model_validate(db.get(type(job), job.id))


@router.post("/refresh-failed", response_model=SubtitlesRefreshAllOut)
def refresh_failed_subtitles(
    db: Session = Depends(get_db), limit: int = Query(default=25, le=200)
) -> SubtitlesRefreshAllOut:
    """Create a subtitles_refresh job for each video with a subtitles_failed job."""
    seen: set[int] = set()
    job_ids: list[int] = []
    for j, _c in _subtitles_failed_jobs(db):
        if len(job_ids) >= limit:
            break
        if j.video_id in seen:
            continue
        seen.add(j.video_id)
        video = db.get(Video, j.video_id)
        if video is None:
            continue
        new = jobs_svc.create_subtitles_refresh_job(db, video)
        job_ids.append(new.id)
    db.commit()
    for jid in job_ids:
        try:
            jobs_svc.submit_job(jid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("subtitles refresh-failed: job %s not submitted: %s", jid, exc)
    return SubtitlesRefreshAllOut(
        videos_selected=len(seen), jobs_created=len(job_ids), job_ids=job_ids
    )
