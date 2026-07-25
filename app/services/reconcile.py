"""Phase 8C: orphan download-job detection & repair (runtime robustness).

A worker crash / host sleep / stack restart can leave a job ``running`` in the
DB while its RQ job is gone (not in the queue or the StartedJobRegistry). Such a
job never progresses. This module finds those orphans and (on ``apply``) safely
re-queues them — WITHOUT re-downloading anything already saved.

Safety:
  * A job is an orphan ONLY if its ``rq_job_id`` is absent from RQ (queue +
    started/scheduled/deferred registries) AND it is older than a threshold
    (default 30 min) so we never touch a job a worker just picked up.
  * If Redis is unreadable we take NO action (can't tell — conservative).
  * If the video already has a body media file, we DON'T re-download: the job is
    reconciled to ``success``. yt-dlp's ``--download-archive`` also prevents
    duplicate downloads if a re-queue does run.
  * Never touches permanent handling, secrets, or raw_json.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.logging_setup import get_logger
from app.models import Job, MediaFile, utcnow

logger = get_logger(__name__)

_ACTIVE_STATUSES = ("queued", "running")
_BODY_MEDIA_TYPES = ("video", "audio")


def rq_present_db_job_ids(settings: Settings | None = None) -> set[int] | None:
    """rq_job_ids RQ currently knows about (queued + started/scheduled/deferred).

    Returns the set of DB job_ids RQ currently references, or ``None`` if RQ
    can't be read (caller must then take no action).

    IMPORTANT: matches by the **DB job_id embedded in each RQ job's args**, NOT
    by ``Job.rq_job_id`` — some enqueue paths don't persist rq_job_id, so a
    UUID-based match would flag every job as an orphan (false positive).
    """
    settings = settings or get_settings()
    try:
        from rq import Queue
        from rq.job import Job as RQJob
        from rq.registry import (
            DeferredJobRegistry,
            ScheduledJobRegistry,
            StartedJobRegistry,
        )

        from app.worker.queue import get_redis

        r = get_redis()
        q = Queue(settings.rq_queue, connection=r)
        rq_uuids: list[str] = list(q.job_ids)
        for Reg in (StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry):
            try:
                rq_uuids += list(Reg(settings.rq_queue, connection=r).get_job_ids())
            except Exception:  # noqa: BLE001 - a missing registry is non-fatal
                pass
        db_ids: set[int] = set()
        for rid in rq_uuids:
            try:
                rq_job = RQJob.fetch(rid, connection=r)
                if rq_job.args:  # run_job(db_job_id) — first arg is the DB job id
                    db_ids.add(int(rq_job.args[0]))
            except Exception:  # noqa: BLE001 - a vanished job is just not present
                continue
        return db_ids
    except Exception as exc:  # noqa: BLE001 - Redis down / rq missing
        logger.warning("reconcile: cannot read RQ registries: %s", exc)
        return None


def _has_body(session: Session, video_id: int | None) -> bool:
    if video_id is None:
        return False
    n = session.scalar(
        select(func.count(MediaFile.id)).where(
            MediaFile.video_id == video_id,
            MediaFile.media_type.in_(_BODY_MEDIA_TYPES),
        )
    )
    return bool(n and n > 0)


def reconcile_orphans(
    session: Session,
    settings: Settings | None = None,
    *,
    apply: bool = False,
    older_than_minutes: int = 30,
    now: datetime | None = None,
    rq_ids: set[int] | None | bool = False,
) -> dict:
    """Find (and optionally repair) orphaned download jobs.

    ``rq_ids`` is the set of DB job_ids currently referenced by RQ. It may be
    passed in (a set of ints, or None for 'unreadable') to make the function
    deterministic in tests; the sentinel ``False`` means 'read from RQ'.
    """
    settings = settings or get_settings()
    now = now or utcnow()
    cutoff = now - timedelta(minutes=max(0, older_than_minutes))
    if rq_ids is False:  # sentinel: read live
        rq_ids = rq_present_db_job_ids(settings)
    rq_unreadable = rq_ids is None

    res = {
        "scanned": 0,
        "orphan_found": 0,
        "requeued": 0,
        "skipped_already_has_body": 0,
        "skipped_recent": 0,
        "skipped_rq_present": 0,
        "errors": 0,
        "apply": bool(apply),
        "older_than_minutes": older_than_minutes,
        "rq_unreadable": rq_unreadable,
        "orphan_job_ids": [],
    }

    stmt = select(Job).where(Job.type == "download", Job.status.in_(_ACTIVE_STATUSES))
    for j in session.scalars(stmt):
        res["scanned"] += 1
        if rq_unreadable:
            # Can't determine presence — never act (avoid double-enqueue).
            res["skipped_rq_present"] += 1
            continue
        if j.id in rq_ids:  # RQ still references this DB job (by args) — not an orphan
            res["skipped_rq_present"] += 1
            continue
        ts = j.started_at or j.created_at
        if ts is not None and ts > cutoff:
            res["skipped_recent"] += 1  # just picked up / mid-transition — leave it
            continue
        # ---- ORPHAN: in DB active, absent from RQ, older than threshold ----
        res["orphan_found"] += 1
        res["orphan_job_ids"].append(j.id)
        try:
            if _has_body(session, j.video_id):
                # already archived — do NOT re-download; reconcile to success.
                res["skipped_already_has_body"] += 1
                if apply:
                    j.status = "success"
                    j.finished_at = now
            else:
                res["requeued"] += 1
                if apply:
                    from app.services import jobs as jobs_svc

                    j.status = "queued"
                    j.started_at = None
                    session.flush()
                    j.rq_job_id = jobs_svc.submit_job(j.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconcile: job %s repair error: %s", j.id, exc)
            res["errors"] += 1

    if apply:
        session.commit()
    return res


def duplicate_video_media(session: Session) -> list[dict]:
    """Videos that have MORE THAN ONE 'video' media file (should be 0)."""
    rows = session.execute(
        select(MediaFile.video_id, func.count(MediaFile.id))
        .where(MediaFile.media_type == "video")
        .group_by(MediaFile.video_id)
        .having(func.count(MediaFile.id) > 1)
    ).all()
    return [{"video_id": vid, "count": int(n)} for vid, n in rows]


def orphan_warning_count(session: Session, settings: Settings | None = None,
                         *, older_than_minutes: int = 30) -> int:
    """Cheap count of orphaned jobs for preflight (dry-run, no mutation)."""
    try:
        r = reconcile_orphans(session, settings, apply=False,
                              older_than_minutes=older_than_minutes)
        return int(r["orphan_found"])
    except Exception as exc:  # noqa: BLE001 - preflight must never crash
        logger.warning("reconcile: orphan_warning_count failed: %s", exc)
        return 0
