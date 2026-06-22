"""Build / version identity + worker heartbeat (Phase 6F).

Goal: detect a *stale worker* — a worker container running older code than the
web container — BEFORE a large import is started. Each running process can
report a ``build_id`` that is stable across processes built from the same
source tree, so web and worker can be compared.

``build_id`` resolution (first hit wins):
  1. ``APP_BUILD_ID`` env (set by CI / explicit builds)
  2. a content hash of the ``app/`` Python source tree (deterministic across
     processes built from the same source; changes when code changes)

The worker publishes a short-TTL heartbeat into Redis on startup and
periodically; the web/CLI ``preflight`` reads it to confirm a worker is alive,
processes ``takeout_import``, and shares the web's ``build_id``.

NO secrets / absolute host paths are ever included in build info or heartbeats.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import socket
import time
from pathlib import Path

from app import __version__

# Redis keys --------------------------------------------------------------- #
WORKER_HEARTBEAT_PREFIX = "archiver:worker:heartbeat:"
WORKER_HEARTBEAT_TTL_SECONDS = 90
WORKER_HEARTBEAT_REFRESH_SECONDS = 30

# Job types the worker dispatcher can process (kept in sync with worker.tasks).
SUPPORTED_JOB_TYPES: tuple[str, ...] = (
    "download",
    "expand",
    "metadata_refresh",
    "comments_refresh",
    "live_chat_refresh",
    "subtitles_refresh",
    "youtube_diagnostic",
    "takeout_import",
)


def _repo_root() -> Path:
    # app/services/build_info.py -> repo root is two levels up from app/
    return Path(__file__).resolve().parent.parent.parent


def _app_dir() -> Path:
    return Path(__file__).resolve().parent.parent  # the `app` package


@functools.lru_cache(maxsize=1)
def _content_hash() -> str:
    """Stable hash of the app/ Python source tree (sorted path+content)."""
    app_dir = _app_dir()
    h = hashlib.sha256()
    try:
        files = sorted(
            p for p in app_dir.rglob("*.py") if "__pycache__" not in p.parts
        )
        for p in files:
            rel = p.relative_to(app_dir).as_posix()
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    except OSError:
        # Degrade to version-only if the tree is unreadable.
        h.update(__version__.encode("utf-8"))
    return h.hexdigest()[:16]


@functools.lru_cache(maxsize=1)
def build_id() -> str:
    env = (os.environ.get("APP_BUILD_ID") or "").strip()
    if env:
        return env
    return f"src:{_content_hash()}"


def app_version() -> str:
    return __version__


def git_commit() -> str | None:
    return (os.environ.get("APP_GIT_COMMIT") or "").strip() or None


def build_time() -> str | None:
    return (os.environ.get("APP_BUILD_TIME") or "").strip() or None


@functools.lru_cache(maxsize=1)
def code_schema_head() -> str | None:
    """The latest Alembic revision in code (the target ``head``). None if the
    Alembic config/scripts can't be located (e.g. trimmed runtime image)."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        root = _repo_root()
        scripts = root / "alembic"
        if not scripts.is_dir():
            return None
        # Build a Config WITHOUT reading alembic.ini — ScriptDirectory only needs
        # script_location, and loading the ini would reconfigure app logging.
        cfg = Config()
        cfg.set_main_option("script_location", str(scripts))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:  # noqa: BLE001 - never let identity reporting crash
        return None


def db_schema_head(session) -> str | None:
    """The Alembic revision currently applied to the DB. None when there is no
    ``alembic_version`` table (e.g. a dev/test DB created via ``create_all``)."""
    from sqlalchemy import text

    try:
        return session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001
        return None


def build_info() -> dict:
    """Process build identity. No DB / Redis access; safe everywhere."""
    return {
        "app_version": app_version(),
        "build_id": build_id(),
        "git_commit": git_commit(),
        "build_time": build_time(),
        "schema_head": code_schema_head(),
        "supported_job_types": list(SUPPORTED_JOB_TYPES),
    }


# --------------------------------------------------------------------------- #
# Worker heartbeat (Redis)
# --------------------------------------------------------------------------- #
def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def write_worker_heartbeat(redis, *, ttl: int = WORKER_HEARTBEAT_TTL_SECONDS) -> None:
    """Publish this worker's identity to Redis with a short TTL.

    Called by the worker process on startup and periodically. A dead worker's
    key auto-expires, so readers never see a stale heartbeat.
    """
    payload = {
        **build_info(),
        "worker_id": _worker_id(),
        "ts": time.time(),
    }
    redis.set(
        f"{WORKER_HEARTBEAT_PREFIX}{_worker_id()}",
        json.dumps(payload),
        ex=max(5, int(ttl)),
    )


def read_worker_heartbeats(redis) -> list[dict]:
    """Return live worker heartbeats (TTL-expired ones are already gone).

    Each entry adds ``age_seconds`` and ``stale`` (older than 2× the refresh
    interval). Never raises — returns [] if Redis is unreachable.
    """
    out: list[dict] = []
    try:
        now = time.time()
        for key in redis.scan_iter(match=f"{WORKER_HEARTBEAT_PREFIX}*"):
            raw = redis.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            ts = float(data.get("ts") or 0)
            data["age_seconds"] = round(max(0.0, now - ts), 1) if ts else None
            data["stale"] = (
                data["age_seconds"] is not None
                and data["age_seconds"] > 2 * WORKER_HEARTBEAT_REFRESH_SECONDS
            )
            out.append(data)
    except Exception:  # noqa: BLE001 - Redis may be down
        return out
    return sorted(out, key=lambda d: d.get("worker_id") or "")
