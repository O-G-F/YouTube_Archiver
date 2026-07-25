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

from collections.abc import Callable
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


def _media_type_video_ids(session: Session, video_ids: list[int], media_type: str) -> set[int]:
    """Set of video ids (within video_ids) that have >=1 media of ``media_type``.

    Used by progress() to separate broad "any metadata media" (info_json OR
    description OR ...) from the rigorous "info_json complete" count.
    """
    if not video_ids:
        return set()
    rows = session.execute(
        select(MediaFile.video_id)
        .where(MediaFile.video_id.in_(video_ids))
        .where(MediaFile.media_type == media_type)
        .distinct()
    ).all()
    return {vid for (vid,) in rows}


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
        # Phase 7H: "metadata fetched" means a real metadata MEDIA file (info_json
        # etc.) exists — a Takeout title-only stub is NOT fetched metadata, so it
        # is correctly selected by --missing-only for the first metadata pass.
        "has_metadata": meta > 0,
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
                "has_metadata": mc > 0,  # Phase 7H: real fetched metadata (info_json), not a title stub
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
# Phase 7J: permanent-failure exclusion for metadata selection
# --------------------------------------------------------------------------- #
def _latest_metadata_reason_by_video(session: Session, *, scan_limit: int = 30000) -> dict[int, str]:
    """video.id -> primary_reason of its LATEST metadata_only liked job.

    A private/deleted/unavailable video never produces an info_json, so it stays
    "missing metadata" forever and would be re-enqueued every batch. We classify
    each video's most-recent metadata attempt so the selector can skip permanent
    ones. ONLY metadata_only liked jobs are considered (body-archive failures are
    not conflated). Rows are never deleted.
    """
    from app.services.job_classify import classify_job

    out: dict[int, str] = {}
    for j in session.scalars(
        select(Job)
        .where(
            Job.type == "download",
            Job.profile_name == METADATA_PROFILE,
            Job.status.in_(("failed", "partial_success", "success")),
        )
        .order_by(Job.id.desc())
        .limit(scan_limit)
    ):
        if (j.meta or {}).get("source_action") != SOURCE_ACTION:
            continue
        vid = j.video_id
        if vid is None or vid in out:
            continue  # id desc -> first seen is the latest job for this video
        out[vid] = classify_job(j).get("primary_reason") or ("ok" if j.status == "success" else "unknown")
    return out


def permanent_metadata_video_ids(session: Session) -> set[int]:
    """video.id set whose latest metadata attempt is permanent (private/deleted/
    unavailable) — excluded from metadata selection by default. Never deleted."""
    from app.services.job_classify import PERMANENT_REASONS

    return {vid for vid, r in _latest_metadata_reason_by_video(session).items() if r in PERMANENT_REASONS}


# --------------------------------------------------------------------------- #
# Phase 9A: per-video size estimator + disk capacity guard
# --------------------------------------------------------------------------- #
_GIB = 1024 ** 3
_MIB = 1024 ** 2


def video_size_estimate(session: Session, settings: Settings) -> dict:
    """Conservative per-video body-size estimate from saved 'video' media files.

    Computes avg / median / p90 of real ``filesize`` values; the estimate is the
    p90 (bounded below by the average) so batch planning errs LARGE. Falls back to
    a fixed size when there is too little history. Reads sizes only — no paths,
    titles, or raw_json.
    """
    sizes = [
        int(s)
        for s in session.scalars(
            select(MediaFile.filesize).where(
                MediaFile.media_type == "video",
                MediaFile.filesize.is_not(None),
                MediaFile.filesize > 0,
            )
        )
    ]
    fallback_bytes = int(max(0.0, settings.archive_size_estimate_fallback_mb) * _MIB) or _MIB
    min_samples = max(1, settings.archive_size_estimate_min_samples)
    n = len(sizes)
    if n < min_samples:
        return {
            "source": "fallback",
            "sample_count": n,
            "estimate_bytes": fallback_bytes,
            "estimate_mb": round(fallback_bytes / _MIB, 1),
            "avg_mb": None,
            "median_mb": None,
            "p90_mb": None,
        }
    sizes.sort()
    avg = sum(sizes) / n
    median = sizes[n // 2]
    p90 = sizes[min(n - 1, int(round(0.9 * (n - 1))))]
    estimate = max(int(p90), int(avg))  # conservative: err large
    return {
        "source": "measured",
        "sample_count": n,
        "estimate_bytes": estimate,
        "estimate_mb": round(estimate / _MIB, 1),
        "avg_mb": round(avg / _MIB, 1),
        "median_mb": round(median / _MIB, 1),
        "p90_mb": round(p90 / _MIB, 1),
    }


def capacity_plan(
    session: Session,
    settings: Settings,
    *,
    selected_count: int,
    min_free_gb: float | None = None,
    allow_low_disk: bool = False,
) -> dict:
    """Disk-capacity decision for a body run of ``selected_count`` videos (Phase 9A).

    ``blocked`` is True ONLY when the disk is READABLE and the run would (or the
    volume already does) drop below ``min_free_gb`` — and it is not overridden by
    ``allow_low_disk``. When free space is unreadable we cannot prove low disk, so
    we never hard-block on it (the caller can still see ``disk_readable=False``).
    """
    from app.services import storage

    disk = storage.disk_usage(settings)
    est = video_size_estimate(session, settings)
    per = max(1, int(est["estimate_bytes"]))
    if min_free_gb is None:
        min_free_gb = settings.archive_min_free_gb
    min_free_bytes = int(max(0.0, min_free_gb) * _GIB)
    selected_count = max(0, int(selected_count))
    required = selected_count * per
    free = disk.get("free_bytes")

    out = {
        "disk": disk,
        "size_estimate": est,
        "selected_count": selected_count,
        "estimated_required_bytes": required,
        "estimated_required_gb": round(required / _GIB, 2),
        "min_free_gb": round(float(min_free_gb), 2),
        "min_free_bytes": min_free_bytes,
        "allow_low_disk": bool(allow_low_disk),
        "disk_readable": bool(disk.get("readable")),
    }
    if free is None:
        out.update(
            estimated_free_after_bytes=None,
            estimated_free_after_gb=None,
            already_below_min_free=False,
            would_go_below_min_free=False,
            disk_safe_limit=None,
            blocked=False,
            block_reason=None,
            note="disk free space unreadable — capacity guard skipped",
        )
        return out

    estimated_free_after = free - required
    already_low = free < min_free_bytes
    would_low = estimated_free_after < min_free_bytes
    disk_safe_limit = int(max(0, (free - min_free_bytes) // per))
    unsafe = already_low or would_low
    blocked = bool(unsafe and not allow_low_disk)
    reason: str | None = None
    if unsafe:
        if already_low:
            reason = (f"free {disk['free_gb']} GiB is already below min-free "
                      f"{out['min_free_gb']} GiB")
        else:
            reason = (f"a run of {selected_count} (~{out['estimated_required_gb']} GiB) would leave "
                      f"{round(estimated_free_after / _GIB, 2)} GiB, below min-free {out['min_free_gb']} GiB "
                      f"(disk-safe limit {disk_safe_limit})")
        if allow_low_disk:
            reason = "OVERRIDDEN by --allow-low-disk: " + reason
    out.update(
        estimated_free_after_bytes=estimated_free_after,
        estimated_free_after_gb=round(estimated_free_after / _GIB, 2),
        already_below_min_free=already_low,
        would_go_below_min_free=would_low,
        disk_safe_limit=disk_safe_limit,
        blocked=blocked,
        block_reason=reason,
        note=None,
    )
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
    # Phase 8A: body archive also excludes permanent (private/deleted/unavailable)
    # — they can't be downloaded and are kept, never deleted.
    permanent_excluded: int = 0
    eligible_missing_body: int = 0
    existing_active_jobs: int = 0
    existing_retryable: int = 0
    recommended_limit: int = 0
    recommended_delay_seconds: float = 0.0
    recommended_profile: str = ""
    profile: str = ""
    notes: list[str] = field(default_factory=list)
    # Phase 9A: batch planning + disk capacity guard.
    requested_limit: int = 0
    cap_per_run: int = 0
    selected_count: int = 0          # what a run at the requested limit would enqueue (pre-disk-guard)
    disk_safe_limit: int | None = None
    limiting_factor: str = ""        # requested | cap | eligible | disk
    blocked: bool = False
    block_reason: str | None = None
    # flattened disk / size-estimate figures (no host path — leak-safe)
    disk_readable: bool = True
    disk_total_gb: float | None = None
    disk_used_gb: float | None = None
    disk_free_gb: float | None = None
    min_free_gb: float = 0.0
    estimated_size_per_video_mb: float = 0.0
    size_estimate_source: str = ""
    size_estimate_sample_count: int = 0
    estimated_required_gb: float = 0.0
    estimated_free_after_gb: float | None = None


def archive_plan(
    session: Session,
    settings: Settings,
    *,
    filters: LikedFilters,
    profile: str | None = None,
    limit: int | None = None,
) -> ArchivePlan:
    """Count what an archive run WOULD touch (no jobs created)."""
    prof = profile or settings.effective_body_archive_profile
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
    # Phase 8A: permanent (private/deleted/unavailable) videos can't be downloaded;
    # they are excluded from the body run by default (kept, never deleted).
    permanent_ids = permanent_metadata_video_ids(session)
    permanent_excluded = sum(
        1 for _lv, v, st in cands
        if v is not None and v.id in permanent_ids and not st["has_body"]
    )
    eligible_missing_body = max(missing_body - permanent_excluded, 0)

    active = 0
    for _lv, v, _st in cands:
        if v is not None and _active_job_exists(session, v.url, prof):
            active += 1

    retryable = len(retryable_liked(session, settings, reason=None, limit=10_000))

    # ---- Phase 9A: batch planning + disk capacity guard ----
    requested_limit = int(limit or settings.liked_archive_default_limit)
    cap = int(settings.liked_archive_max_enqueue_per_run)
    # What a run at the requested limit WOULD enqueue, ignoring disk (cap + eligible bound it).
    intended = max(0, min(requested_limit, cap, eligible_missing_body))
    capr = capacity_plan(session, settings, selected_count=intended)
    disk = capr["disk"]
    est = capr["size_estimate"]
    disk_safe = capr.get("disk_safe_limit")  # None when free space is unreadable

    bounds = {"requested": requested_limit, "cap": cap, "eligible": eligible_missing_body}
    if disk_safe is not None:
        bounds["disk"] = disk_safe
    recommended_limit = max(0, min(bounds.values()))
    limiting_factor = min(bounds, key=lambda k: bounds[k])

    rec_delay = (
        settings.liked_archive_job_delay_seconds
        or settings.download_job_delay_seconds
        or 30.0
    )
    notes = [
        "Start small: archive 10-30 videos at a time, then check classification.",
        "metadata_only does NOT download the body; a video profile DOES.",
        "Size is an ESTIMATE (p90 of saved videos); actual varies with video length.",
    ]
    if missing_meta:
        notes.append(f"{missing_meta} need metadata first (run enqueue-metadata).")
    if capr.get("blocked"):
        notes.append(f"DISK BLOCK: {capr.get('block_reason')}")
    elif not capr.get("disk_readable"):
        notes.append("Disk free space unreadable here — capacity guard is inactive.")
    return ArchivePlan(
        total_candidates=total,
        missing_metadata=missing_meta,
        missing_body=missing_body,
        has_body=has_body,
        permanent_excluded=permanent_excluded,
        eligible_missing_body=eligible_missing_body,
        existing_active_jobs=active,
        existing_retryable=retryable,
        recommended_limit=recommended_limit,
        recommended_delay_seconds=rec_delay,
        recommended_profile=settings.effective_body_archive_profile,
        profile=prof,
        notes=notes,
        requested_limit=requested_limit,
        cap_per_run=cap,
        selected_count=intended,
        disk_safe_limit=disk_safe,
        limiting_factor=limiting_factor,
        blocked=bool(capr.get("blocked")),
        block_reason=capr.get("block_reason"),
        disk_readable=bool(capr.get("disk_readable")),
        disk_total_gb=disk.get("total_gb"),
        disk_used_gb=disk.get("used_gb"),
        disk_free_gb=disk.get("free_gb"),
        min_free_gb=capr.get("min_free_gb", settings.archive_min_free_gb),
        estimated_size_per_video_mb=est.get("estimate_mb", 0.0),
        size_estimate_source=est.get("source", ""),
        size_estimate_sample_count=est.get("sample_count", 0),
        estimated_required_gb=capr.get("estimated_required_gb", 0.0),
        estimated_free_after_gb=capr.get("estimated_free_after_gb"),
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
    skipped_existing_job: int = 0  # an active (queued/running) job already exists
    skipped_already_has_metadata: int = 0
    skipped_already_has_body: int = 0
    skipped_permanent: int = 0  # Phase 7J: private/deleted/unavailable (latest metadata attempt)
    job_ids: list[int] = field(default_factory=list)
    profile: str = ""
    downloads_body: bool = False
    dry_run: bool = False
    # Phase 9A: disk capacity guard outcome (body archive only).
    blocked: bool = False
    block_reason: str | None = None
    capacity: dict = field(default_factory=dict)


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
    exclude_permanent: bool = False,
    prioritize_info_json: bool = False,
    disk_guard: bool = False,
    allow_low_disk: bool = False,
    min_free_gb: float | None = None,
) -> EnqueueResult:
    cap = settings.liked_archive_max_enqueue_per_run
    eff_limit = min(limit or settings.liked_archive_default_limit, cap)
    res = EnqueueResult(profile=profile, downloads_body=downloads_body, dry_run=dry_run)
    # Phase 9A: disk capacity guard (body archive). Estimate the whole batch
    # (eff_limit) conservatively; REFUSE a real run that would drop below min-free
    # unless overridden. Skipped when free space is unreadable (can't prove low).
    if disk_guard:
        capr = capacity_plan(
            session, settings, selected_count=eff_limit,
            min_free_gb=min_free_gb, allow_low_disk=allow_low_disk,
        )
        res.capacity = capr
        res.blocked = bool(capr.get("blocked"))
        res.block_reason = capr.get("block_reason")
        if res.blocked and not dry_run:
            logger.warning("liked archive: enqueue BLOCKED by disk guard: %s", res.block_reason)
            return res  # refuse — no jobs created
    # Phase 7J: skip videos whose latest metadata attempt was permanent
    # (private/deleted/unavailable) so they aren't re-enqueued every batch.
    permanent_ids = permanent_metadata_video_ids(session) if exclude_permanent else set()

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
    if prioritize_info_json:
        # Phase 8A: archive videos we have COMPLETE metadata for (info_json) first,
        # keeping liked_at-desc order within each group (stable sort).
        info_ids = _media_type_video_ids(
            session, [v.id for _l, v, _s in cands if v is not None], "info_json"
        )
        cands.sort(key=lambda t: 0 if (t[1] is not None and t[1].id in info_ids) else 1)
    for lv, video, state in cands:
        if res.selected_count >= eff_limit:
            break
        if skip_if == "metadata" and state["has_metadata"]:
            res.skipped_already_has_metadata += 1
            continue
        if skip_if == "body" and state["has_body"]:
            res.skipped_already_has_body += 1
            continue
        if permanent_ids and video is not None and video.id in permanent_ids:
            res.skipped_permanent += 1
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
                    # Phase 8C: persist rq_job_id so tooling can correlate DB<->RQ.
                    rq_id = jobs_svc.submit_job(jid)
                    job = session.get(Job, jid)
                    if job is not None:
                        job.rq_job_id = rq_id
                except Exception as exc:  # noqa: BLE001
                    logger.warning("liked archive: job %s not submitted: %s", jid, exc)
            session.commit()
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
    include_permanent: bool = False,
) -> EnqueueResult:
    """Enqueue metadata_only jobs (NEVER downloads the body).

    By default excludes videos whose latest metadata attempt was permanent
    (private/deleted/unavailable); pass ``include_permanent=True`` to retry them.
    """
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
        exclude_permanent=not include_permanent,
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
    exclude_permanent: bool = True,
    prioritize_info_json: bool = True,
    allow_low_disk: bool = False,
    min_free_gb: float | None = None,
) -> EnqueueResult:
    """Enqueue a BODY archive (downloads the video body with the given profile).

    Phase 8A: permanent failures (private/deleted/unavailable) are excluded by
    default — they can't be downloaded and are kept, never deleted. Pass
    ``exclude_permanent=False`` to override (not recommended). Videos with a
    complete info_json are archived first (``prioritize_info_json``).

    Phase 9A: defaults to the production body profile (comments-light) and applies
    the disk capacity guard — a real run that would drop the archive volume below
    ARCHIVE_MIN_FREE_GB is REFUSED unless ``allow_low_disk=True``.
    """
    prof = profile or settings.effective_body_archive_profile
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
        exclude_permanent=exclude_permanent,
        prioritize_info_json=prioritize_info_json,
        disk_guard=True,
        allow_low_disk=allow_low_disk,
        min_free_gb=min_free_gb,
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
    metadata_only: bool = False,
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
        if metadata_only and j.profile_name != METADATA_PROFILE:
            continue  # Phase 7I: retry-metadata restricts to metadata_only jobs
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
    permanent_ids = permanent_metadata_video_ids(session)  # Phase 7J
    # Phase 7L: split the broad "any metadata media" count from the rigorous
    # "info_json complete" count so the full-metadata decision uses real completeness.
    info_json_ids = _media_type_video_ids(session, video_ids, "info_json")
    description_ids = _media_type_video_ids(session, video_ids, "description")
    latest_reason = _latest_metadata_reason_by_video(session)
    from app.services.job_classify import PERMANENT_REASONS

    seen: set[str] = set()
    total = meta_fetched = body_saved = 0
    skipped_permanent_meta = permanent_unique = eligible_body = 0
    info_json_complete = description_only = retryable_partial = 0
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
        has_meta = bool(v is not None and meta_map.get(v.id, 0) > 0)  # Phase 7H: any metadata media (broad)
        has_body = bool(v is not None and body_map.get(v.id, 0) > 0)
        is_permanent = bool(v is not None and v.id in permanent_ids)  # Phase 7J
        has_info = bool(v is not None and v.id in info_json_ids)        # Phase 7L: info_json complete
        has_desc = bool(v is not None and v.id in description_ids)
        if has_meta:
            meta_fetched += 1
        if has_info:
            info_json_complete += 1
        if has_meta and not has_info:
            # Partial: has some metadata media (usually .description) but no info_json.
            if has_desc:
                description_only += 1
            r = latest_reason.get(v.id) if v is not None else None
            if r and r not in PERMANENT_REASONS and r != "ok":
                retryable_partial += 1  # upgradeable to full info_json via retry-metadata
        if has_body:
            body_saved += 1
        elif not is_permanent:
            eligible_body += 1  # Phase 9A: missing body AND downloadable (not permanent)
        if is_permanent:
            permanent_unique += 1
            if not has_meta:
                skipped_permanent_meta += 1  # missing + permanent => excluded from selection
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
    metadata_missing = total - meta_fetched
    return {
        "total_liked": total,
        # Phase 7H/7L: metadata_fetched is a BROAD count (>=1 of info_json /
        # description / thumbnail / link / live_chat). For full-metadata decisions
        # use info_json_complete_count (rigorous), not this broad number.
        "metadata_fetched": meta_fetched,
        "metadata_any_count": meta_fetched,
        "info_json_complete_count": info_json_complete,
        "description_only_count": description_only,
        "retryable_partial_count": retryable_partial,
        "metadata_missing": metadata_missing,
        # Phase 7J: missing minus permanent (private/deleted/unavailable) = what
        # metadata-run will actually select. permanent rows are kept, not retried.
        "eligible_metadata_missing": metadata_missing - skipped_permanent_meta,
        "skipped_permanent_metadata": skipped_permanent_meta,
        "permanent_unique_videos": permanent_unique,
        "body_saved": body_saved,
        "body_missing": total - body_saved,
        # Phase 9A: missing body minus permanent (private/deleted/unavailable) =
        # what a body archive run can actually download. permanent kept, not deleted.
        "eligible_missing_body": eligible_body,
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


def operations_status(session: Session, settings: Settings) -> dict:
    """Phase 9A: one-shot production operations snapshot for body archiving.

    Consolidates progress + disk + size estimate + orphan/duplicate + DB stats
    (comments table size, raw_json total) so an operator sees, at a glance,
    whether it is safe to run another batch. Counts/figures only — never
    raw_json, titles, cookies, or host paths.
    """
    from app.services import db_stats as dbs
    from app.services import queue_health
    from app.services import reconcile
    from app.services import storage

    prog = progress(session, settings)
    disk = storage.disk_usage(settings)
    est = video_size_estimate(session, settings)
    stats = dbs.db_stats(session)
    q = queue_health.queue_status(session)

    # orphan dry-run summary (never mutates; guards a down/absent RQ)
    try:
        orphan = reconcile.reconcile_orphans(session, settings, apply=False)
        orphan_summary = {
            "scanned": orphan.get("scanned", 0),
            "orphan_found": orphan.get("orphan_found", 0),
            "rq_unreadable": bool(orphan.get("rq_unreadable")),
        }
    except Exception as exc:  # noqa: BLE001 - status must never crash
        logger.warning("operations_status: orphan check failed: %s", exc)
        orphan_summary = {"scanned": 0, "orphan_found": 0, "rq_unreadable": True}

    try:
        dup_count = len(reconcile.duplicate_video_media(session))
    except Exception:  # noqa: BLE001
        dup_count = 0

    comments_bytes = (stats.get("table_sizes_bytes") or {}).get("comments")

    return {
        "default_body_profile": settings.effective_body_archive_profile,
        "body_saved": prog.get("body_saved", 0),
        "remaining_eligible_body": prog.get("eligible_missing_body", 0),
        "permanent_unique_videos": prog.get("permanent_unique_videos", 0),
        "active_archive_jobs": prog.get("active_archive_jobs", 0),
        "queued_jobs": q.get("queued", 0),
        "running_jobs": q.get("running", 0),
        "total_active_jobs": q.get("total_active", 0),
        "worker_count": q.get("worker_count", 0),
        "disk": disk,
        "min_free_gb": settings.archive_min_free_gb,
        "size_estimate": est,
        "orphan": orphan_summary,
        "duplicate_video_media_files": dup_count,
        "comments_table_bytes": int(comments_bytes) if comments_bytes else 0,
        "raw_json_stored_total": stats.get("raw_json_stored_total", 0),
    }


def failure_breakdown(session: Session, *, scan_limit: int = 20000) -> dict:
    """Phase 7H: count failed/partial liked-archive jobs by classification
    reason (private / deleted / unavailable / network / rate_limited / unknown /
    …). Lets the operator see WHY archive/metadata jobs failed at each stage.

    Counts only — no raw_json / titles / paths. Failed videos are recorded with
    a reason, never deleted.
    """
    from app.services.job_classify import PERMANENT_REASONS

    by_reason: dict[str, int] = {}              # per-JOB attempts
    latest_reason_by_video: dict[int, str] = {}  # video_id -> latest failed reason
    total_failed = total_partial = retryable = permanent = 0
    for j in session.scalars(
        select(Job).where(Job.type == "download").order_by(Job.id.desc()).limit(scan_limit)
    ):
        if (j.meta or {}).get("source_action") != SOURCE_ACTION:
            continue
        if j.status not in ("failed", "partial_success"):
            continue
        c = classify_job(j)
        if j.status == "failed":
            total_failed += 1
        else:
            total_partial += 1
        reason = c.get("primary_reason") or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1
        if c.get("retryable"):
            retryable += 1
        if reason in PERMANENT_REASONS:
            permanent += 1
        # unique per video: id desc -> first seen is the latest attempt
        if j.video_id is not None and j.video_id not in latest_reason_by_video:
            latest_reason_by_video[j.video_id] = reason

    unique_by_reason: dict[str, int] = {}
    permanent_unique = 0
    for r in latest_reason_by_video.values():
        unique_by_reason[r] = unique_by_reason.get(r, 0) + 1
        if r in PERMANENT_REASONS:
            permanent_unique += 1

    def _sorted(d: dict) -> dict:
        return dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True))

    return {
        "total_failed": total_failed,
        "total_partial": total_partial,
        "retryable": retryable,
        "permanent": permanent,  # private/deleted/unavailable attempts — not retried
        "permanent_unique_videos": permanent_unique,
        "by_reason": _sorted(by_reason),                  # backward-compat (= attempts)
        "attempts_by_reason": _sorted(by_reason),         # per-job attempts (Phase 7J)
        "unique_videos_by_reason": _sorted(unique_by_reason),  # distinct videos, latest reason
    }


def retry_failed_liked(
    session: Session,
    settings: Settings,
    *,
    reason: str | None = None,
    limit: int = 20,
    now: datetime | None = None,
    submit: bool = True,
    metadata_only: bool = False,
) -> list[int]:
    """Re-queue retryable liked-archive jobs (respects the attempt cap + backoff).

    Only ``retryable`` jobs are selected, so permanent failures
    (private/deleted/unavailable) are NEVER re-queued (Phase 7H/7I)."""
    candidates = retryable_liked(
        session, settings, reason=reason, limit=limit, now=now, metadata_only=metadata_only
    )
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


# --------------------------------------------------------------------------- #
# Phase 7I: safe staged metadata-run with rate-limit-ratio gating
# --------------------------------------------------------------------------- #
def metadata_rate_decision(
    rate_limited: int, attempted: int, *, warn_ratio: float, stop_ratio: float
) -> dict:
    """Pure decision: ok / warn / stop from the rate_limited ratio of a run."""
    ratio = round(rate_limited / attempted, 3) if attempted else 0.0
    level = "stop" if ratio >= stop_ratio else "warn" if ratio >= warn_ratio else "ok"
    return {"attempted": attempted, "rate_limited": rate_limited, "ratio": ratio, "level": level}


_LEVEL_ORDER = {"ok": 0, "warn": 1, "stop": 2}


def combine_run_level(overall_level: str, batch_levels: list[str], stopped: str | None) -> str:
    """Phase 7L: the run's reported level must reflect the WORST batch, not just
    the overall (averaged) ratio.

    A run can average ratio < 0.5 (overall "ok") yet contain a batch that hit the
    STOP threshold and halted the run — reporting "ok" there is misleading. So the
    final level is the worst of (overall, every batch); and if the run halted
    *because* a batch hit the rate-limit STOP, the level is forced to "stop".
    """
    rate_stopped = bool(stopped and str(stopped).startswith("rate_limit_ratio>="))
    candidates = [overall_level, *batch_levels]
    if rate_stopped:
        candidates.append("stop")
    return max(candidates, key=lambda lv: _LEVEL_ORDER.get(lv, 0))


def _classify_run_jobs(session: Session, job_ids: list[int]) -> dict:
    """Classify a run's (terminal) jobs by primary reason."""
    from collections import Counter

    from app.services.job_classify import PERMANENT_REASONS

    by_reason: Counter = Counter()
    attempted = success = rate_limited = permanent = 0
    for jid in job_ids:
        j = session.get(Job, jid)
        if j is None or j.status not in ("success", "failed", "partial_success"):
            continue
        attempted += 1
        if j.status == "success":
            success += 1
            continue
        pr = classify_job(j).get("primary_reason") or "unknown"
        by_reason[pr] += 1
        if pr == "rate_limited":
            rate_limited += 1
        if pr in PERMANENT_REASONS:
            permanent += 1
    return {
        "attempted": attempted, "success": success, "rate_limited": rate_limited,
        "permanent": permanent, "by_reason": dict(by_reason),
    }


def _wait_for_metadata_jobs(job_ids: list[int], *, timeout: float = 3600.0, interval: float = 3.0) -> bool:
    """Poll a batch of jobs to terminal state (fresh sessions). True if all done."""
    import time as _t

    from app.db import session_scope

    waited = 0.0
    while waited < timeout:
        with session_scope() as s:
            pending = sum(
                1 for jid in job_ids
                if (j := s.get(Job, jid)) is not None and j.status in ("queued", "running")
            )
        if pending == 0:
            return True
        _t.sleep(interval)
        waited += interval
    return False


def metadata_run(
    settings: Settings, *, target_limit: int | None = None, apply: bool = True,
    max_batches: int = 40, wait_timeout: float = 3600.0, include_permanent: bool = False,
    on_batch: Callable[[dict], None] | None = None,
) -> dict:
    """Staged liked-metadata fetch (Phase 7I). Manages its own sessions.

    Loops: enqueue a capped batch of missing-metadata videos → wait for the
    worker → measure that batch's rate_limited ratio → continue until the target
    is met, nothing is missing, or the ratio hits the STOP threshold (so a full
    run halts when YouTube is throttling). ``target_limit=None`` => all missing.
    ``apply=False`` => plan only (dry-run; no jobs, no waiting). NEVER downloads
    the body; never retries permanent failures.
    """
    import uuid

    from app.db import session_scope
    from app.services import build_info as bi
    from app.services import db_stats as dbs

    cap = max(1, settings.liked_metadata_max_enqueue_per_run)
    warn_ratio = settings.liked_metadata_warn_on_rate_limit_ratio
    stop_ratio = settings.liked_metadata_stop_on_rate_limit_ratio
    run_id = uuid.uuid4().hex[:16]

    with session_scope() as s:
        before = progress(s, settings)
        size_before = dbs.db_stats(s)["total_size_mb"]

    out = {
        "ok": False, "run_id": run_id, "apply": apply, "target_limit": target_limit,
        "metadata_fetched_before": before["metadata_fetched"],
        "metadata_missing": before["metadata_missing"],
        "db_size_mb_before": size_before, "db_size_mb_after": size_before,
        "batches": [], "enqueued_total": 0, "attempted": 0, "rate_limited": 0,
        "skipped_permanent": 0, "include_permanent": include_permanent,
        "eligible_metadata_missing": before.get("eligible_metadata_missing"),
        "permanent_unique_videos": before.get("permanent_unique_videos"),
        "ratio": 0.0, "level": "ok", "stopped_reason": None,
        "overall_ratio": 0.0, "overall_level": "ok",
        "worst_batch_ratio": 0.0, "worst_batch_level": "ok", "batch_stop_triggered": False,
        "info_json_complete_before": before.get("info_json_complete_count"),
        "info_json_complete_after": before.get("info_json_complete_count"),
        "metadata_fetched_after": before["metadata_fetched"],
        "warn_ratio": warn_ratio, "stop_ratio": stop_ratio, "recommended_next": None, "message": None,
    }

    if not apply:
        with session_scope() as s:
            r = enqueue_metadata(
                s, settings, filters=LikedFilters(missing_metadata=True),
                limit=(min(target_limit, cap) if target_limit else cap), dry_run=True, submit=False,
                include_permanent=include_permanent,
            )
        out.update(ok=True, plan_selected=r.selected_count, skipped_permanent=r.skipped_permanent,
                   message="dry-run plan (no jobs, no body) — re-run without --dry-run to fetch",
                   recommended_next="run with --apply (worker required)")
        return out

    # apply requires a live worker (don't enqueue jobs nothing will run)
    try:
        from app.worker.queue import get_redis

        workers = bi.read_worker_heartbeats(get_redis())
    except Exception:  # noqa: BLE001
        workers = []
    if not workers:
        out["message"] = "no worker heartbeat — start a worker before metadata-run --apply"
        return out

    enqueued = attempted = rate_limited = skipped_permanent = 0
    remaining = target_limit
    stopped = None
    for batch_i in range(max_batches):
        if remaining is not None and remaining <= 0:
            break
        batch = cap if remaining is None else min(remaining, cap)
        with session_scope() as s:
            r = enqueue_metadata(
                s, settings, filters=LikedFilters(missing_metadata=True), limit=batch,
                dry_run=False, submit=True, extra_meta={"metadata_run_id": run_id, "batch": batch_i},
                include_permanent=include_permanent,
            )
            s.commit()
            job_ids = list(r.job_ids)
            created = r.jobs_created
            batch_skipped_perm = r.skipped_permanent
        skipped_permanent = batch_skipped_perm  # permanent set is global; last value is the live count
        if created == 0:
            # nothing ELIGIBLE left (permanent failures are excluded, not looped on)
            stopped = "no_more_eligible" if batch_skipped_perm else "no_more_missing"
            break
        _wait_for_metadata_jobs(job_ids, timeout=wait_timeout)
        with session_scope() as s:
            bstats = _classify_run_jobs(s, job_ids)
        enqueued += created
        attempted += bstats["attempted"]
        rate_limited += bstats["rate_limited"]
        if remaining is not None:
            remaining -= created
        dec = metadata_rate_decision(bstats["rate_limited"], bstats["attempted"],
                                     warn_ratio=warn_ratio, stop_ratio=stop_ratio)
        batch_row = {"batch": batch_i, "created": created, "selected": created,
                     "skipped_permanent": batch_skipped_perm, **bstats,
                     "ratio": dec["ratio"], "level": dec["level"]}
        out["batches"].append(batch_row)
        if on_batch is not None:
            # Phase 7L: emit per-batch progress AS IT HAPPENS so detached runs
            # (docker compose exec -d ... > /logs/mr*.log) show live batch ratios
            # instead of nothing until the run finishes.
            try:
                on_batch(batch_row)
            except Exception:  # noqa: BLE001 - never let a logging callback kill the run
                pass
        if dec["level"] == "stop":
            stopped = f"rate_limit_ratio>={stop_ratio}"
            break
    else:
        stopped = "max_batches"

    overall = metadata_rate_decision(rate_limited, attempted, warn_ratio=warn_ratio, stop_ratio=stop_ratio)
    # Phase 7L: the reported level reflects the WORST batch + any rate-limit STOP,
    # not just the averaged overall ratio (which can read "ok" despite a STOP).
    batch_levels = [b["level"] for b in out["batches"]]
    worst_batch_ratio = max((b["ratio"] for b in out["batches"]), default=0.0)
    worst_batch_level = max(batch_levels, key=lambda lv: _LEVEL_ORDER.get(lv, 0)) if batch_levels else "ok"
    final_level = combine_run_level(overall["level"], batch_levels, stopped)
    batch_stop_triggered = bool(stopped and str(stopped).startswith("rate_limit_ratio>="))

    with session_scope() as s:
        after = progress(s, settings)
        size_after = dbs.db_stats(s)["total_size_mb"]

    eligible_after = after.get("eligible_metadata_missing", after["metadata_missing"])
    if final_level == "stop":
        rec = ("rate limit too high (a batch hit the STOP threshold) — set "
               "COOKIES_FILE / YOUTUBE_PO_TOKEN + raise LIKED_METADATA_JOB_DELAY_SECONDS, "
               "then `retry-metadata --retryable` later (run halted before flooding)")
    elif final_level == "warn":
        rec = "elevated 429 — consider PO-token / larger delay; `retry-metadata --reason rate_limited` later"
    elif eligible_after and eligible_after > 0:
        rec = f"{eligible_after} eligible still missing — re-run metadata-run"
    else:
        rec = ("all ELIGIBLE liked metadata fetched; "
               f"{after.get('permanent_unique_videos', 0)} permanent (private/deleted/unavailable) kept, not retried")

    out.update(
        ok=True, enqueued_total=enqueued, attempted=attempted, rate_limited=rate_limited,
        skipped_permanent=skipped_permanent,
        eligible_metadata_missing=eligible_after,
        permanent_unique_videos=after.get("permanent_unique_videos"),
        ratio=overall["ratio"], level=final_level,
        overall_ratio=overall["ratio"], overall_level=overall["level"],
        worst_batch_ratio=worst_batch_ratio, worst_batch_level=worst_batch_level,
        batch_stop_triggered=batch_stop_triggered, stopped_reason=stopped,
        info_json_complete_after=after.get("info_json_complete_count"),
        metadata_fetched_after=after["metadata_fetched"], db_size_mb_after=size_after,
        recommended_next=rec,
        message=(f"metadata-run complete (level={final_level}; overall ratio={overall['ratio']} "
                 f"[{overall['level']}], worst batch={worst_batch_ratio} [{worst_batch_level}], "
                 f"stopped={stopped})"),
    )
    return out
