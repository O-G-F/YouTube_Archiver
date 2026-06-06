"""Liked-videos bulk archive + throttling-aware queue (Phase 7C).

Turns imported "liked videos" (Takeout My Activity / YouTube Data API) into a
*safe, small-batch* archive workflow:

  - plan / dry-run before doing anything (counts + recommended limit/delay)
  - enqueue metadata_only (NEVER downloads the body)
  - enqueue a body archive (downloads the BODY — callers must be explicit)
  - de-duplicate against already-queued/running jobs
  - surface body/metadata state and liked-tagged retryable jobs

Every job created here is tagged in ``job.meta`` with
``source_action="liked_archive"`` + ``liked_video_id`` + ``liked_at`` +
``requested_profile`` so the UI/CLI can identify and retry them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_setup import get_logger
from app.models import Job, LikedVideo, MediaFile, Video
from app.services import jobs as jobs_svc
from app.services.job_classify import classify_job
from app.services.profiles import get_profile_spec
from app.services.urls import canonical_video_url

logger = get_logger(__name__)

BODY_MEDIA_TYPES = ("video", "audio")
META_MEDIA_TYPES = ("info_json", "description", "thumbnail", "link", "live_chat")
ACTIVE_STATUSES = ("queued", "running")
SOURCE_ACTION = "liked_archive"
METADATA_PROFILE = "metadata_only"


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
@dataclass
class LikedFilters:
    source: str | None = None  # "takeout_my_activity" | "youtube_data_api" | None/"all"
    channel: str | None = None
    title: str | None = None
    liked_after: datetime | None = None
    liked_before: datetime | None = None
    missing_metadata: bool = False
    missing_body: bool = False


def _apply_filters(stmt, f: LikedFilters):
    stmt = stmt.where(LikedVideo.youtube_video_id.is_not(None))
    if f.source and f.source != "all":
        stmt = stmt.where(LikedVideo.source == f.source)
    if f.channel:
        stmt = stmt.where(LikedVideo.channel_title.ilike(f"%{f.channel}%"))
    if f.title:
        like = f"%{f.title}%"
        stmt = stmt.where(
            LikedVideo.title.ilike(like)
            | LikedVideo.channel_title.ilike(like)
            | LikedVideo.youtube_video_id.ilike(like)
        )
    if f.liked_after:
        stmt = stmt.where(LikedVideo.liked_at >= f.liked_after)
    if f.liked_before:
        stmt = stmt.where(LikedVideo.liked_at <= f.liked_before)
    return stmt


# --------------------------------------------------------------------------- #
# Body / metadata state
# --------------------------------------------------------------------------- #
def _body_count_map(session: Session, video_ids: list[int]) -> dict[int, int]:
    if not video_ids:
        return {}
    rows = session.execute(
        select(MediaFile.video_id, func.count(MediaFile.id))
        .where(MediaFile.video_id.in_(video_ids))
        .where(MediaFile.media_type.in_(BODY_MEDIA_TYPES))
        .group_by(MediaFile.video_id)
    ).all()
    return {vid: int(n) for vid, n in rows}


def _meta_count_map(session: Session, video_ids: list[int]) -> dict[int, int]:
    if not video_ids:
        return {}
    rows = session.execute(
        select(MediaFile.video_id, func.count(MediaFile.id))
        .where(MediaFile.video_id.in_(video_ids))
        .where(MediaFile.media_type.in_(META_MEDIA_TYPES))
        .group_by(MediaFile.video_id)
    ).all()
    return {vid: int(n) for vid, n in rows}


def video_state(session: Session, video: Video | None) -> dict:
    """has_metadata / has_body / body_media_count / metadata_file_count for a video."""
    if video is None:
        return {
            "has_metadata": False,
            "has_body": False,
            "body_media_count": 0,
            "metadata_file_count": 0,
        }
    body = int(
        session.scalar(
            select(func.count(MediaFile.id)).where(
                MediaFile.video_id == video.id,
                MediaFile.media_type.in_(BODY_MEDIA_TYPES),
            )
        )
        or 0
    )
    meta = int(
        session.scalar(
            select(func.count(MediaFile.id)).where(
                MediaFile.video_id == video.id,
                MediaFile.media_type.in_(META_MEDIA_TYPES),
            )
        )
        or 0
    )
    return {
        "has_metadata": bool(video.title) or meta > 0,
        "has_body": body > 0,
        "body_media_count": body,
        "metadata_file_count": meta,
    }


def latest_archive_job(session: Session, video: Video | None, url: str | None) -> Job | None:
    """Most recent download/metadata job for this liked video (by url)."""
    target_url = (video.url if video else None) or url
    if not target_url:
        return None
    return session.scalar(
        select(Job)
        .where(Job.url == target_url, Job.type == "download")
        .order_by(Job.id.desc())
        .limit(1)
    )


# --------------------------------------------------------------------------- #
# Candidate selection (dedup by youtube_video_id)
# --------------------------------------------------------------------------- #
def _select_candidates(session: Session, f: LikedFilters, *, limit: int | None):
    """Return [(LikedVideo, Video|None, state)] after all filters, deduped by id."""
    stmt = (
        select(LikedVideo, Video)
        .join(Video, Video.id == LikedVideo.video_id, isouter=True)
        .order_by(LikedVideo.liked_at.desc().nullslast(), LikedVideo.id.desc())
    )
    stmt = _apply_filters(stmt, f)
    rows = session.execute(stmt).all()

    video_ids = [v.id for _lv, v in rows if v is not None]
    body_map = _body_count_map(session, video_ids)
    meta_map = _meta_count_map(session, video_ids)

    out: list[tuple[LikedVideo, Video | None, dict]] = []
    seen: set[str] = set()
    for lv, video in rows:
        vid = lv.youtube_video_id
        if not vid or vid in seen:
            continue
        if video is not None:
            bc = body_map.get(video.id, 0)
            mc = meta_map.get(video.id, 0)
            state = {
                "has_metadata": bool(video.title) or mc > 0,
                "has_body": bc > 0,
                "body_media_count": bc,
                "metadata_file_count": mc,
            }
        else:
            state = {"has_metadata": False, "has_body": False, "body_media_count": 0, "metadata_file_count": 0}
        if f.missing_metadata and state["has_metadata"]:
            continue
        if f.missing_body and state["has_body"]:
            continue
        seen.add(vid)
        out.append((lv, video, state))
        if limit is not None and len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Plan / dry-run
# --------------------------------------------------------------------------- #
@dataclass
class ArchivePlan:
    total_candidates: int = 0
    missing_metadata: int = 0
    missing_body: int = 0
    has_body: int = 0
    existing_active_jobs: int = 0
    existing_retryable: int = 0
    recommended_limit: int = 0
    recommended_delay_seconds: float = 0.0
    recommended_profile: str = ""
    profile: str = ""
    notes: list[str] = field(default_factory=list)


def archive_plan(
    session: Session,
    settings: Settings,
    *,
    filters: LikedFilters,
    profile: str | None = None,
    limit: int | None = None,
) -> ArchivePlan:
    """Count what an archive run WOULD touch (no jobs created)."""
    prof = profile or settings.liked_archive_default_profile
    # full filtered set, deduped by youtube id (ignore body/metadata sub-filters here)
    base = LikedFilters(
        source=filters.source,
        channel=filters.channel,
        title=filters.title,
        liked_after=filters.liked_after,
        liked_before=filters.liked_before,
    )
    cands = _select_candidates(session, base, limit=None)
    total = len(cands)
    missing_meta = sum(1 for _lv, _v, st in cands if not st["has_metadata"])
    missing_body = sum(1 for _lv, _v, st in cands if not st["has_body"])
    has_body = sum(1 for _lv, _v, st in cands if st["has_body"])

    active = 0
    for _lv, v, _st in cands:
        if v is not None and _active_job_exists(session, v.url, prof):
            active += 1

    retryable = len(retryable_liked(session, settings, reason=None, limit=10_000))

    rec_limit = min(
        limit or settings.liked_archive_default_limit,
        settings.liked_archive_max_enqueue_per_run,
        max(missing_body, 0) or settings.liked_archive_default_limit,
    )
    rec_delay = (
        settings.liked_archive_job_delay_seconds
        or settings.download_job_delay_seconds
        or 30.0
    )
    notes = [
        "Start small: archive 10-30 videos at a time, then check classification.",
        "metadata_only does NOT download the body; a video profile DOES.",
    ]
    if missing_meta:
        notes.append(f"{missing_meta} need metadata first (run enqueue-metadata).")
    return ArchivePlan(
        total_candidates=total,
        missing_metadata=missing_meta,
        missing_body=missing_body,
        has_body=has_body,
        existing_active_jobs=active,
        existing_retryable=retryable,
        recommended_limit=rec_limit,
        recommended_delay_seconds=rec_delay,
        recommended_profile=settings.liked_archive_default_profile,
        profile=prof,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #
def _active_job_exists(session: Session, url: str | None, profile: str) -> bool:
    if not url:
        return False
    n = session.scalar(
        select(func.count(Job.id)).where(
            Job.url == url,
            Job.profile_name == profile,
            Job.type == "download",
            Job.status.in_(ACTIVE_STATUSES),
        )
    )
    return bool(n and n > 0)


# --------------------------------------------------------------------------- #
# Enqueue
# --------------------------------------------------------------------------- #
@dataclass
class EnqueueResult:
    selected_count: int = 0
    jobs_created: int = 0
    skipped_existing_job: int = 0
    skipped_already_has_metadata: int = 0
    skipped_already_has_body: int = 0
    job_ids: list[int] = field(default_factory=list)
    profile: str = ""
    downloads_body: bool = False
    dry_run: bool = False


def _create_liked_job(
    session: Session, video: Video, liked: LikedVideo, profile: str
) -> Job:
    job = Job(
        type="download",
        status="queued",
        url=video.url or canonical_video_url(video.youtube_video_id),
        video_id=video.id,
        profile_name=profile,
        priority=0,
        meta={
            "enqueued_by": "liked_videos",
            "source_action": SOURCE_ACTION,
            "liked_video_id": liked.id,
            "liked_at": liked.liked_at.isoformat() if liked.liked_at else None,
            "requested_profile": profile,
        },
    )
    session.add(job)
    session.flush()
    return job


def _enqueue(
    session: Session,
    settings: Settings,
    *,
    filters: LikedFilters,
    profile: str,
    limit: int | None,
    skip_if: str,  # "metadata" | "body" | "none"
    dry_run: bool,
    downloads_body: bool,
    submit: bool = True,
) -> EnqueueResult:
    cap = settings.liked_archive_max_enqueue_per_run
    eff_limit = min(limit or settings.liked_archive_default_limit, cap)
    res = EnqueueResult(profile=profile, downloads_body=downloads_body, dry_run=dry_run)

    # Select candidates WITHOUT the missing-metadata/body sub-filters so the
    # skip counters below can report how many already had metadata/body. The
    # has-metadata / has-body skipping is done explicitly via ``skip_if``.
    sel_filters = LikedFilters(
        source=filters.source,
        channel=filters.channel,
        title=filters.title,
        liked_after=filters.liked_after,
        liked_before=filters.liked_before,
    )
    cands = _select_candidates(session, sel_filters, limit=None)
    for lv, video, state in cands:
        if res.selected_count >= eff_limit:
            break
        if skip_if == "metadata" and state["has_metadata"]:
            res.skipped_already_has_metadata += 1
            continue
        if skip_if == "body" and state["has_body"]:
            res.skipped_already_has_body += 1
            continue
        v = video or jobs_svc.resolve_or_create_video(session, lv.youtube_video_id)
        if v is None:
            continue
        if _active_job_exists(session, v.url, profile):
            res.skipped_existing_job += 1
            continue
        res.selected_count += 1
        if dry_run:
            continue
        job = _create_liked_job(session, v, lv, profile)
        res.job_ids.append(job.id)

    if not dry_run:
        res.jobs_created = len(res.job_ids)
        session.commit()
        if submit:
            for jid in res.job_ids:
                try:
                    jobs_svc.submit_job(jid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("liked archive: job %s not submitted: %s", jid, exc)
    return res


def enqueue_metadata(
    session: Session,
    settings: Settings,
    *,
    filters: LikedFilters,
    limit: int | None = None,
    profile: str = METADATA_PROFILE,
    dry_run: bool = False,
    submit: bool = True,
) -> EnqueueResult:
    """Enqueue metadata_only jobs (NEVER downloads the body)."""
    get_profile_spec(session, profile)  # validate (raises KeyError -> 400 upstream)
    # metadata jobs only make sense for those missing metadata when missing-only.
    return _enqueue(
        session,
        settings,
        filters=filters,
        profile=profile,
        limit=limit,
        skip_if="metadata" if filters.missing_metadata else "none",
        dry_run=dry_run,
        downloads_body=False,
        submit=submit,
    )


def enqueue_archive(
    session: Session,
    settings: Settings,
    *,
    filters: LikedFilters,
    limit: int | None = None,
    profile: str | None = None,
    dry_run: bool = False,
    submit: bool = True,
) -> EnqueueResult:
    """Enqueue a BODY archive (downloads the video body with the given profile)."""
    prof = profile or settings.liked_archive_default_profile
    get_profile_spec(session, prof)
    return _enqueue(
        session,
        settings,
        filters=filters,
        profile=prof,
        limit=limit,
        skip_if="body" if filters.missing_body else "none",
        dry_run=dry_run,
        downloads_body=True,
        submit=submit,
    )


# --------------------------------------------------------------------------- #
# Retryable (liked-tagged only)
# --------------------------------------------------------------------------- #
def retryable_liked(
    session: Session,
    settings: Settings,
    *,
    reason: str | None = None,
    limit: int = 50,
    scan: int = 1000,
):
    """Failed/partial liked-archive jobs that are retryable and under the cap."""
    max_attempts = settings.download_retry_max_attempts
    stmt = (
        select(Job)
        .where(Job.status.in_(("failed", "partial_success")))
        .where(Job.type == "download")
        .order_by(Job.id.desc())
        .limit(scan)
    )
    out: list[tuple[Job, dict]] = []
    for j in session.scalars(stmt):
        if (j.meta or {}).get("source_action") != SOURCE_ACTION:
            continue
        if (j.retry_count or 0) >= max_attempts:
            continue
        c = classify_job(j)
        if not c["retryable"]:
            continue
        if reason and reason not in c["reasons"]:
            continue
        out.append((j, c))
        if len(out) >= limit:
            break
    return out


def retry_failed_liked(
    session: Session,
    settings: Settings,
    *,
    reason: str | None = None,
    limit: int = 20,
) -> list[int]:
    """Re-queue retryable liked-archive jobs (respects the attempt cap)."""
    candidates = retryable_liked(session, settings, reason=reason, limit=limit)
    job_ids: list[int] = []
    for j, _c in candidates:
        jobs_svc.retry_job(session, j)
        job_ids.append(j.id)
    session.commit()
    for jid in job_ids:
        try:
            jobs_svc.submit_job(jid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("retry-failed liked: job %s not resubmitted: %s", jid, exc)
    return job_ids
