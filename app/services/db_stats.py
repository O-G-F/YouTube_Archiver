"""Database size / row-count stats (Phase 6E).

Surfaces table row counts + approximate sizes so the operator can see the DB
impact of large Takeout imports (raw_json is the main growth driver). Uses
``pg_total_relation_size`` on PostgreSQL and a best-effort page-count estimate
on SQLite. NO personal content / raw_json is read or returned.
"""

from __future__ import annotations

from sqlalchemy import String, cast, func, select, text
from sqlalchemy.orm import Session

from app.models import (
    Base,
    LikedVideo,
    SearchHistoryEvent,
    TakeoutImportSession,
    Video,
    WatchHistoryEvent,
)


def _count(session: Session, table) -> int | None:
    try:
        return int(session.scalar(select(func.count()).select_from(table)) or 0)
    except Exception:  # noqa: BLE001
        return None


def _raw_not_null(session: Session, model) -> int:
    """Count rows with an ACTUAL raw blob stored.

    A no-raw-json import sets ``raw_json=None``; SQLAlchemy's JSON type renders
    Python ``None`` as the JSON literal ``null`` (not SQL ``NULL``), so a plain
    ``IS NOT NULL`` over-counts. Exclude both SQL NULL and the JSON-null literal
    (text ``'null'`` on SQLite + PostgreSQL JSONB alike).
    """
    try:
        return int(session.scalar(
            select(func.count())
            .where(model.raw_json.is_not(None))
            .where(cast(model.raw_json, String) != "null")
        ) or 0)
    except Exception:  # noqa: BLE001
        return 0


def db_stats(session: Session) -> dict:
    dialect = session.bind.dialect.name if session.bind is not None else "unknown"

    table_counts: dict[str, int | None] = {}
    for table in Base.metadata.sorted_tables:
        table_counts[table.name] = _count(session, table)

    table_sizes: dict[str, int] = {}
    total_size_bytes: int | None = None
    if dialect == "postgresql":
        try:
            for relname, size in session.execute(
                text(
                    "SELECT relname, pg_total_relation_size(relid) "
                    "FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC"
                )
            ):
                table_sizes[str(relname)] = int(size or 0)
            total_size_bytes = int(session.scalar(text("SELECT pg_database_size(current_database())")) or 0)
        except Exception:  # noqa: BLE001
            pass
    elif dialect == "sqlite":
        try:
            pc = session.execute(text("PRAGMA page_count")).scalar()
            ps = session.execute(text("PRAGMA page_size")).scalar()
            if pc and ps:
                total_size_bytes = int(pc) * int(ps)
        except Exception:  # noqa: BLE001
            pass

    raw_json_stored = {
        "liked_videos": _raw_not_null(session, LikedVideo),
        "watch_history_events": _raw_not_null(session, WatchHistoryEvent),
        "search_history_events": _raw_not_null(session, SearchHistoryEvent),
    }

    return {
        "dialect": dialect,
        "total_size_bytes": total_size_bytes,
        "total_size_mb": round(total_size_bytes / 1024 / 1024, 2) if total_size_bytes else None,
        "table_counts": table_counts,
        "table_sizes_bytes": table_sizes,
        "raw_json_stored": raw_json_stored,
        "raw_json_stored_total": sum(raw_json_stored.values()),
        "videos": table_counts.get("videos", 0) or 0,
        "liked_videos": table_counts.get("liked_videos", 0) or 0,
        "watch_history_events": table_counts.get("watch_history_events", 0) or 0,
        "search_history_events": table_counts.get("search_history_events", 0) or 0,
        "takeout_import_sessions": table_counts.get("takeout_import_sessions", 0) or 0,
    }
