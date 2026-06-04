"""Liked-videos library endpoints (Phase 6A).

Liked videos are personal data: ``raw_json`` is NOT returned unless
``include_raw=true``. Enqueueing metadata uses ``metadata_only`` (no body DL).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.logging_setup import get_logger
from app.models import LikedVideo, Video
from app.schemas import (
    LikedVideoOut,
    LikedVideosEnqueueOut,
    LikedVideosEnqueueRequest,
    LikedVideoStatsOut,
)
from app.services import jobs as jobs_svc
from app.services.profiles import get_profile_spec

router = APIRouter(prefix="/api/liked-videos", tags=["liked-videos"])
logger = get_logger(__name__)


@router.get("", response_model=list[LikedVideoOut])
def list_liked_videos(
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, description="search title/channel/id"),
    only_missing_metadata: bool = Query(default=False),
    include_raw: bool = Query(default=False),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[LikedVideoOut]:
    stmt = select(LikedVideo, Video).join(
        Video, Video.id == LikedVideo.video_id, isouter=True
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (LikedVideo.title.ilike(like))
            | (LikedVideo.channel_title.ilike(like))
            | (LikedVideo.youtube_video_id.ilike(like))
        )
    stmt = stmt.order_by(
        LikedVideo.liked_at.desc().nullslast(), LikedVideo.id.desc()
    )
    rows = db.execute(stmt.limit(limit).offset(offset)).all()
    out: list[LikedVideoOut] = []
    for lv, video in rows:
        fetched = bool(video is not None and video.title)
        if only_missing_metadata and fetched:
            continue
        o = LikedVideoOut.model_validate(lv)
        o.metadata_fetched = fetched
        # prefer the enriched Video title/channel for display when present
        if video is not None:
            o.title = video.title or o.title
            o.channel_title = video.channel_title or o.channel_title
        if not include_raw:
            o.raw_json = None
        out.append(o)
    return out


@router.get("/stats", response_model=LikedVideoStatsOut)
def liked_videos_stats(db: Session = Depends(get_db)) -> LikedVideoStatsOut:
    total = int(db.scalar(select(func.count(LikedVideo.id))) or 0)
    with_vid = int(
        db.scalar(select(func.count(LikedVideo.id)).where(LikedVideo.youtube_video_id.is_not(None)))
        or 0
    )
    linked = int(
        db.scalar(select(func.count(LikedVideo.id)).where(LikedVideo.video_id.is_not(None))) or 0
    )
    fetched = int(
        db.scalar(
            select(func.count(LikedVideo.id))
            .join(Video, Video.id == LikedVideo.video_id)
            .where(Video.title.is_not(None))
        )
        or 0
    )
    earliest = db.scalar(select(func.min(LikedVideo.liked_at)))
    latest = db.scalar(select(func.max(LikedVideo.liked_at)))
    return LikedVideoStatsOut(
        total=total,
        with_video_id=with_vid,
        linked_videos=linked,
        metadata_fetched=fetched,
        earliest=earliest,
        latest=latest,
    )


@router.post("/enqueue-metadata", response_model=LikedVideosEnqueueOut)
def enqueue_metadata(
    req: LikedVideosEnqueueRequest, db: Session = Depends(get_db)
) -> LikedVideosEnqueueOut:
    """Enqueue metadata_only jobs for liked videos (default: those missing metadata).

    Uses ``metadata_only`` so the video BODY is never downloaded.
    """
    profile = req.profile or "metadata_only"
    try:
        get_profile_spec(db, profile)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown profile: {profile!r}")

    stmt = (
        select(LikedVideo, Video)
        .join(Video, Video.id == LikedVideo.video_id, isouter=True)
        .where(LikedVideo.youtube_video_id.is_not(None))
        .order_by(LikedVideo.liked_at.desc().nullslast(), LikedVideo.id.desc())
    )
    rows = db.execute(stmt).all()
    job_ids: list[int] = []
    selected = 0
    seen: set[str] = set()
    for lv, video in rows:
        if req.limit is not None and selected >= req.limit:
            break
        if req.only_missing_metadata and video is not None and video.title:
            continue
        vid = lv.youtube_video_id
        if not vid or vid in seen:
            continue
        seen.add(vid)
        selected += 1
        v = jobs_svc.resolve_or_create_video(db, vid)
        if v is None:
            continue
        job = jobs_svc.create_job_for_url(
            db, v.url, profile, extra_meta={"enqueued_by": "liked_videos"}
        )
        job_ids.append(job.id)

    db.commit()
    submitted = 0
    for jid in job_ids:
        try:
            jobs_svc.submit_job(jid)
            submitted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("liked-videos enqueue: job %s not submitted: %s", jid, exc)
    return LikedVideosEnqueueOut(
        videos_selected=selected, jobs_created=len(job_ids), job_ids=job_ids
    )
