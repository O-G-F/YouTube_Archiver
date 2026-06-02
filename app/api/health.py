"""Health endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.api.deps import get_db
from app.schemas import HealthOut
from app.services.ytdlp import ytdlp_version

router = APIRouter(tags=["health"])


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
