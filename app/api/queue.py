"""Queue health endpoint (Phase 7D)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas import QueueStatusOut
from app.services import queue_health

router = APIRouter(prefix="/api/queue", tags=["queue"])


@router.get("/status", response_model=QueueStatusOut)
def queue_status(db: Session = Depends(get_db)) -> QueueStatusOut:
    """Queued/running counts by type + source_action (for throttling/visibility)."""
    return QueueStatusOut(**queue_health.queue_status(db))
