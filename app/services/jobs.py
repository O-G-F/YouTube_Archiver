"""Job creation, status transitions, and enqueueing.

Job types (Phase 0-1):
  - ``download``         : a single video (always ``--no-playlist``)
  - ``expand``           : a playlist/channel URL -> child ``download`` jobs
  - ``metadata_refresh`` : info.json + comments only, NEVER re-downloads the body

DB helpers here are pure (no Redis), so jobs can be created and executed inline
(CLI ``--now`` / tests). :func:`submit_job` is the thin RQ hand-off.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Job, Video, utcnow
from app.services.urls import canonical_video_url, normalize_url

# RQ entrypoint is referenced by string to avoid an import cycle
# (jobs -> worker.tasks -> jobs).
_RQ_TASK = "app.worker.tasks.run_job"


def create_job_for_url(
    session: Session,
    raw_url: str,
    profile_name: str,
    *,
    priority: int = 0,
) -> Job:
    """Create a queued job for a URL, classifying video vs playlist/channel."""
    parsed = normalize_url(raw_url)
    if parsed.kind == "video":
        job_type = "download"
    elif parsed.kind in ("playlist", "channel"):
        job_type = "expand"
    else:
        raise ValueError(f"cannot archive URL of kind {parsed.kind!r}")

    job = Job(
        type=job_type,
        status="queued",
        url=parsed.canonical_url,
        profile_name=profile_name,
        priority=priority,
    )
    session.add(job)
    session.flush()
    return job


def create_download_child_job(
    session: Session,
    youtube_video_id: str,
    profile_name: str,
    *,
    parent_job_id: int | None = None,
    collection_id: int | None = None,
    priority: int = 0,
) -> Job:
    job = Job(
        type="download",
        status="queued",
        url=canonical_video_url(youtube_video_id),
        profile_name=profile_name,
        parent_job_id=parent_job_id,
        collection_id=collection_id,
        priority=priority,
    )
    session.add(job)
    session.flush()
    return job


def create_metadata_refresh_job(
    session: Session,
    video: Video,
    *,
    profile_name: str = "comments_refresh_only",
    priority: int = 0,
) -> Job:
    """Create a metadata/comment refresh job that never re-downloads the body."""
    job = Job(
        type="metadata_refresh",
        status="queued",
        url=canonical_video_url(video.youtube_video_id),
        video_id=video.id,
        profile_name=profile_name,
        priority=priority,
    )
    session.add(job)
    session.flush()
    return job


# --------------------------------------------------------------------------- #
# Status transitions
# --------------------------------------------------------------------------- #
def mark_running(session: Session, job: Job) -> None:
    job.status = "running"
    job.started_at = utcnow()
    job.error_message = None
    session.flush()


def mark_success(session: Session, job: Job, *, progress: float = 100.0) -> None:
    job.status = "success"
    job.progress = progress
    job.finished_at = utcnow()
    session.flush()


def mark_partial_success(session: Session, job: Job, note: str) -> None:
    """Job finished with usable output despite a non-zero yt-dlp exit.

    Used when, e.g., the body/info/subtitles were saved but one subtitle
    language returned HTTP 429. The note (stderr tail) is kept for inspection.
    """
    job.status = "partial_success"
    job.progress = 100.0
    job.finished_at = utcnow()
    job.error_message = note[:8000]
    session.flush()


def mark_failed(session: Session, job: Job, error_message: str) -> None:
    job.status = "failed"
    job.finished_at = utcnow()
    job.error_message = error_message[:8000]
    session.flush()


def mark_canceled(session: Session, job: Job) -> None:
    job.status = "canceled"
    job.finished_at = utcnow()
    session.flush()


def retry_job(session: Session, job: Job) -> Job:
    """Reset a failed/canceled job back to queued."""
    job.status = "queued"
    job.error_message = None
    job.started_at = None
    job.finished_at = None
    job.progress = 0.0
    session.flush()
    return job


# --------------------------------------------------------------------------- #
# RQ hand-off (optional; requires Redis)
# --------------------------------------------------------------------------- #
def submit_job(job_id: int) -> str | None:
    """Enqueue a job onto RQ. Returns the RQ job id (None if Redis unavailable)."""
    from app.worker.queue import get_queue

    queue = get_queue()
    rq_job = queue.enqueue(_RQ_TASK, job_id, job_timeout="12h")
    return rq_job.id
