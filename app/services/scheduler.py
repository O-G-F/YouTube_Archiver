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
from app.services import jobs as jobs_svc

logger = get_logger(__name__)


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


def run_once(
    settings: Settings | None = None,
    *,
    reason: str = "scheduler",
    max_items: int | None = None,
) -> dict:
    """Create one expand job per crawlable collection. Returns a summary dict.

    With ``reason="scheduler"`` the run is skipped when SCHEDULER_ENABLED is
    false. A manual run (``reason="manual"``) always runs.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()

    if reason == "scheduler" and not settings.scheduler_enabled:
        _scheduler_log(settings, "skip: SCHEDULER_ENABLED is false")
        return {
            "enabled": False,
            "reason": reason,
            "collections_checked": 0,
            "jobs_created": 0,
            "submitted": 0,
            "job_ids": [],
        }

    cap = max_items if max_items is not None else settings.expand_max_items
    job_ids: list[int] = []
    checked = 0
    with session_scope() as s:
        collections = list(
            s.scalars(select(Collection).where(Collection.enabled.is_(True)))
        )
        for c in collections:
            policy = c.crawl_policy or "new_only"
            if policy == "manual":
                continue
            checked += 1
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
                logger.warning(
                    "scheduler: skipping collection %s (%s): %s", c.id, c.url, exc
                )
        s.commit()

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
        "collections_checked": checked,
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
    return {
        "enabled": settings.scheduler_enabled,
        "interval_seconds": settings.scheduler_interval_seconds,
        "enabled_collections": enabled_collections,
        "crawlable_collections": crawlable,
    }
