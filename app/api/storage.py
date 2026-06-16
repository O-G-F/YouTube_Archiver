"""Storage / database stats endpoint (Phase 6E)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas import DbStatsOut
from app.services import db_stats as db_stats_svc

router = APIRouter(prefix="/api/storage", tags=["storage"])


@router.get("/db-stats", response_model=DbStatsOut)
def storage_db_stats(db: Session = Depends(get_db)) -> DbStatsOut:
    """Row counts + approximate table/DB sizes (raw_json growth). No content read."""
    return DbStatsOut(**db_stats_svc.db_stats(db))
