"""Google Takeout preview / import endpoints (Phase 3A).

Reads a ZIP already placed under ``TAKEOUT_IMPORT_ROOT`` (no upload yet).
The path is validated against the import root (path-traversal protection) and
members are read in-memory (no extraction; zip-slip guarded).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.schemas import (
    TakeoutImportOut,
    TakeoutImportRequest,
    TakeoutPreviewOut,
    TakeoutPreviewRequest,
)
from app.services import takeout

router = APIRouter(prefix="/api/takeout", tags=["takeout"])


@router.post("/preview", response_model=TakeoutPreviewOut)
def takeout_preview(req: TakeoutPreviewRequest) -> TakeoutPreviewOut:
    settings = get_settings()
    try:
        zip_path = takeout.resolve_takeout_path(settings, req.path)
        with takeout.open_archive(zip_path) as archive:
            preview = archive.preview(sample=5)
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TakeoutPreviewOut(path=req.path, **preview)


@router.post("/import", response_model=TakeoutImportOut)
def takeout_import(
    req: TakeoutImportRequest, db: Session = Depends(get_db)
) -> TakeoutImportOut:
    settings = get_settings()
    try:
        result = takeout.run_import(
            db, settings, req.path, limit=req.limit, dry_run=req.dry_run
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return TakeoutImportOut(**result)
