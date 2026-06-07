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
import uuid
from datetime import datetime, timedelta, timezone
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


def _run_liked_metadata(s, settings: Settings, limit: int, run_id: str) -> dict:
    """Scheduler pass: enqueue metadata_only for liked videos missing metadata."""
    from app.services import liked_archive as la

    r = la.enqueue_metadata(
        s, settings,
        filters=la.LikedFilters(missing_metadata=True),
        limit=limit, submit=False,
        extra_meta={
            "scheduled_by": "scheduler_liked_metadata",
            "selected_by": "missing_metadata",
            "scheduler_run_id": run_id,
        },
    )
    return {"selected": r.selected_count, "jobs_created": r.jobs_created,
            "job_ids": list(r.job_ids), "skipped_active": 0, "skipped_dup": r.skipped_existing_job}


def _run_liked_archive(s, settings: Settings, limit: int, run_id: str) -> dict:
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
        extra_meta={
            "scheduled_by": "scheduler_liked_archive",
            "selected_by": "missing_body",
            "scheduler_run_id": run_id,
        },
    )
    return {"selected": r.selected_count, "jobs_created": r.jobs_created,
            "job_ids": list(r.job_ids), "skipped_active": 0, "skipped_dup": r.skipped_existing_job}


def _run_liked_retry(s, settings: Settings, now: datetime, limit: int, run_id: str) -> dict:
    """Scheduler pass: re-queue retryable liked jobs whose backoff has elapsed."""
    from app.services import liked_archive as la

    candidates = la.retryable_liked(s, settings, limit=limit, now=now)
    # count retryable jobs still inside their backoff window (skipped this pass)
    all_retryable = la.retryable_liked(s, settings, limit=10_000)  # now=None -> ignores backoff
    skipped_backoff = sum(
        1 for j, _c in all_retryable if j.next_retry_at is not None and j.next_retry_at > now
    )
    job_ids: list[int] = []
    for j, _c in candidates:
        j.meta = {
            **(j.meta or {}),
            "scheduled_by": "scheduler_liked_retry",
            "selected_by": "retryable",
            "scheduler_run_id": run_id,
        }
        jobs_svc.retry_job(s, j)
        job_ids.append(j.id)
    return {"selected": len(candidates), "requeued": len(job_ids),
            "job_ids": job_ids, "skipped_backoff": skipped_backoff}


def _body_count(s) -> int:
    from app.models import MediaFile

    return int(
        s.scalar(
            select(func.count(MediaFile.id)).where(MediaFile.media_type.in_(("video", "audio")))
        )
        or 0
    )


def _liked_snapshot(settings: Settings) -> dict:
    """Point-in-time {progress, queue, body} snapshot (own session; fail-safe)."""
    from app.services import liked_archive as la
    from app.services import queue_health

    try:
        with session_scope() as s:
            return {
                "progress": la.progress(s, settings),
                "queue": queue_health.queue_status(s),
                "body": _body_count(s),
            }
    except Exception:  # noqa: BLE001
        logger.exception("scheduler: liked snapshot failed")
        return {"progress": {}, "queue": {}, "body": 0}


def _record_scheduler_run(s, settings, *, run_id, run_type, reason, started_at, summary,
                          progress_before, progress_after, queue_before, queue_after,
                          body_before, body_after, skipped_backoff) -> None:
    """Persist a SchedulerRun row. Fail-safe: never breaks job processing."""
    from app.models import SchedulerRun

    try:
        liked_created = (
            summary["liked_metadata_jobs_created"]
            + summary["liked_archive_jobs_created"]
            + summary["liked_retry_jobs_requeued"]
        )
        non_liked_created = summary["collection_jobs_created"] + summary["comments_jobs_created"]
        total_created = liked_created + non_liked_created
        skipped = summary["skipped_active_jobs"] + summary["skipped_duplicates"] + skipped_backoff
        # status: failed only if the pass errored (handled by caller); else
        # partial_success when something was skipped or retryable work remains.
        if total_created == 0 and skipped > 0:
            status = "partial_success"
        elif (progress_after or {}).get("retryable_liked_jobs", 0) > 0 and run_type in (
            "liked_archive", "liked_retry", "all"
        ):
            status = "partial_success"
        else:
            status = "success"
        run = SchedulerRun(
            run_id=run_id,
            run_type=run_type,
            reason=reason,
            started_at=started_at,
            finished_at=_now(),
            status=status,
            selected_count=summary["liked_metadata_selected"]
            + summary["liked_archive_selected"]
            + summary["liked_retry_selected"],
            jobs_created=total_created,
            jobs_submitted=summary["submitted"],
            skipped_active_jobs=summary["skipped_active_jobs"],
            skipped_duplicates=summary["skipped_duplicates"],
            skipped_backoff=skipped_backoff,
            retryable_count=(progress_after or {}).get("retryable_liked_jobs", 0),
            failed_count=(progress_after or {}).get("failed_liked_jobs", 0),
            partial_count=(progress_after or {}).get("partial_liked_jobs", 0),
            success_count=(progress_after or {}).get("body_saved", 0),
            body_count_before=body_before,
            body_count_after=body_after,
            meta={
                "summary": summary,
                "progress_before": progress_before,
                "progress_after": progress_after,
                "queue_before": queue_before,
                "queue_after": queue_after,
            },
        )
        s.add(run)
        s.flush()
    except Exception:  # noqa: BLE001 - recording must never break the run
        logger.exception("scheduler: failed to record SchedulerRun %s", run_id)


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
    started_at = now
    run_id = uuid.uuid4().hex[:16]
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
    lretry = {"selected": 0, "requeued": 0, "job_ids": [], "skipped_backoff": 0}
    # run-type for history (single pass -> its name; multiple -> "all")
    active_types = [
        t for t, on in [
            ("collections", run_collections), ("comments", run_comments),
            ("liked_metadata", run_liked_meta), ("liked_archive", run_liked_arch),
            ("liked_retry", run_liked_retry),
        ] if on
    ]
    run_type = active_types[0] if len(active_types) == 1 else "all"

    snap_before = _liked_snapshot(settings)

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
            lm = _run_liked_metadata(s, settings, settings.scheduler_liked_metadata_limit_per_run, run_id)
        if run_liked_arch:
            larch = _run_liked_archive(s, settings, settings.scheduler_liked_archive_limit_per_run, run_id)
        if run_liked_retry:
            lretry = _run_liked_retry(s, settings, now, settings.scheduler_liked_retry_limit_per_run, run_id)
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
        "run_id": run_id,
    }

    # Record the run + a progress/queue snapshot (Phase 7E). Fail-safe.
    snap_after = _liked_snapshot(settings)
    try:
        with session_scope() as s:
            _record_scheduler_run(
                s, settings,
                run_id=run_id, run_type=run_type, reason=reason, started_at=started_at,
                summary=summary,
                progress_before=snap_before.get("progress"), progress_after=snap_after.get("progress"),
                queue_before=snap_before.get("queue"), queue_after=snap_after.get("queue"),
                body_before=snap_before.get("body", 0), body_after=snap_after.get("body", 0),
                skipped_backoff=lretry.get("skipped_backoff", 0),
            )
            s.commit()
    except Exception:  # noqa: BLE001 - never let history recording break a run
        logger.exception("scheduler: run recording failed (run_id=%s)", run_id)

    summary["skipped_backoff"] = lretry.get("skipped_backoff", 0)

    # Optional auto-retention (scheduler loop only; default OFF). Fail-safe.
    if is_scheduler and (settings.scheduler_run_retention_days or settings.scheduler_run_keep_last):
        try:
            with session_scope() as s:
                res = cleanup_runs(
                    s,
                    keep_last=settings.scheduler_run_keep_last,
                    older_than_days=settings.scheduler_run_retention_days,
                    dry_run=False,
                    now=now,
                )
                s.commit()
            if res["deleted"]:
                logger.info("scheduler: retention pruned %s old run(s)", res["deleted"])
        except Exception:  # noqa: BLE001
            logger.exception("scheduler: retention cleanup failed")

    _scheduler_log(settings, f"run_once {summary}")
    logger.info("scheduler run_once: %s", summary)
    return summary


def list_runs(
    s, *, run_type: str | None = None, status: str | None = None,
    date_from: datetime | None = None, date_to: datetime | None = None, limit: int = 50,
) -> list:
    from app.models import SchedulerRun

    stmt = select(SchedulerRun).order_by(SchedulerRun.id.desc())
    if run_type:
        stmt = stmt.where(SchedulerRun.run_type == run_type)
    if status:
        stmt = stmt.where(SchedulerRun.status == status)
    if date_from:
        stmt = stmt.where(SchedulerRun.started_at >= date_from)
    if date_to:
        stmt = stmt.where(SchedulerRun.started_at <= date_to)
    return list(s.scalars(stmt.limit(limit)))


def get_run(s, run_id: str):
    from app.models import SchedulerRun

    return s.scalar(select(SchedulerRun).where(SchedulerRun.run_id == run_id))


def run_jobs(s, run_id: str, *, limit: int = 500) -> list:
    """Jobs created by a scheduler run (job.meta.scheduler_run_id == run_id)."""
    from app.models import Job

    out = []
    for j in s.scalars(select(Job).order_by(Job.id.desc()).limit(2000)):
        if (j.meta or {}).get("scheduler_run_id") == run_id:
            out.append(j)
            if len(out) >= limit:
                break
    return out


def progress_history(
    s, *, limit: int = 100, run_type: str | None = None,
    date_from: datetime | None = None, date_to: datetime | None = None,
    downsample: str | None = None,
) -> list[dict]:
    """Liked-progress snapshots over time (from scheduler runs that captured one).

    ``downsample="daily"`` keeps only the last snapshot per calendar day.
    """
    from app.models import SchedulerRun

    stmt = select(SchedulerRun).order_by(SchedulerRun.id.desc())
    if run_type:
        stmt = stmt.where(SchedulerRun.run_type == run_type)
    if date_from:
        stmt = stmt.where(SchedulerRun.started_at >= date_from)
    if date_to:
        stmt = stmt.where(SchedulerRun.started_at <= date_to)
    # over-fetch a bit when downsampling so we still return ~limit points
    scan = limit * 5 if downsample else limit
    runs = list(s.scalars(stmt.limit(scan)))
    points: list[dict] = []
    for r in reversed(runs):  # chronological
        prog = ((r.meta or {}).get("progress_after")) or {}
        if not prog:
            continue
        ts = r.finished_at or r.started_at
        points.append({
            "run_id": r.run_id,
            "run_type": r.run_type,
            "at": ts.isoformat() if ts else None,
            "total_liked": prog.get("total_liked", 0),
            "metadata_fetched": prog.get("metadata_fetched", 0),
            "metadata_missing": prog.get("metadata_missing", 0),
            "body_saved": prog.get("body_saved", 0),
            "body_missing": prog.get("body_missing", 0),
            "retryable_liked_jobs": prog.get("retryable_liked_jobs", 0),
            "failed_liked_jobs": prog.get("failed_liked_jobs", 0),
            "partial_liked_jobs": prog.get("partial_liked_jobs", 0),
            "active_archive_jobs": prog.get("active_archive_jobs", 0),
        })
    if downsample == "daily":
        by_day: dict[str, dict] = {}
        for p in points:  # points are chronological -> last per day wins
            day = (p["at"] or "")[:10]
            by_day[day] = p
        points = list(by_day.values())
    return points[-limit:]


def cleanup_runs(
    s, *, keep_last: int = 0, older_than_days: int = 0, dry_run: bool = True, now: datetime | None = None,
) -> dict:
    """Prune old scheduler_runs. NEVER deletes jobs (job.meta.scheduler_run_id stays).

    A run is deletable when it is OLDER than ``older_than_days`` (if > 0) AND not
    among the most-recent ``keep_last`` (if > 0). With both 0, nothing is deleted.
    """
    from app.models import SchedulerRun

    now = now or _now()
    all_runs = list(s.scalars(select(SchedulerRun).order_by(SchedulerRun.id.desc())))
    total = len(all_runs)
    keep_ids: set[int] = set()
    if keep_last and keep_last > 0:
        keep_ids = {r.id for r in all_runs[:keep_last]}

    cutoff = None
    if older_than_days and older_than_days > 0:
        cutoff = now - timedelta(days=older_than_days)

    deletable = []
    for r in all_runs:
        if r.id in keep_ids:
            continue
        # if an age bound is set, only delete rows older than the cutoff
        if cutoff is not None and (r.started_at or now) >= cutoff:
            continue
        # with NO bounds at all, delete nothing (safety)
        if not keep_last and not older_than_days:
            continue
        deletable.append(r)

    deleted_run_ids = [r.run_id for r in deletable]
    if not dry_run:
        for r in deletable:
            s.delete(r)  # only the run row; jobs are untouched
        s.flush()
    return {
        "total_runs": total,
        "matched": len(deletable),
        "deleted": 0 if dry_run else len(deletable),
        "kept": total - len(deletable),
        "dry_run": dry_run,
        "keep_last": keep_last,
        "older_than_days": older_than_days,
        "deleted_run_ids": deleted_run_ids if not dry_run else [],
        "matched_run_ids": deleted_run_ids,
    }


_ENV_KEYS = {
    "scheduler_liked_archive_limit_per_run": "SCHEDULER_LIKED_ARCHIVE_LIMIT_PER_RUN",
    "scheduler_liked_retry_limit_per_run": "SCHEDULER_LIKED_RETRY_LIMIT_PER_RUN",
    "liked_archive_job_delay_seconds": "LIKED_ARCHIVE_JOB_DELAY_SECONDS",
    "scheduler_liked_suppress_when_active": "SCHEDULER_LIKED_SUPPRESS_WHEN_ACTIVE",
}


def recommend_export(rec: dict, fmt: str = "env") -> str:
    """Render a recommendation as a copy-paste .env snippet / JSON / human text.

    NEVER writes any file. Contains no secrets. Only the recommended tuning keys.
    """
    recommended = rec.get("recommended", {})
    current = rec.get("current", {})
    if fmt == "json":
        import json

        return json.dumps(
            {"recommended": recommended, "current": current, "reasons": rec.get("reasons", [])},
            indent=2,
        )

    lines: list[str] = []
    if fmt == "human":
        lines.append("# Recommended scheduler settings (suggestion only — NOT applied)")
        for key, env in _ENV_KEYS.items():
            cur = current.get(key)
            new = recommended.get(key)
            mark = "  <= CHANGE" if str(new) != str(cur) else ""
            lines.append(f"  {env}: {cur} -> {new}{mark}")
        lines.append("# reasons:")
        for r in rec.get("reasons", []):
            lines.append(f"#  - {r}")
        return "\n".join(lines)

    # fmt == "env": copy-paste-able .env block (changes commented with current)
    lines.append("# --- recommended (paste into .env, then `docker compose up -d`) ---")
    lines.append("# Suggestion only; review before applying. No secrets included.")
    for key, env in _ENV_KEYS.items():
        cur = current.get(key)
        new = recommended.get(key)
        val = "true" if new is True else "false" if new is False else new
        changed = str(new) != str(cur)
        suffix = f"   # was {cur}" if changed else "   # unchanged"
        lines.append(f"{env}={val}{suffix}")
    return "\n".join(lines)


def scheduler_stats(s, *, lookback: int = 50) -> dict:
    """Aggregate recent scheduler runs (status rates, totals, by run_type)."""
    from app.models import SchedulerRun

    runs = list(s.scalars(select(SchedulerRun).order_by(SchedulerRun.id.desc()).limit(lookback)))
    total = len(runs)
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    jobs_created = jobs_submitted = skipped_active = skipped_dup = skipped_backoff = 0
    for r in runs:
        by_type[r.run_type] = by_type.get(r.run_type, 0) + 1
        by_status[r.status] = by_status.get(r.status, 0) + 1
        jobs_created += r.jobs_created
        jobs_submitted += r.jobs_submitted
        skipped_active += r.skipped_active_jobs
        skipped_dup += r.skipped_duplicates
        skipped_backoff += r.skipped_backoff
    last = runs[0] if runs else None
    return {
        "runs_considered": total,
        "by_type": by_type,
        "by_status": by_status,
        "jobs_created": jobs_created,
        "jobs_submitted": jobs_submitted,
        "skipped_active_jobs": skipped_active,
        "skipped_duplicates": skipped_dup,
        "skipped_backoff": skipped_backoff,
        "last_run_id": last.run_id if last else None,
        "last_run_type": last.run_type if last else None,
        "last_run_status": last.status if last else None,
        "last_run_at": last.started_at.isoformat() if last else None,
    }


def _archive_outcome_rates(s, *, scan: int = 300) -> dict:
    """Outcome rates for liked-archive BODY jobs (from classification)."""
    from app.models import Job
    from app.services.job_classify import classify_job

    finished = rate_limited = incomplete = success = failed = 0
    for j in s.scalars(select(Job).where(Job.type == "download").order_by(Job.id.desc()).limit(scan)):
        meta = j.meta or {}
        if meta.get("source_action") != "liked_archive" or j.profile_name == "metadata_only":
            continue
        if j.status in ("queued", "running"):
            continue
        finished += 1
        if j.status == "success":
            success += 1
        else:
            failed += 1
        reasons = classify_job(j).get("reasons", [])
        if "rate_limited" in reasons:
            rate_limited += 1
        if "incomplete_data" in reasons:
            incomplete += 1
    return {
        "finished": finished,
        "success": success,
        "failed": failed,
        "rate_limited": rate_limited,
        "incomplete_data": incomplete,
        "success_rate": round(success / finished, 3) if finished else None,
        "throttle_rate": round((rate_limited + incomplete) / finished, 3) if finished else None,
    }


def recommend_settings(s, settings: Settings, *, lookback: int = 30) -> dict:
    """Suggest safe archive/retry limits from recent results. NEVER auto-applies."""
    rates = _archive_outcome_rates(s)
    progress = None
    try:
        from app.services import liked_archive as la

        progress = la.progress(s, settings)
    except Exception:  # noqa: BLE001
        progress = {}
    from app.services import liked_archive as la2

    active_body = la2.active_liked_archive_count(s)
    retryable = (progress or {}).get("retryable_liked_jobs", 0)

    cur_archive = settings.scheduler_liked_archive_limit_per_run
    cur_retry = settings.scheduler_liked_retry_limit_per_run
    cur_delay = settings.liked_archive_job_delay_seconds or settings.download_job_delay_seconds or 0

    rec_archive = cur_archive
    rec_retry = cur_retry
    rec_delay = cur_delay
    reasons: list[str] = []

    throttle = rates["throttle_rate"]
    success_rate = rates["success_rate"]
    if throttle is not None and throttle >= 0.3:
        rec_archive = 1
        rec_delay = max(int(cur_delay) or 0, 300)
        reasons.append(
            f"Throttle rate {int(throttle*100)}% (429/incomplete) is high → archive limit 1, "
            f"longer delay (>= {rec_delay}s)."
        )
    elif success_rate is not None and success_rate >= 0.8 and active_body == 0:
        rec_archive = min(cur_archive + 1, 5)
        reasons.append(
            f"Success rate {int(success_rate*100)}% is good and no active body jobs → "
            f"archive limit may rise to {rec_archive} (cap 5)."
        )
    else:
        reasons.append("Not enough signal to change the archive limit → keep current.")

    if active_body > 0:
        reasons.append("Active body jobs in flight → keep SUPPRESS_WHEN_ACTIVE on.")
    if retryable >= 5:
        rec_retry = min(cur_retry, 2)
        reasons.append(
            f"{retryable} retryable liked jobs → keep retry limit small ({rec_retry}); "
            f"raise DOWNLOAD_RETRY_BACKOFF_SECONDS so retries wait longer."
        )

    return {
        "based_on": {"finished_archive_jobs": rates["finished"], "retryable": retryable, "active_body_jobs": active_body},
        "rates": rates,
        "current": {
            "scheduler_liked_archive_limit_per_run": cur_archive,
            "scheduler_liked_retry_limit_per_run": cur_retry,
            "liked_archive_job_delay_seconds": cur_delay,
            "scheduler_liked_suppress_when_active": settings.scheduler_liked_suppress_when_active,
        },
        "recommended": {
            "scheduler_liked_archive_limit_per_run": rec_archive,
            "scheduler_liked_retry_limit_per_run": rec_retry,
            "liked_archive_job_delay_seconds": rec_delay,
            "scheduler_liked_suppress_when_active": True,
        },
        "reasons": reasons,
        "note": "Recommendation only — settings are NOT changed automatically.",
    }


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
