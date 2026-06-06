"""Queue health snapshot (Phase 7D).

Reports queued/running counts grouped by job type and by ``source_action`` so
the scheduler / UI can throttle new enqueues while a batch is in flight. Worker
count is best-effort (requires Redis); ``None`` when unavailable.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job


def _source_action(job: Job) -> str:
    meta = job.meta or {}
    return (
        meta.get("source_action")
        or meta.get("scheduled_by")
        or meta.get("enqueued_by")
        or "manual"
    )


def queue_status(session: Session) -> dict:
    queued = running = 0
    by_type: dict[str, int] = {}
    by_source_action: dict[str, int] = {}
    oldest_queued_at = None
    oldest_queued_job_id = None

    for j in session.scalars(
        select(Job).where(Job.status.in_(("queued", "running"))).order_by(Job.id.asc())
    ):
        if j.status == "queued":
            queued += 1
        else:
            running += 1
        by_type[j.type] = by_type.get(j.type, 0) + 1
        sa = _source_action(j)
        by_source_action[sa] = by_source_action.get(sa, 0) + 1
        if j.status == "queued" and oldest_queued_at is None:
            oldest_queued_at = j.created_at
            oldest_queued_job_id = j.id

    return {
        "queued": queued,
        "running": running,
        "total_active": queued + running,
        "by_type": by_type,
        "by_source_action": by_source_action,
        "oldest_queued_at": oldest_queued_at.isoformat() if oldest_queued_at else None,
        "oldest_queued_job_id": oldest_queued_job_id,
        "worker_count": _worker_count(),
    }


def _worker_count() -> int | None:
    """Best-effort RQ worker count (None if Redis/RQ is unavailable)."""
    try:
        from rq import Worker

        from app.worker.queue import get_queue

        queue = get_queue()
        return Worker.count(queue=queue)
    except Exception:  # noqa: BLE001 - Redis down / RQ API change
        return None
