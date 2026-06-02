"""Job management + log access endpoints (requirements 9.4, Phase 1.5)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.logging_setup import get_logger
from app.models import DownloadProfile, Job, Video
from app.schemas import (
    JobDetailOut,
    JobLogOut,
    JobLogsOut,
    JobOut,
    ProfileOut,
    VideoOut,
)
from app.services import jobs as jobs_svc
from app.services import logs as logs_svc
from app.services import storage

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
logger = get_logger(__name__)


@router.get("", response_model=list[JobOut])
def list_jobs(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    type: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Job]:
    stmt = select(Job).order_by(Job.id.desc())
    if status:
        stmt = stmt.where(Job.status == status)
    if type:
        stmt = stmt.where(Job.type == type)
    stmt = stmt.limit(limit).offset(offset)
    return list(db.scalars(stmt))


@router.get("/{job_id}", response_model=JobDetailOut)
def get_job(job_id: int, db: Session = Depends(get_db)) -> JobDetailOut:
    job = _require_job(db, job_id)
    return _build_job_detail(db, job)


@router.get("/{job_id}/logs", response_model=JobLogsOut)
def get_job_logs(
    job_id: int,
    db: Session = Depends(get_db),
    tail: int | None = Query(default=None, ge=1, le=1_000_000),
) -> JobLogsOut:
    job = _require_job(db, job_id)
    settings = get_settings()
    return JobLogsOut(
        job_id=job.id,
        log_path=job.log_path,
        available=logs_svc.job_log_dir(settings, job) is not None,
        command=logs_svc.read_log(settings, job, "command", tail=tail),
        stdout=logs_svc.read_log(settings, job, "stdout", tail=tail),
        stderr=logs_svc.read_log(settings, job, "stderr", tail=tail),
    )


@router.get("/{job_id}/logs/{stream}", response_class=PlainTextResponse)
def get_job_log_stream(
    job_id: int,
    stream: str,
    db: Session = Depends(get_db),
    tail: int | None = Query(default=None, ge=1, le=1_000_000),
) -> str:
    if stream not in logs_svc.LOG_FILES:
        raise HTTPException(status_code=404, detail=f"unknown log stream: {stream!r}")
    job = _require_job(db, job_id)
    content = logs_svc.read_log(get_settings(), job, stream, tail=tail)
    if content is None:
        raise HTTPException(status_code=404, detail=f"{stream} log not found")
    return content


# Backwards-compatible combined tail view (now traversal-safe).
@router.get("/{job_id}/log", response_model=JobLogOut)
def get_job_log(job_id: int, db: Session = Depends(get_db)) -> JobLogOut:
    job = _require_job(db, job_id)
    settings = get_settings()
    return JobLogOut(
        job_id=job.id,
        command=logs_svc.read_log(settings, job, "command"),
        stdout_tail=logs_svc.read_log(settings, job, "stdout", tail=400),
        stderr_tail=logs_svc.read_log(settings, job, "stderr", tail=400),
    )


@router.post("/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: int, db: Session = Depends(get_db)) -> Job:
    job = _require_job(db, job_id)
    if job.status not in ("failed", "canceled", "partial_success"):
        raise HTTPException(status_code=409, detail=f"cannot retry a {job.status} job")
    jobs_svc.retry_job(db, job)
    db.commit()
    try:
        job.rq_job_id = jobs_svc.submit_job(job.id)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("retry: job %s not resubmitted to RQ: %s", job.id, exc)
    return job


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: int, db: Session = Depends(get_db)) -> Job:
    job = _require_job(db, job_id)
    if job.status in ("success",):
        raise HTTPException(status_code=409, detail="cannot cancel a finished job")
    _try_cancel_rq(job.rq_job_id)
    jobs_svc.mark_canceled(db, job)
    return job


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _require_job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _build_job_detail(db: Session, job: Job) -> JobDetailOut:
    settings = get_settings()
    detail = JobDetailOut.model_validate(job)
    paths = logs_svc.relative_log_paths(settings, job)
    detail.stdout_log_path = paths["stdout"]
    detail.stderr_log_path = paths["stderr"]
    detail.command_log_path = paths["command"]

    video = db.get(Video, job.video_id) if job.video_id else None
    if video is not None:
        detail.video = VideoOut.model_validate(video)

    if job.profile_name:
        prow = db.scalar(
            select(DownloadProfile).where(DownloadProfile.name == job.profile_name)
        )
        if prow is not None:
            detail.profile = ProfileOut.model_validate(prow)
            if video is not None and job.type == "download":
                out_dir = storage.video_output_dir(
                    settings, prow.media_mode, video.channel_id, video.youtube_video_id
                )
                detail.output_dir = storage.to_relative(settings, out_dir)
    return detail


def _try_cancel_rq(rq_job_id: str | None) -> None:
    if not rq_job_id:
        return
    try:
        from rq.job import Job as RQJob

        from app.worker.queue import get_redis

        RQJob.fetch(rq_job_id, connection=get_redis()).cancel()
    except Exception as exc:  # noqa: BLE001
        logger.info("cancel: could not cancel RQ job %s: %s", rq_job_id, exc)
