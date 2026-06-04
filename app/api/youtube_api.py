"""YouTube Data API OAuth differential sync endpoints (Phase 6B).

DEFAULT DISABLED. ``status`` never exposes secrets/paths. ``sync-liked`` returns
HTTP 200 with ``ok=false`` + a classification when OAuth is not configured (so
the UI shows guidance instead of an error).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.logging_setup import get_logger
from app.schemas import YouTubeApiStatusOut, YouTubeApiSyncOut, YouTubeApiSyncRequest
from app.services import youtube_api

router = APIRouter(prefix="/api/youtube-api", tags=["youtube-api"])
logger = get_logger(__name__)


@router.get("/status", response_model=YouTubeApiStatusOut)
def youtube_api_status() -> YouTubeApiStatusOut:
    return YouTubeApiStatusOut(**youtube_api.status_dict(get_settings()))


@router.post("/sync-liked", response_model=YouTubeApiSyncOut)
def youtube_api_sync_liked(
    req: YouTubeApiSyncRequest, db: Session = Depends(get_db)
) -> YouTubeApiSyncOut:
    settings = get_settings()
    try:
        result = youtube_api.sync_liked(
            db,
            settings,
            method=req.method,
            stop_on_existing=req.stop_on_existing,
            limit=req.limit,
            dry_run=req.dry_run,
        )
        db.commit()
        return YouTubeApiSyncOut(ok=True, **result)
    except youtube_api.YouTubeApiError as exc:
        db.rollback()
        logger.info("youtube-api sync-liked unavailable: %s", exc.classification)
        return YouTubeApiSyncOut(
            ok=False, classification=exc.classification, message=exc.message
        )
