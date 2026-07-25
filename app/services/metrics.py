"""Phase 9E: operational metrics (Prometheus text exposition).

Counts/gauges only — NO high-cardinality labels (no identity, video/channel id,
full URL, host path, or secret). Every metric is defensive so a scrape never
fails the endpoint. Served under an auth-required route.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AuditEvent, utcnow


def _fmt(name: str, value, mtype: str = "gauge", help_text: str = "") -> list[str]:
    out = []
    if help_text:
        out.append(f"# HELP {name} {help_text}")
    out.append(f"# TYPE {name} {mtype}")
    out.append(f"{name} {value}")
    return out


def _auth_counts(session: Session, *, hours: int = 24) -> dict:
    since = utcnow() - timedelta(hours=hours)
    rows = session.execute(
        select(AuditEvent.event_type, func.count(AuditEvent.id))
        .where(AuditEvent.occurred_at >= since, AuditEvent.category == "auth")
        .group_by(AuditEvent.event_type)
    ).all()
    return {str(k): int(n) for k, n in rows}


def render_prometheus(session: Session, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    lines: list[str] = []

    from app.services import audit
    from app.services import build_info as bi
    from app.services import db_stats as dbs
    from app.services import liked_archive as la
    from app.services import queue_health, reconcile, storage

    try:
        q = queue_health.queue_status(session)
        lines += _fmt("ytarch_jobs_active", int(q.get("total_active") or 0), help_text="active jobs")
        lines += _fmt("ytarch_jobs_queued", int(q.get("queued") or 0))
        lines += _fmt("ytarch_jobs_running", int(q.get("running") or 0))
        lines += _fmt("ytarch_worker_count", int(q.get("worker_count") or 0))
    except Exception:  # noqa: BLE001
        pass

    try:
        workers = bi.read_worker_heartbeats(_redis_or_none())
        ages = [w.get("age_seconds") for w in workers if w.get("age_seconds") is not None]
        lines += _fmt("ytarch_worker_heartbeat_age_seconds", round(min(ages), 1) if ages else -1)
    except Exception:  # noqa: BLE001
        lines += _fmt("ytarch_worker_heartbeat_age_seconds", -1)

    try:
        lines += _fmt("ytarch_orphan_jobs", int(reconcile.orphan_warning_count(session, settings)))
    except Exception:  # noqa: BLE001
        pass
    try:
        lines += _fmt("ytarch_duplicate_media_files", len(reconcile.duplicate_video_media(session)))
    except Exception:  # noqa: BLE001
        pass

    try:
        disk = storage.disk_usage(settings)
        lines += _fmt("ytarch_archive_disk_free_bytes", int(disk.get("free_bytes") or 0))
    except Exception:  # noqa: BLE001
        pass

    try:
        prog = la.progress(session, settings)
        lines += _fmt("ytarch_archive_body_saved", int(prog.get("body_saved") or 0))
        lines += _fmt("ytarch_archive_remaining_eligible", int(prog.get("eligible_missing_body") or 0))
    except Exception:  # noqa: BLE001
        pass

    try:
        stats = dbs.db_stats(session)
        lines += _fmt("ytarch_raw_json_stored_total", int(stats.get("raw_json_stored_total") or 0))
        cb = (stats.get("table_sizes_bytes") or {}).get("comments")
        lines += _fmt("ytarch_comments_table_bytes", int(cb) if cb else 0)
    except Exception:  # noqa: BLE001
        pass

    lines += _fmt("ytarch_redis_available", 1 if _redis_ok() else 0)

    try:
        total = int(session.scalar(select(func.count(AuditEvent.id))) or 0)
        lines += _fmt("ytarch_audit_events_total", total, mtype="counter")
        chain = audit.verify_chain(session, settings)
        lines += _fmt("ytarch_audit_chain_valid", 1 if chain.get("valid") else 0)
        ac = _auth_counts(session)
        lines += _fmt("ytarch_auth_login_success_total", ac.get("login_success", 0), mtype="counter")
        lines += _fmt("ytarch_auth_login_failure_total", ac.get("login_failure", 0), mtype="counter")
        lines += _fmt("ytarch_auth_rate_limited_total", ac.get("login_rate_limited", 0), mtype="counter")
        lines += _fmt("ytarch_csrf_rejected_total", ac.get("csrf_rejected", 0), mtype="counter")
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(lines) + "\n"


def _redis_or_none():
    try:
        from app.worker.queue import get_redis

        return get_redis()
    except Exception:  # noqa: BLE001
        return None


def _redis_ok() -> bool:
    try:
        from app.worker.queue import get_redis

        get_redis().ping()
        return True
    except Exception:  # noqa: BLE001
        return False
