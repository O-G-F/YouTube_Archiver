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
    """Explicit application version (Phase 10A). ``APP_VERSION`` overrides the
    package ``__version__`` so a release can stamp ``vX.Y.Z`` while development
    keeps ``0.0.0-dev`` / the package default."""
    return (os.environ.get("APP_VERSION") or "").strip() or __version__


def git_commit() -> str | None:
    return (os.environ.get("APP_GIT_COMMIT") or "").strip() or None


def build_time() -> str | None:
    return (os.environ.get("APP_BUILD_TIME") or "").strip() or None


def git_tree_clean() -> bool | None:
    """Whether the source tree was clean at build time (Phase 10A).

    In a built image this comes from the ``APP_GIT_TREE_CLEAN`` build arg (1/0);
    running from a source checkout it is computed from git. None when unknown
    (no marker and not a git checkout). Never returns paths."""
    env = (os.environ.get("APP_GIT_TREE_CLEAN") or "").strip().lower()
    if env in ("1", "true", "yes", "clean"):
        return True
    if env in ("0", "false", "no", "dirty"):
        return False
    try:
        import subprocess

        root = _repo_root()
        if not (root / ".git").exists():
            return None
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip() == ""
    except Exception:  # noqa: BLE001 - never let identity reporting crash
        return None


@functools.lru_cache(maxsize=1)
def frontend_build_id() -> str | None:
    """Stable id for the built SPA bundle (Phase 10A).

    ``APP_FRONTEND_BUILD_ID`` overrides; otherwise hash the built
    ``frontend/dist`` (its asset filenames are content-hashed by Vite, so this
    changes whenever the UI changes). None when no built bundle is present."""
    env = (os.environ.get("APP_FRONTEND_BUILD_ID") or "").strip()
    if env:
        return env
    dist = _repo_root() / "frontend" / "dist"
    if not dist.is_dir():
        return None
    try:
        h = hashlib.sha256()
        for p in sorted(dist.rglob("*")):
            if p.is_file():
                h.update(p.relative_to(dist).as_posix().encode("utf-8"))
                h.update(b"\0")
                h.update(p.read_bytes())
                h.update(b"\0")
        return f"ui:{h.hexdigest()[:16]}"
    except OSError:
        return None


def image_digest() -> str | None:
    """The published container image digest, when the deploy/build recorded it
    via ``APP_IMAGE_DIGEST`` (a registry ``sha256:…`` ref). None otherwise."""
    return (os.environ.get("APP_IMAGE_DIGEST") or "").strip() or None


def version_info() -> dict:
    """Consolidated release/version identity (Phase 10A). Scalars only — no host
    paths, repo paths, usernames, secrets, or raw environment values."""
    return {
        "app_version": app_version(),
        "git_commit": git_commit(),
        "git_tree_clean": git_tree_clean(),
        "build_id": build_id(),
        "build_timestamp": build_time(),
        "schema_head": code_schema_head(),
        "frontend_build_id": frontend_build_id(),
        "image_digest": image_digest(),
    }


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


def worker_convergence(redis, *, web_build_id: str | None = None) -> dict:
    """Phase 9F.2: has the worker fleet converged on the CURRENT web build?

    After ``deploy up -d`` recreates the containers, a just-stopped worker's
    heartbeat lingers in Redis until its TTL expires. Its build_id differs from
    the freshly built web, so ``worker_build_match`` transiently FAILs even
    though the real containers are healthy. A deploy should WAIT for that stale
    registration to expire rather than fail — but must still fail on a genuine,
    persistent build mismatch or a missing worker.

    Ready ⇔ at least one ACTIVE (non-stale) worker reports the current build AND
    NO worker (stale or fresh) reports a different build — i.e. every old
    registration is gone, so the subsequent preflight will also pass.

    Returns COUNTS only (never worker ids / hostnames / redis url / paths):
    ``ready``, ``reason``, ``web_build_id``, ``worker_count``,
    ``active_current_count``, ``mismatched_fresh_count``,
    ``mismatched_stale_count``, ``stale_current_count``.
    """
    if web_build_id is None:
        web_build_id = build_info()["build_id"]
    workers = read_worker_heartbeats(redis)
    active_current = [w for w in workers if not w.get("stale") and w.get("build_id") == web_build_id]
    mismatched_fresh = [w for w in workers if not w.get("stale") and w.get("build_id") != web_build_id]
    mismatched_stale = [w for w in workers if w.get("stale") and w.get("build_id") != web_build_id]
    stale_current = [w for w in workers if w.get("stale") and w.get("build_id") == web_build_id]

    if not workers:
        reason = "no_workers"
    elif not active_current:
        reason = "no_active_current_worker"
    elif mismatched_fresh:
        reason = "mismatched_fresh_present"  # a genuine live mismatch — waiting won't fix a persistent one
    elif mismatched_stale:
        reason = "mismatched_stale_present"  # old registration still expiring — wait it out
    else:
        reason = "converged"

    return {
        "ready": reason == "converged",
        "reason": reason,
        "web_build_id": web_build_id,
        "worker_count": len(workers),
        "active_current_count": len(active_current),
        "mismatched_fresh_count": len(mismatched_fresh),
        "mismatched_stale_count": len(mismatched_stale),
        "stale_current_count": len(stale_current),
    }
