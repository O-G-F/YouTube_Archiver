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
    session: Session, video: Video, liked: LikedVideo, profile: str,
    *, extra_meta: dict | None = None,
) -> Job:
    meta = {
        "enqueued_by": "liked_videos",
        "source_action": SOURCE_ACTION,
        "liked_video_id": liked.id,
        "liked_at": liked.liked_at.isoformat() if liked.liked_at else None,
        "requested_profile": profile,
    }
    if extra_meta:
        meta.update(extra_meta)
    job = Job(
        type="download",
        status="queued",
        url=video.url or canonical_video_url(video.youtube_video_id),
        video_id=video.id,
        profile_name=profile,
        priority=0,
        meta=meta,
    )
    session.add(job)
    session.flush()
    return job


def active_liked_archive_count(session: Session) -> int:
    """Count queued/running liked-archive download jobs (for the suppress brake)."""
    n = 0
    for j in session.scalars(
        select(Job).where(Job.type == "download", Job.status.in_(ACTIVE_STATUSES))
    ):
        if (j.meta or {}).get("source_action") == SOURCE_ACTION:
            n += 1
    return n


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
    extra_meta: dict | None = None,
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
        job = _create_liked_job(session, v, lv, profile, extra_meta=extra_meta)
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
    extra_meta: dict | None = None,
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
        extra_meta=extra_meta,
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
    extra_meta: dict | None = None,
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
        extra_meta=extra_meta,
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
    now: datetime | None = None,
):
    """Failed/partial liked-archive jobs that are retryable and under the cap.

    When ``now`` is given, jobs still inside their backoff window
    (``next_retry_at`` in the future) are skipped — used by the scheduler so it
    never retries before the backoff has elapsed.
    """
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
        if now is not None and j.next_retry_at is not None and j.next_retry_at > now:
            continue  # still inside the backoff window
        c = classify_job(j)
        if not c["retryable"]:
            continue
        if reason and reason not in c["reasons"]:
            continue
        out.append((j, c))
        if len(out) >= limit:
            break
    return out


def progress(session: Session, settings: Settings, *, top_channels: int = 10) -> dict:
    """Aggregate liked-archive progress (counts by state / source / channel).

    Personal data (raw_json, like history) is NOT included.
    """
    rows = session.execute(
        select(LikedVideo, Video)
        .join(Video, Video.id == LikedVideo.video_id, isouter=True)
        .where(LikedVideo.youtube_video_id.is_not(None))
    ).all()
    video_ids = [v.id for _lv, v in rows if v is not None]
    body_map = _body_count_map(session, video_ids)
    meta_map = _meta_count_map(session, video_ids)

    seen: set[str] = set()
    total = meta_fetched = body_saved = 0
    by_source: dict[str, int] = {}
    by_channel: dict[str, int] = {}
    earliest = latest = None
    for lv, v in rows:
        by_source[lv.source or "unknown"] = by_source.get(lv.source or "unknown", 0) + 1
        if lv.liked_at:
            earliest = lv.liked_at if earliest is None or lv.liked_at < earliest else earliest
            latest = lv.liked_at if latest is None or lv.liked_at > latest else latest
        vid = lv.youtube_video_id
        if vid in seen:
            continue
        seen.add(vid)
        total += 1
        has_meta = bool(v is not None and (v.title or meta_map.get(v.id, 0) > 0))
        has_body = bool(v is not None and body_map.get(v.id, 0) > 0)
        if has_meta:
            meta_fetched += 1
        if has_body:
            body_saved += 1
        ch = (v.channel_title if v is not None and v.channel_title else lv.channel_title) or "—"
        by_channel[ch] = by_channel.get(ch, 0) + 1

    # liked-archive job stats (scan recent download jobs)
    active_archive = failed = partial = 0
    last_archive_at = last_success_at = None
    for j in session.scalars(
        select(Job).where(Job.type == "download").order_by(Job.id.desc()).limit(5000)
    ):
        if (j.meta or {}).get("source_action") != SOURCE_ACTION:
            continue
        ts = j.finished_at or j.created_at
        if last_archive_at is None:
            last_archive_at = ts
        if j.status in ACTIVE_STATUSES and j.profile_name != METADATA_PROFILE:
            active_archive += 1
        elif j.status == "failed":
            failed += 1
        elif j.status == "partial_success":
            partial += 1
        elif j.status == "success" and last_success_at is None:
            last_success_at = ts

    retryable = len(retryable_liked(session, settings, limit=10_000))
    top = sorted(by_channel.items(), key=lambda kv: kv[1], reverse=True)[:top_channels]
    return {
        "total_liked": total,
        "metadata_fetched": meta_fetched,
        "metadata_missing": total - meta_fetched,
        "body_saved": body_saved,
        "body_missing": total - body_saved,
        "active_archive_jobs": active_archive,
        "retryable_liked_jobs": retryable,
        "failed_liked_jobs": failed,
        "partial_liked_jobs": partial,
        "by_source": by_source,
        "by_channel": [{"channel": c, "count": n} for c, n in top],
        "earliest_liked_at": earliest.isoformat() if earliest else None,
        "latest_liked_at": latest.isoformat() if latest else None,
        "last_archive_job_at": last_archive_at.isoformat() if last_archive_at else None,
        "last_successful_archive_at": last_success_at.isoformat() if last_success_at else None,
    }


def retry_failed_liked(
    session: Session,
    settings: Settings,
    *,
    reason: str | None = None,
    limit: int = 20,
    now: datetime | None = None,
    submit: bool = True,
) -> list[int]:
    """Re-queue retryable liked-archive jobs (respects the attempt cap + backoff)."""
    candidates = retryable_liked(session, settings, reason=reason, limit=limit, now=now)
    job_ids: list[int] = []
    for j, _c in candidates:
        jobs_svc.retry_job(session, j)
        job_ids.append(j.id)
    session.commit()
    if submit:
        for jid in job_ids:
            try:
                jobs_svc.submit_job(jid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("retry-failed liked: job %s not resubmitted: %s", jid, exc)
    return job_ids
