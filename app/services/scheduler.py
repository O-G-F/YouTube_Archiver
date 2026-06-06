"""Collection re-crawl scheduler (Phase 2B).

A simple loop that periodically creates ``expand`` jobs for enabled collections
(honouring each collection's ``crawl_policy``):

  - ``manual``   : skipped by the scheduler (manual refresh only)
  - ``new_only`` : discover + enqueue new videos, NO removed detection
  - ``refresh``  : also update last_seen_at / detect removed_at

Scheduler-created jobs are tagged ``meta.scheduled_by`` and carry the resolved
``detect_removed`` flag for the worker. A run summary is appended to
``LOG_ROOT/scheduler/scheduler.log``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, or_, select

from app.config import Settings, get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import Collection, DownloadProfile
from app.services import comment_policy
from app.services import jobs as jobs_svc

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _scheduler_log(settings: Settings, message: str) -> None:
    try:
        log_dir = settings.log_root / "scheduler"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "scheduler.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{_utc_stamp()} {message}\n")
    except OSError:
        pass


def _profile_name_for(session, collection: Collection, settings: Settings) -> str:
    if collection.download_profile_id:
        prow = session.get(DownloadProfile, collection.download_profile_id)
        if prow is not None:
            return prow.name
    return settings.default_profile


def _enqueue_collections(s, settings: Settings, reason: str, cap: int | None) -> list[int]:
    """Create one expand job per crawlable collection. Returns created job ids."""
    job_ids: list[int] = []
    collections = list(s.scalars(select(Collection).where(Collection.enabled.is_(True))))
    for c in collections:
        policy = c.crawl_policy or "new_only"
        if policy == "manual":
            continue
        profile = _profile_name_for(s, c, settings)
        try:
            job = jobs_svc.create_job_for_url(
                s,
                c.url,
                profile,
                max_items=cap,
                extra_meta={
                    "scheduled_by": reason,
                    "crawl_policy": policy,
                    "detect_removed": policy == "refresh",
                    "collection_id": c.id,
                },
            )
            job_ids.append(job.id)
        except Exception as exc:  # noqa: BLE001 - bad/non-expandable url
            logger.warning("scheduler: skipping collection %s (%s): %s", c.id, c.url, exc)
    return job_ids


def _crawlable_count(s) -> int:
    return sum(
        1
        for c in s.scalars(select(Collection).where(Collection.enabled.is_(True)))
        if (c.crawl_policy or "new_only") != "manual"
    )


def _requeue_due_retries(s, now: datetime, limit: int | None) -> list[int]:
    """Re-queue retryable jobs whose backoff window (next_retry_at) has elapsed."""
    from app.models import Job

    stmt = (
        select(Job)
        .where(
            Job.status.in_(("failed", "partial_success")),
            Job.next_retry_at.is_not(None),
            Job.next_retry_at <= now,
        )
        .order_by(Job.next_retry_at.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    job_ids: list[int] = []
    for job in s.scalars(stmt):
        jobs_svc.retry_job(s, job)  # increments retry_count, clears next_retry_at
        job_ids.append(job.id)
    return job_ids


def _enqueue_due_comments(s, settings: Settings, now: datetime, limit: int | None) -> list[int]:
    """Create comments_refresh jobs for videos due for a comment refresh."""
    job_ids: list[int] = []
    due = comment_policy.select_due_videos(s, now, limit=limit)
    for v in due:
        prev = v.next_comments_refresh_at
        job = jobs_svc.create_comments_refresh_job(
            s,
            v,
            extra_meta={
                "scheduled_by": "scheduler_comments",
                "due_reason": "never_refreshed" if prev is None else "due",
                "previous_next_comments_refresh_at": prev.isoformat() if prev else None,
                "comments_policy": getattr(v, "comments_refresh_policy", None),
            },
        )
        job_ids.append(job.id)
    return job_ids


def _active_liked_body_count(s) -> int:
    """Active (queued/running) liked-archive BODY jobs (profile != metadata_only)."""
    from app.models import Job

    n = 0
    for j in s.scalars(
        select(Job).where(Job.type == "download", Job.status.in_(("queued", "running")))
    ):
        meta = j.meta or {}
        if meta.get("source_action") == "liked_archive" and j.profile_name != "metadata_only":
            n += 1
    return n


def _run_liked_metadata(s, settings: Settings, limit: int) -> dict:
    """Scheduler pass: enqueue metadata_only for liked videos missing metadata."""
    from app.services import liked_archive as la

    r = la.enqueue_metadata(
        s, settings,
        filters=la.LikedFilters(missing_metadata=True),
        limit=limit, submit=False,
        extra_meta={"scheduled_by": "scheduler_liked_metadata", "selected_by": "missing_metadata"},
    )
    return {"selected": r.selected_count, "jobs_created": r.jobs_created,
            "job_ids": list(r.job_ids), "skipped_active": 0, "skipped_dup": r.skipped_existing_job}


def _run_liked_archive(s, settings: Settings, limit: int) -> dict:
    """Scheduler pass: enqueue a small BODY archive (downloads bodies)."""
    from app.services import liked_archive as la

    # Safety brake: don't pile body DLs while one is already in flight.
    if settings.scheduler_liked_suppress_when_active and _active_liked_body_count(s) > 0:
        return {"selected": 0, "jobs_created": 0, "job_ids": [], "skipped_active": 1, "skipped_dup": 0}
    source = (settings.scheduler_liked_archive_source or "").strip() or None
    r = la.enqueue_archive(
        s, settings,
        filters=la.LikedFilters(
            source=source, missing_body=settings.scheduler_liked_archive_missing_body_only
        ),
        limit=limit,
        profile=settings.effective_scheduler_liked_archive_profile,
        submit=False,
        extra_meta={"scheduled_by": "scheduler_liked_archive", "selected_by": "missing_body"},
    )
    return {"selected": r.selected_count, "jobs_created": r.jobs_created,
            "job_ids": list(r.job_ids), "skipped_active": 0, "skipped_dup": r.skipped_existing_job}


def _run_liked_retry(s, settings: Settings, now: datetime, limit: int) -> dict:
    """Scheduler pass: re-queue retryable liked jobs whose backoff has elapsed."""
    from app.services import liked_archive as la

    candidates = la.retryable_liked(s, settings, limit=limit, now=now)
    job_ids: list[int] = []
    for j, _c in candidates:
        j.meta = {**(j.meta or {}), "scheduled_by": "scheduler_liked_retry", "selected_by": "retryable"}
        jobs_svc.retry_job(s, j)
        job_ids.append(j.id)
    return {"selected": len(candidates), "requeued": len(job_ids), "job_ids": job_ids}


def run_once(
    settings: Settings | None = None,
    *,
    reason: str = "scheduler",
    max_items: int | None = None,
    do_collections: bool = True,
    do_comments: bool = True,
    do_liked_metadata: bool = False,
    do_liked_archive: bool = False,
    do_liked_retry: bool = False,
) -> dict:
    """One scheduler cycle: re-crawl collections and/or enqueue due comment refreshes.

    With ``reason="scheduler"`` each part runs only if its toggle is enabled
    (``SCHEDULER_ENABLED`` for collections, ``SCHEDULER_COMMENTS_ENABLED`` for
    comments). A manual run (``reason="manual"``) honours the ``do_*`` flags
    directly (used by the run-once CLI/API ``--collections``/``--comments``).
    """
    settings = settings or get_settings()
    settings.ensure_dirs()

    is_scheduler = reason == "scheduler"
    run_collections = do_collections and (settings.scheduler_enabled if is_scheduler else True)
    run_comments = do_comments and (settings.scheduler_comments_enabled if is_scheduler else True)
    run_retries = settings.scheduler_retry_enabled if is_scheduler else False
    # Liked passes: scheduler honours the enable flags; a manual run honours the
    # explicit do_liked_* flags (used by run-once --liked-* / API).
    run_liked_meta = (settings.scheduler_liked_metadata_enabled if is_scheduler else do_liked_metadata)
    run_liked_arch = (settings.scheduler_liked_archive_enabled if is_scheduler else do_liked_archive)
    run_liked_retry = (settings.scheduler_liked_retry_enabled if is_scheduler else do_liked_retry)

    base = {
        "reason": reason,
        "collections_checked": 0,
        "collection_jobs_created": 0,
        "due_comment_videos_checked": 0,
        "comments_jobs_created": 0,
        "retries_requeued": 0,
        "liked_metadata_selected": 0,
        "liked_metadata_jobs_created": 0,
        "liked_archive_selected": 0,
        "liked_archive_jobs_created": 0,
        "liked_retry_selected": 0,
        "liked_retry_jobs_requeued": 0,
        "skipped_active_jobs": 0,
        "skipped_duplicates": 0,
        "skipped_frozen": 0,
        "skipped_recent": 0,
        "jobs_created": 0,
        "submitted": 0,
        "job_ids": [],
    }
    any_run = any([run_collections, run_comments, run_retries, run_liked_meta, run_liked_arch, run_liked_retry])
    if not any_run:
        _scheduler_log(settings, f"skip: nothing to do (reason={reason})")
        return {**base, "enabled": False}

    now = _now()
    cap = max_items if max_items is not None else settings.expand_max_items
    collection_job_ids: list[int] = []
    comment_job_ids: list[int] = []
    retry_job_ids: list[int] = []
    liked_job_ids: list[int] = []
    collections_checked = 0
    due_checked = 0
    skipped_frozen = 0
    skipped_recent = 0
    lm = {"selected": 0, "jobs_created": 0, "job_ids": [], "skipped_active": 0, "skipped_dup": 0}
    larch = {"selected": 0, "jobs_created": 0, "job_ids": [], "skipped_active": 0, "skipped_dup": 0}
    lretry = {"selected": 0, "requeued": 0, "job_ids": []}

    with session_scope() as s:
        if run_collections:
            collections_checked = _crawlable_count(s)
            collection_job_ids = _enqueue_collections(s, settings, reason, cap)
        if run_comments:
            skipped_frozen = comment_policy.count_frozen(s)
            skipped_recent = comment_policy.count_recent(s, now)
            comment_job_ids = _enqueue_due_comments(
                s, settings, now, settings.scheduler_comments_limit_per_run
            )
            due_checked = len(comment_job_ids)
        if run_retries:
            retry_job_ids = _requeue_due_retries(
                s, now, settings.scheduler_retry_limit_per_run
            )
        if run_liked_meta:
            lm = _run_liked_metadata(s, settings, settings.scheduler_liked_metadata_limit_per_run)
        if run_liked_arch:
            larch = _run_liked_archive(s, settings, settings.scheduler_liked_archive_limit_per_run)
        if run_liked_retry:
            lretry = _run_liked_retry(s, settings, now, settings.scheduler_liked_retry_limit_per_run)
        liked_job_ids = list(lm["job_ids"]) + list(larch["job_ids"]) + list(lretry["job_ids"])
        s.commit()

    job_ids = collection_job_ids + comment_job_ids + retry_job_ids + liked_job_ids
    submitted = 0
    for jid in job_ids:
        try:
            jobs_svc.submit_job(jid)
            submitted += 1
        except Exception as exc:  # noqa: BLE001 - Redis may be down
            logger.warning("scheduler: job %s not submitted to RQ: %s", jid, exc)

    summary = {
        "enabled": True,
        "reason": reason,
        "collections_checked": collections_checked,
        "collection_jobs_created": len(collection_job_ids),
        "due_comment_videos_checked": due_checked,
        "comments_jobs_created": len(comment_job_ids),
        "retries_requeued": len(retry_job_ids),
        "liked_metadata_selected": lm["selected"],
        "liked_metadata_jobs_created": lm["jobs_created"],
        "liked_archive_selected": larch["selected"],
        "liked_archive_jobs_created": larch["jobs_created"],
        "liked_retry_selected": lretry["selected"],
        "liked_retry_jobs_requeued": lretry["requeued"],
        "skipped_active_jobs": lm["skipped_active"] + larch["skipped_active"],
        "skipped_duplicates": lm["skipped_dup"] + larch["skipped_dup"],
        "skipped_frozen": skipped_frozen,
        "skipped_recent": skipped_recent,
        "jobs_created": len(job_ids),
        "submitted": submitted,
        "job_ids": job_ids,
    }
    _scheduler_log(settings, f"run_once {summary}")
    logger.info("scheduler run_once: %s", summary)
    return summary


def run_forever(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    interval = max(5, settings.scheduler_interval_seconds)
    logger.info(
        "scheduler: loop start (enabled=%s, interval=%ss)",
        settings.scheduler_enabled,
        interval,
    )
    _scheduler_log(
        settings,
        f"loop start enabled={settings.scheduler_enabled} interval={interval}s",
    )
    while True:
        try:
            run_once(settings, reason="scheduler")
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("scheduler: run_once iteration failed")
        time.sleep(interval)


def status(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    now = _now()
    with session_scope() as s:
        enabled_collections = int(
            s.scalar(
                select(func.count(Collection.id)).where(Collection.enabled.is_(True))
            )
            or 0
        )
        crawlable = int(
            s.scalar(
                select(func.count(Collection.id)).where(
                    Collection.enabled.is_(True),
                    or_(
                        Collection.crawl_policy.is_(None),
                        Collection.crawl_policy != "manual",
                    ),
                )
            )
            or 0
        )
        due_comment_videos = len(comment_policy.select_due_videos(s, now))
        frozen_comment_videos = comment_policy.count_frozen(s)
    return {
        "enabled": settings.scheduler_enabled,
        "interval_seconds": settings.scheduler_interval_seconds,
        "enabled_collections": enabled_collections,
        "crawlable_collections": crawlable,
        "comments_enabled": settings.scheduler_comments_enabled,
        "comments_limit_per_run": settings.scheduler_comments_limit_per_run,
        "due_comment_videos": due_comment_videos,
        "frozen_comment_videos": frozen_comment_videos,
    }
