"""Comment refresh + read endpoints (Phase 4A).

Comments may contain personal data: ``raw_json`` is NOT returned unless
``include_raw=true``. Refresh never re-downloads the video body.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.logging_setup import get_logger
from app.models import Comment, MetadataSnapshot, Video
from app.schemas import (
    AuthorCount,
    CommentOut,
    CommentsDueOut,
    CommentsDueVideoOut,
    CommentsRefreshAllOut,
    CommentsRefreshAllRequest,
    CommentsRefreshRequest,
    CommentStatsOut,
    JobOut,
    MetadataSnapshotOut,
)
from app.services import comment_policy
from app.services import jobs as jobs_svc
from app.services.profiles import get_profile_spec

router = APIRouter(tags=["comments"])
logger = get_logger(__name__)

# Safety cap when no explicit limit is given to refresh-all (avoids enqueuing
# thousands of jobs by accident).
_REFRESH_ALL_DEFAULT_LIMIT = 50


def _resolve_profile(db: Session, profile: str | None) -> str:
    name = profile or "comments_refresh_only"
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
        logger.warning("comments: job %s not submitted to RQ: %s", job.id, exc)


@router.post("/api/comments/refresh", response_model=JobOut, status_code=201)
def refresh_comments(req: CommentsRefreshRequest, db: Session = Depends(get_db)) -> JobOut:
    # Accept `target` (official) or `video` (compat alias), but not both.
    if req.has_conflict():
        raise HTTPException(
            status_code=400, detail="specify only one of 'target' or 'video'"
        )
    target = req.resolved_target()
    if not target:
        raise HTTPException(
            status_code=400, detail="missing 'target' (a YouTube video id or URL)"
        )
    profile = _resolve_profile(db, req.profile)
    video = jobs_svc.resolve_or_create_video(db, target)
    if video is None:
        raise HTTPException(status_code=400, detail=f"could not resolve video: {target!r}")
    job = jobs_svc.create_comments_refresh_job(db, video, profile_name=profile)
    _submit(db, job)
    return JobOut.model_validate(db.get(type(job), job.id))


@router.post("/api/videos/{video_id}/comments/refresh", response_model=JobOut, status_code=201)
def refresh_video_comments(
    video_id: int, db: Session = Depends(get_db), profile: str | None = Query(default=None)
) -> JobOut:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    name = _resolve_profile(db, profile)
    job = jobs_svc.create_comments_refresh_job(db, video, profile_name=name)
    _submit(db, job)
    return JobOut.model_validate(db.get(type(job), job.id))


@router.post("/api/comments/refresh-all", response_model=CommentsRefreshAllOut)
def refresh_all_comments(
    req: CommentsRefreshAllRequest, db: Session = Depends(get_db)
) -> CommentsRefreshAllOut:
    profile = _resolve_profile(db, req.profile)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    due_only = req.effective_due_only()
    limit = req.limit_videos or _REFRESH_ALL_DEFAULT_LIMIT
    videos = comment_policy.select_refreshable_videos(db, now, limit, due_only=due_only)
    job_ids: list[int] = []
    for v in videos:
        job_ids.append(jobs_svc.create_comments_refresh_job(db, v, profile_name=profile).id)
    db.commit()
    submitted = 0
    for jid in job_ids:
        try:
            jobs_svc.submit_job(jid)
            submitted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("comments refresh-all: job %s not submitted: %s", jid, exc)
    return CommentsRefreshAllOut(
        videos_selected=len(videos),
        jobs_created=len(job_ids),
        due_only=due_only,
        job_ids=job_ids,
    )


@router.get("/api/comments/due", response_model=CommentsDueOut)
def comments_due(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
) -> CommentsDueOut:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    videos = comment_policy.select_due_videos(db, now, limit)
    out = [
        CommentsDueVideoOut(
            video_id=v.id,
            youtube_video_id=v.youtube_video_id,
            title=v.title,
            comments_state=v.comments_state,
            last_comments_refresh_at=v.last_comments_refresh_at,
            next_comments_refresh_at=v.next_comments_refresh_at,
            due_reason="never_refreshed" if v.next_comments_refresh_at is None else "due",
        )
        for v in videos
    ]
    return CommentsDueOut(now=now, count=len(out), videos=out)


@router.get("/api/videos/{video_id}/comments", response_model=list[CommentOut])
def list_video_comments(
    video_id: int,
    db: Session = Depends(get_db),
    include_missing: bool = Query(default=True),
    include_raw: bool = Query(default=False),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[CommentOut]:
    if db.get(Video, video_id) is None:
        raise HTTPException(status_code=404, detail="video not found")
    stmt = select(Comment).where(Comment.video_id == video_id)
    if not include_missing:
        stmt = stmt.where(Comment.is_deleted_or_missing.is_(False))
    stmt = stmt.order_by(Comment.like_count.desc().nullslast(), Comment.id.asc())
    rows = list(db.scalars(stmt.limit(limit).offset(offset)))
    out = [CommentOut.model_validate(r) for r in rows]
    if not include_raw:
        for o in out:
            o.raw_json = None
    return out


@router.get("/api/videos/{video_id}/comments/stats", response_model=CommentStatsOut)
def video_comments_stats(video_id: int, db: Session = Depends(get_db)) -> CommentStatsOut:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    total = int(db.scalar(select(func.count(Comment.id)).where(Comment.video_id == video_id)) or 0)
    missing = int(
        db.scalar(
            select(func.count(Comment.id)).where(
                Comment.video_id == video_id, Comment.is_deleted_or_missing.is_(True)
            )
        )
        or 0
    )
    distinct_authors = int(
        db.scalar(
            select(func.count(func.distinct(Comment.author_channel_id))).where(
                Comment.video_id == video_id
            )
        )
        or 0
    )
    top = db.execute(
        select(Comment.author_name, func.count(Comment.id))
        .where(Comment.video_id == video_id, Comment.author_name.is_not(None))
        .group_by(Comment.author_name)
        .order_by(func.count(Comment.id).desc())
        .limit(10)
    ).all()
    return CommentStatsOut(
        video_id=video.id,
        youtube_video_id=video.youtube_video_id,
        total=total,
        active=total - missing,
        missing=missing,
        distinct_authors=distinct_authors,
        last_comments_refresh_at=video.last_comments_refresh_at,
        next_comments_refresh_at=video.next_comments_refresh_at,
        comments_state=video.comments_state,
        top_authors=[AuthorCount(author_name=a, count=n) for a, n in top],
    )


@router.get("/api/videos/{video_id}/snapshots", response_model=list[MetadataSnapshotOut])
def list_video_snapshots(
    video_id: int,
    db: Session = Depends(get_db),
    snapshot_type: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
) -> list[MetadataSnapshot]:
    if db.get(Video, video_id) is None:
        raise HTTPException(status_code=404, detail="video not found")
    stmt = select(MetadataSnapshot).where(MetadataSnapshot.video_id == video_id)
    if snapshot_type:
        stmt = stmt.where(MetadataSnapshot.snapshot_type == snapshot_type)
    return list(db.scalars(stmt.order_by(MetadataSnapshot.id.desc()).limit(limit)))
