"""Health endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import get_db
from app.config import get_settings
from app.schemas import HealthOut
from app.services.ytdlp import ytdlp_version

router = APIRouter(tags=["health"])


@router.get("/health")
def health_root() -> dict:
    """Bare liveness endpoint (load-balancer / uptime-probe convention).

    This is an explicit route so ``/health`` returns 200 even when the built SPA
    is absent (CI / a fresh checkout before `npm run build`) — the auth
    middleware already treats it as public. No DB/Redis, no internals leaked."""
    return {"status": "ok"}


@router.get("/health/live")
def health_live() -> dict:
    """Liveness: is the process able to answer? No DB/Redis, no internals leaked."""
    return {"status": "ok"}


@router.get("/health/ready")
def health_ready(response: Response, db: Session = Depends(get_db)) -> dict:
    """Readiness: DB + Redis + disk reachable. 503 when not ready. No detailed
    failure reasons are exposed to unauthenticated callers (use preflight for detail)."""
    ready = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        ready = False
    try:
        from app.worker.queue import get_redis

        get_redis().ping()
    except Exception:  # noqa: BLE001
        ready = False
    try:
        from app.services import storage

        disk = storage.disk_usage(get_settings())
        if disk.get("readable") and (disk.get("free_gb") or 0) <= 0:
            ready = False
    except Exception:  # noqa: BLE001
        pass
    if not ready:
        response.status_code = 503
    return {"ready": ready, "status": "ready" if ready else "not_ready"}


@router.get("/api/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False

    redis_ok = False
    try:
        from app.worker.queue import get_redis

        get_redis().ping()
        redis_ok = True
    except Exception:  # noqa: BLE001
        redis_ok = False

    return HealthOut(
        status="ok" if db_ok else "degraded",
        version=__version__,
        ytdlp_version=ytdlp_version(),
        database=db_ok,
        redis=redis_ok,
    )
