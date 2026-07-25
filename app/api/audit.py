"""Phase 9E: audit trail read API (admin, no update/delete).

All routes are under ``/api/audit`` so the AuthMiddleware requires authentication.
Responses contain only pseudonyms + sanitised metadata — never raw identity,
secrets, or host paths. There is NO create/update/delete endpoint (append-only).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.schemas import AuditListOut, AuditStatsOut, AuditVerifyOut
from app.services import audit

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", ""))
    except ValueError:
        return None


@router.get("/events", response_model=AuditListOut)
def list_events(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    event_type: str | None = Query(default=None),
    category: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    request_id: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
) -> AuditListOut:
    rows = audit.list_events(db, limit=limit, offset=offset, event_type=event_type,
                             category=category, severity=severity, outcome=outcome,
                             request_id=request_id, correlation_id=correlation_id,
                             since=_dt(since), until=_dt(until))
    return AuditListOut(events=[audit.event_to_dict(e) for e in rows], limit=limit, offset=offset)


@router.get("/stats", response_model=AuditStatsOut)
def audit_stats(db: Session = Depends(get_db), days: int = Query(default=30, ge=1, le=730)) -> AuditStatsOut:
    return AuditStatsOut(**audit.stats(db, days=days))


@router.get("/verify", response_model=AuditVerifyOut)
def audit_verify(db: Session = Depends(get_db)) -> AuditVerifyOut:
    return AuditVerifyOut(**audit.verify_chain(db, get_settings()))


@router.get("/export")
def audit_export(db: Session = Depends(get_db), since: str | None = Query(default=None),
                 until: str | None = Query(default=None)) -> StreamingResponse:
    """Stream the (redacted) audit trail as JSONL, capped by AUDIT_MAX_EXPORT_EVENTS."""
    def _gen():
        for line in audit.export_events(db, get_settings(), since=_dt(since), until=_dt(until)):
            yield line + "\n"
    return StreamingResponse(_gen(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": "attachment; filename=audit.jsonl"})


@router.get("/events/{event_id}", response_model=dict)
def get_event(event_id: int, db: Session = Depends(get_db)) -> dict:
    from app.models import AuditEvent

    ev = db.get(AuditEvent, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="audit event not found")
    return audit.event_to_dict(ev)
