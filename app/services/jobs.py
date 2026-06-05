"""Job creation, status transitions, and enqueueing.

Job types (Phase 0-1):
  - ``download``         : a single video (always ``--no-playlist``)
  - ``expand``           : a playlist/channel URL -> child ``download`` jobs
  - ``metadata_refresh`` : info.json + comments only, NEVER re-downloads the body

DB helpers here are pure (no Redis), so jobs can be created and executed inline
(CLI ``--now`` / tests). :func:`submit_job` is the thin RQ hand-off.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Job, Video, utcnow
from app.services.urls import (
    UrlError,
    canonical_video_url,
    is_video_id,
    normalize_url,
)

logger = get_logger(__name__)

# RQ entrypoint is referenced by string to avoid an import cycle
# (jobs -> worker.tasks -> jobs).
_RQ_TASK = "app.worker.tasks.run_job"


def create_job_for_url(
    session: Session,
    raw_url: str,
    profile_name: str,
    *,
    priority: int = 0,
    max_items: int | None = None,
    extra_meta: dict | None = None,
) -> Job:
    """Create a queued job for a URL, classifying video vs playlist/channel.

    ``max_items`` (expand only) overrides EXPAND_MAX_ITEMS for this job.
    ``extra_meta`` is merged into ``job.meta`` (e.g. scheduler tags / policy).
    """
    parsed = normalize_url(raw_url)
    if parsed.kind == "video":
        job_type = "download"
    elif parsed.kind in ("playlist", "channel"):
        job_type = "expand"
    else:
        raise ValueError(f"cannot archive URL of kind {parsed.kind!r}")

    meta: dict = {}
    if job_type == "expand" and max_items is not None:
        meta["max_items"] = int(max_items)
    if extra_meta:
        meta.update(extra_meta)

    job = Job(
        type=job_type,
        status="queued",
        url=parsed.canonical_url,
        profile_name=profile_name,
        priority=priority,
        meta=meta or None,
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


def resolve_or_create_video(session: Session, video_or_url: str) -> Video | None:
    """Resolve a youtube video id or URL to a Video row, creating a stub if needed."""
    value = (video_or_url or "").strip()
    vid = value if is_video_id(value) else None
    if vid is None:
        try:
            parsed = normalize_url(value)
        except UrlError:
            return None
        vid = parsed.video_id
    if not vid:
        return None
    video = session.scalar(select(Video).where(Video.youtube_video_id == vid))
    if video is None:
        video = Video(
            youtube_video_id=vid, url=canonical_video_url(vid), first_seen_at=utcnow()
        )
        session.add(video)
        session.flush()
    return video


def create_comments_refresh_job(
    session: Session,
    video: Video,
    *,
    profile_name: str = "comments_refresh_only",
    priority: int = 0,
    extra_meta: dict | None = None,
) -> Job:
    """Create a comments_refresh job (Phase 4A): comments + diff, never re-DL body."""
    meta = {"target_video_id": video.youtube_video_id}
    if extra_meta:
        meta.update(extra_meta)
    job = Job(
        type="comments_refresh",
        status="queued",
        url=canonical_video_url(video.youtube_video_id),
        video_id=video.id,
        profile_name=profile_name,
        priority=priority,
        meta=meta,
    )
    session.add(job)
    session.flush()
    return job


def create_live_chat_refresh_job(
    session: Session,
    video: Video,
    *,
    profile_name: str = "live_chat_refresh_only",
    priority: int = 0,
    extra_meta: dict | None = None,
) -> Job:
    """Create a live_chat_refresh job (Phase 4B): live chat only, never re-DL body."""
    meta = {"target_video_id": video.youtube_video_id}
    if extra_meta:
        meta.update(extra_meta)
    job = Job(
        type="live_chat_refresh",
        status="queued",
        url=canonical_video_url(video.youtube_video_id),
        video_id=video.id,
        profile_name=profile_name,
        priority=priority,
        meta=meta,
    )
    session.add(job)
    session.flush()
    return job


def create_subtitles_refresh_job(
    session: Session,
    video: Video,
    *,
    profile_name: str = "subtitles_refresh_only",
    priority: int = 0,
    extra_meta: dict | None = None,
) -> Job:
    """Create a subtitles_refresh job (Phase 7A): subtitles only, never re-DL body."""
    meta = {"target_video_id": video.youtube_video_id}
    if extra_meta:
        meta.update(extra_meta)
    job = Job(
        type="subtitles_refresh",
        status="queued",
        url=canonical_video_url(video.youtube_video_id),
        video_id=video.id,
        profile_name=profile_name,
        priority=priority,
        meta=meta,
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


def retry_job(session: Session, job: Job, *, increment: bool = True) -> Job:
    """Reset a failed/canceled/partial job back to queued.

    Increments ``retry_count`` (capped by callers) and clears ``next_retry_at``
    so the scheduler won't double-pick it. Logs/meta are preserved.
    """
    job.status = "queued"
    job.error_message = None
    job.started_at = None
    job.finished_at = None
    job.progress = 0.0
    job.next_retry_at = None
    if increment:
        job.retry_count = (job.retry_count or 0) + 1
    session.flush()
    return job


def apply_classification(
    session: Session, job: Job, settings, error_text: str | None, *, now=None
) -> dict:
    """Persist a stderr-based classification into ``job.meta`` and, for a
    retryable failure under the attempt cap, schedule ``next_retry_at`` (backoff).

    Centralizes Phase 7A logic so every job type (download / metadata /
    comments / live_chat / subtitles) explains itself the same way.
    """
    from app.models import utcnow
    from app.services.job_classify import classify_text, compute_next_retry_at

    now = now or utcnow()
    classification = classify_text(job.status, error_text, job.meta)
    job.meta = {**(job.meta or {}), "classification": classification}

    next_at = None
    if classification["retryable"] and job.status in ("failed", "partial_success"):
        next_at = compute_next_retry_at(
            classification["reasons"], job.retry_count or 0, settings, now, seed=job.id or 0
        )
    job.next_retry_at = next_at
    session.flush()
    return classification


# --------------------------------------------------------------------------- #
# RQ hand-off (optional; requires Redis)
# --------------------------------------------------------------------------- #
def submit_job(job_id: int) -> str | None:
    """Enqueue a job onto RQ. Returns the RQ job id (None if Redis unavailable)."""
    from app.worker.queue import get_queue

    queue = get_queue()
    rq_job = queue.enqueue(_RQ_TASK, job_id, job_timeout="12h")
    return rq_job.id


def create_and_submit(
    session: Session,
    raw_url: str,
    profile_name: str,
    *,
    priority: int = 0,
    max_items: int | None = None,
    extra_meta: dict | None = None,
) -> Job:
    """Create a job, commit it (so the worker can see it), then enqueue to RQ.

    If Redis is unavailable the job stays ``queued`` in the DB (run later with
    ``archiver download run`` or ``--now``). Raises ``UrlError``/``ValueError``
    for unsupported URLs (callers should validate / translate to HTTP errors).
    """
    job = create_job_for_url(
        session,
        raw_url,
        profile_name,
        priority=priority,
        max_items=max_items,
        extra_meta=extra_meta,
    )
    session.commit()
    try:
        job.rq_job_id = submit_job(job.id)
        session.commit()
    except Exception as exc:  # noqa: BLE001 - Redis may be down
        logger.warning("job %s created but not submitted to RQ: %s", job.id, exc)
    return job
