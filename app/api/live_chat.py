"""Live chat refresh + read endpoints (Phase 4B).

Live chat messages contain personal data: ``raw_json``/author info is NOT
returned unless ``include_raw=true``. Refresh never re-downloads the video body
(``--skip-download`` + ``--write-subs --sub-langs live_chat``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.logging_setup import get_logger
from app.models import LiveChatMessage, Video
from app.schemas import (
    JobOut,
    LiveChatMessageOut,
    LiveChatRefreshAllOut,
    LiveChatRefreshAllRequest,
    LiveChatRefreshRequest,
    LiveChatStatsOut,
)
from app.services import comment_policy
from app.services import jobs as jobs_svc
from app.services.profiles import get_profile_spec

router = APIRouter(tags=["live-chat"])
logger = get_logger(__name__)

_REFRESH_ALL_DEFAULT_LIMIT = 25


def _resolve_profile(db: Session, profile: str | None) -> str:
    name = profile or "live_chat_refresh_only"
    try:
        get_profile_spec(db, name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown profile: {name!r}")
    return name


def _submit(db: Session, job) -> None:
    db.commit()
    try:
        job.rq_job_id = jobs_svc.submit_job(job.id)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - Redis may be down
        logger.warning("live-chat: job %s not submitted to RQ: %s", job.id, exc)


@router.post("/api/live-chat/refresh", response_model=JobOut, status_code=201)
def refresh_live_chat(req: LiveChatRefreshRequest, db: Session = Depends(get_db)) -> JobOut:
    if req.has_conflict():
        raise HTTPException(status_code=400, detail="specify only one of 'target' or 'video'")
    target = req.resolved_target()
    if not target:
        raise HTTPException(
            status_code=400, detail="missing 'target' (a YouTube video id or URL)"
        )
    profile = _resolve_profile(db, req.profile)
    video = jobs_svc.resolve_or_create_video(db, target)
    if video is None:
        raise HTTPException(status_code=400, detail=f"could not resolve video: {target!r}")
    job = jobs_svc.create_live_chat_refresh_job(db, video, profile_name=profile)
    _submit(db, job)
    return JobOut.model_validate(db.get(type(job), job.id))


@router.post("/api/videos/{video_id}/live-chat/refresh", response_model=JobOut, status_code=201)
def refresh_video_live_chat(
    video_id: int, db: Session = Depends(get_db), profile: str | None = Query(default=None)
) -> JobOut:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    name = _resolve_profile(db, profile)
    job = jobs_svc.create_live_chat_refresh_job(db, video, profile_name=name)
    _submit(db, job)
    return JobOut.model_validate(db.get(type(job), job.id))


@router.post("/api/live-chat/refresh-all", response_model=LiveChatRefreshAllOut)
def refresh_all_live_chat(
    req: LiveChatRefreshAllRequest, db: Session = Depends(get_db)
) -> LiveChatRefreshAllOut:
    profile = _resolve_profile(db, req.profile)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    limit = req.limit_videos or _REFRESH_ALL_DEFAULT_LIMIT
    videos = comment_policy.select_due_live_chat_videos(db, now, limit)
    job_ids: list[int] = []
    for v in videos:
        job_ids.append(jobs_svc.create_live_chat_refresh_job(db, v, profile_name=profile).id)
    db.commit()
    for jid in job_ids:
        try:
            jobs_svc.submit_job(jid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("live-chat refresh-all: job %s not submitted: %s", jid, exc)
    return LiveChatRefreshAllOut(
        videos_selected=len(videos), jobs_created=len(job_ids), job_ids=job_ids
    )


@router.get("/api/videos/{video_id}/live-chat", response_model=list[LiveChatMessageOut])
def list_video_live_chat(
    video_id: int,
    db: Session = Depends(get_db),
    include_missing: bool = Query(default=True),
    superchats_only: bool = Query(default=False),
    include_raw: bool = Query(default=False),
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[LiveChatMessageOut]:
    if db.get(Video, video_id) is None:
        raise HTTPException(status_code=404, detail="video not found")
    stmt = select(LiveChatMessage).where(LiveChatMessage.video_id == video_id)
    if not include_missing:
        stmt = stmt.where(LiveChatMessage.is_deleted_or_missing.is_(False))
    if superchats_only:
        stmt = stmt.where(LiveChatMessage.is_superchat.is_(True))
    stmt = stmt.order_by(LiveChatMessage.timestamp_ms.asc().nullslast(), LiveChatMessage.id.asc())
    rows = list(db.scalars(stmt.limit(limit).offset(offset)))
    out = [LiveChatMessageOut.model_validate(r) for r in rows]
    if not include_raw:
        for o in out:
            o.raw_json = None
    return out


@router.get("/api/videos/{video_id}/live-chat/stats", response_model=LiveChatStatsOut)
def video_live_chat_stats(video_id: int, db: Session = Depends(get_db)) -> LiveChatStatsOut:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")

    def _count(*conds) -> int:
        return int(
            db.scalar(
                select(func.count(LiveChatMessage.id)).where(
                    LiveChatMessage.video_id == video_id, *conds
                )
            )
            or 0
        )

    total = _count()
    missing = _count(LiveChatMessage.is_deleted_or_missing.is_(True))
    superchats = _count(LiveChatMessage.is_superchat.is_(True))
    members = _count(LiveChatMessage.is_member_message.is_(True))
    distinct_authors = int(
        db.scalar(
            select(func.count(func.distinct(LiveChatMessage.author_channel_id))).where(
                LiveChatMessage.video_id == video_id
            )
        )
        or 0
    )
    return LiveChatStatsOut(
        video_id=video.id,
        youtube_video_id=video.youtube_video_id,
        total=total,
        active=total - missing,
        missing=missing,
        superchats=superchats,
        member_messages=members,
        distinct_authors=distinct_authors,
        has_live_chat=bool(video.has_live_chat),
        live_chat_state=video.live_chat_state,
        last_live_chat_refresh_at=video.last_live_chat_refresh_at,
        next_live_chat_refresh_at=video.next_live_chat_refresh_at,
    )
