"""Liked-videos library + bulk-archive endpoints (Phase 6A / 7C).

Liked videos are personal data: ``raw_json`` is NOT returned unless
``include_raw=true``. ``enqueue-metadata`` uses ``metadata_only`` (no body DL);
``enqueue-archive`` downloads the BODY (callers must opt in explicitly).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.logging_setup import get_logger
from app.models import Job, LikedVideo, MediaFile, Video
from app.schemas import (
    JobOutClassified,
    LikedArchiveEnqueueOut,
    LikedArchivePlanOut,
    LikedArchiveRequest,
    LikedFailureBreakdownOut,
    LikedOperationsOut,
    LikedProgressHistoryOut,
    LikedProgressHistoryPoint,
    LikedProgressOut,
    LikedRetryFailedOut,
    LikedRetryFailedRequest,
    LikedVideoOut,
    LikedVideosEnqueueOut,
    LikedVideosEnqueueRequest,
    LikedVideoStatsOut,
)
from app.schemas import JobClassification
from app.services import jobs as jobs_svc
from app.services import liked_archive as la
from app.services.job_classify import classify_job
from app.services.urls import canonical_video_url

router = APIRouter(prefix="/api/liked-videos", tags=["liked-videos"])
logger = get_logger(__name__)


def _filters(req: LikedArchiveRequest) -> la.LikedFilters:
    return la.LikedFilters(
        source=req.source,
        channel=req.channel,
        title=req.title,
        liked_after=req.liked_after,
        liked_before=req.liked_before,
        missing_metadata=req.missing_metadata,
        missing_body=req.missing_body,
    )


@router.get("", response_model=list[LikedVideoOut])
def list_liked_videos(
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, description="search title/channel/id"),
    only_missing_metadata: bool = Query(default=False),
    only_missing_body: bool = Query(default=False),
    source: str | None = Query(default=None),
    include_raw: bool = Query(default=False),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[LikedVideoOut]:
    stmt = select(LikedVideo, Video).join(
        Video, Video.id == LikedVideo.video_id, isouter=True
    )
    if source and source != "all":
        stmt = stmt.where(LikedVideo.source == source)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (LikedVideo.title.ilike(like))
            | (LikedVideo.channel_title.ilike(like))
            | (LikedVideo.youtube_video_id.ilike(like))
        )
    stmt = stmt.order_by(LikedVideo.liked_at.desc().nullslast(), LikedVideo.id.desc())
    # Over-fetch so client-side body/metadata filters still fill the page.
    rows = db.execute(stmt.limit(limit + offset + 200).offset(0)).all()

    video_ids = [v.id for _lv, v in rows if v is not None]
    body_map = la._body_count_map(db, video_ids)
    meta_map = la._meta_count_map(db, video_ids)

    # Resolve each liked row to the SAME canonical URL that archive jobs use
    # (jobs are created with video.url or canonical_video_url(id), i.e. the
    # /watch?v= form — not the youtu.be short link stored on LikedVideo).
    def _job_url(lv: LikedVideo, video: Video | None) -> str | None:
        if video is not None and video.url:
            return video.url
        if lv.youtube_video_id:
            return canonical_video_url(lv.youtube_video_id)
        return lv.url

    url_for_row = {lv.id: _job_url(lv, v) for lv, v in rows}
    urls = [u for u in url_for_row.values() if u]
    latest: dict[str, Job] = {}
    if urls:
        for j in db.scalars(
            select(Job).where(Job.url.in_(urls), Job.type == "download").order_by(Job.id.desc())
        ):
            latest.setdefault(j.url, j)

    filtered: list[LikedVideoOut] = []
    for lv, video in rows:
        bc = body_map.get(video.id, 0) if video is not None else 0
        mc = meta_map.get(video.id, 0) if video is not None else 0
        has_meta = bool(video is not None and mc > 0)  # Phase 7H: real fetched metadata (info_json)
        has_body = bc > 0
        if only_missing_metadata and has_meta:
            continue
        if only_missing_body and has_body:
            continue
        o = LikedVideoOut.model_validate(lv)
        o.metadata_fetched = has_meta
        o.has_metadata = has_meta
        o.has_body = has_body
        o.body_media_count = bc
        o.metadata_file_count = mc
        if video is not None:
            o.title = video.title or o.title
            o.channel_title = video.channel_title or o.channel_title
        ju = url_for_row.get(lv.id)
        j = latest.get(ju) if ju else None
        if j is not None:
            o.latest_archive_job_id = j.id
            o.latest_archive_job_status = j.status
            c = classify_job(j)
            o.latest_archive_classification = c.get("summary")
        if not include_raw:
            o.raw_json = None
        filtered.append(o)
    return filtered[offset : offset + limit]


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
    # Phase 7H: "metadata fetched" = a real metadata media file (info_json etc.)
    # exists for the linked Video — a Takeout title-only stub does NOT count.
    fetched = int(
        db.scalar(
            select(func.count(func.distinct(LikedVideo.id)))
            .join(Video, Video.id == LikedVideo.video_id)
            .join(MediaFile, MediaFile.video_id == Video.id)
            .where(MediaFile.media_type.in_(la.META_MEDIA_TYPES))
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


# --------------------------------------------------------------------------- #
# Phase 7C: plan / enqueue / retry
# --------------------------------------------------------------------------- #
@router.get("/progress", response_model=LikedProgressOut)
def liked_progress(db: Session = Depends(get_db)) -> LikedProgressOut:
    """Liked-archive progress dashboard data (no personal data / raw_json)."""
    return LikedProgressOut(**la.progress(db, get_settings()))


@router.get("/failure-breakdown", response_model=LikedFailureBreakdownOut)
def liked_failure_breakdown(db: Session = Depends(get_db)) -> LikedFailureBreakdownOut:
    """Phase 7H: failed/partial liked-archive jobs grouped by classification
    reason (private/deleted/unavailable/network/rate_limited/unknown). Counts only."""
    return LikedFailureBreakdownOut(**la.failure_breakdown(db))


@router.get("/progress/history", response_model=LikedProgressHistoryOut)
def liked_progress_history(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, le=500),
    run_type: str | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    downsample: str | None = Query(default=None, description="daily"),
) -> LikedProgressHistoryOut:
    """Liked-progress snapshots over time (from scheduler runs; no raw_json)."""
    from datetime import datetime

    from app.services import scheduler as scheduler_svc

    def _dt(v):
        try:
            return datetime.fromisoformat(v.replace("Z", "")) if v else None
        except ValueError:
            return None

    points = [
        LikedProgressHistoryPoint(**p)
        for p in scheduler_svc.progress_history(
            db, limit=limit, run_type=run_type,
            date_from=_dt(date_from), date_to=_dt(date_to), downsample=downsample,
        )
    ]
    return LikedProgressHistoryOut(points=points)


@router.post("/archive-plan", response_model=LikedArchivePlanOut)
def archive_plan(req: LikedArchiveRequest, request: Request, db: Session = Depends(get_db)) -> LikedArchivePlanOut:
    """Preview what an archive run would touch — no jobs are created."""
    settings = get_settings()
    plan = la.archive_plan(db, settings, filters=_filters(req), profile=req.profile, limit=req.limit)
    from app.services import audit

    audit.record_request_event(db, settings, request, event_type="archive_plan_requested",
                               category="archive", action="plan",
                               metadata={"limit": req.limit, "selected": plan.selected_count,
                                         "blocked": plan.blocked})
    return LikedArchivePlanOut(**plan.__dict__)


@router.post("/enqueue-metadata", response_model=LikedVideosEnqueueOut)
def enqueue_metadata(
    req: LikedVideosEnqueueRequest, db: Session = Depends(get_db)
) -> LikedVideosEnqueueOut:
    """Enqueue metadata_only jobs (never downloads the body). Backward compatible."""
    profile = req.profile or la.METADATA_PROFILE
    try:
        result = la.enqueue_metadata(
            db,
            get_settings(),
            filters=la.LikedFilters(missing_metadata=req.only_missing_metadata),
            limit=req.limit,
            profile=profile,
        )
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown profile: {profile!r}")
    return LikedVideosEnqueueOut(
        videos_selected=result.selected_count,
        jobs_created=result.jobs_created,
        job_ids=result.job_ids,
    )


@router.post("/enqueue-metadata-v2", response_model=LikedArchiveEnqueueOut)
def enqueue_metadata_v2(
    req: LikedArchiveRequest, db: Session = Depends(get_db)
) -> LikedArchiveEnqueueOut:
    """Richer metadata enqueue (filters + dry-run). Never downloads the body."""
    profile = req.profile or la.METADATA_PROFILE
    try:
        result = la.enqueue_metadata(
            db, get_settings(), filters=_filters(req), limit=req.limit,
            profile=profile, dry_run=req.dry_run, include_permanent=req.include_permanent,
        )
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown profile: {profile!r}")
    return LikedArchiveEnqueueOut(**result.__dict__)


@router.post("/enqueue-archive", response_model=LikedArchiveEnqueueOut)
def enqueue_archive(
    req: LikedArchiveRequest, request: Request, db: Session = Depends(get_db)
) -> LikedArchiveEnqueueOut:
    """Enqueue a BODY archive (downloads the video body with the given profile).

    WARNING: this downloads the video BODY. Use a small ``limit`` and check the
    plan first. ``dry_run=true`` creates no jobs.
    """
    settings = get_settings()
    profile = req.profile or settings.effective_body_archive_profile
    try:
        result = la.enqueue_archive(
            db, settings, filters=_filters(req), limit=req.limit,
            profile=profile, dry_run=req.dry_run,
            allow_low_disk=req.allow_low_disk, min_free_gb=req.min_free_gb,
        )
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown profile: {profile!r}")
    from app.services import audit

    audit.record_request_event(
        db, settings, request,
        event_type=("archive_enqueue_blocked" if result.blocked else "archive_enqueue_created"),
        category="archive", severity=("warning" if result.blocked else "info"),
        outcome=("blocked" if result.blocked else "success"), action="enqueue",
        reason_code=("disk_guard" if result.blocked else None),
        metadata={"profile": profile, "limit": req.limit, "dry_run": req.dry_run,
                  "jobs_created": result.jobs_created, "selected": result.selected_count},
    )
    return LikedArchiveEnqueueOut(**result.__dict__)


@router.get("/operations", response_model=LikedOperationsOut)
def liked_operations(db: Session = Depends(get_db)) -> LikedOperationsOut:
    """Phase 9A: consolidated body-archive operations snapshot (disk / queue /
    orphan / duplicate / DB size). Counts + figures only — no raw_json / paths."""
    return LikedOperationsOut(**la.operations_status(db, get_settings()))


@router.get("/retryable", response_model=list[JobOutClassified])
def liked_retryable(
    db: Session = Depends(get_db),
    reason: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
) -> list[JobOutClassified]:
    """Retryable liked-archive jobs (failed/partial, under the attempt cap)."""
    out: list[JobOutClassified] = []
    for j, c in la.retryable_liked(db, get_settings(), reason=reason, limit=limit):
        item = JobOutClassified.model_validate(j)
        item.classification = JobClassification(**c)
        out.append(item)
    return out


@router.post("/retry-failed", response_model=LikedRetryFailedOut)
def liked_retry_failed(
    req: LikedRetryFailedRequest, db: Session = Depends(get_db)
) -> LikedRetryFailedOut:
    """Re-queue retryable liked-archive jobs (respects the attempt cap)."""
    job_ids = la.retry_failed_liked(
        db, get_settings(), reason=req.reason, limit=req.limit
    )
    return LikedRetryFailedOut(retried=len(job_ids), job_ids=job_ids)
