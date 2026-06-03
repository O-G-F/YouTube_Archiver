"""RQ task handlers.

``run_job(job_id)`` is the single RQ entrypoint; it dispatches by job type. The
same function runs inline (CLI ``--now`` / tests) since it takes only a job id
and reads everything from the DB.

Key invariants:
  - download jobs are single-video (``--no-playlist``) and use the download
    archive for duplicate avoidance.
  - metadata_refresh jobs use ``--skip-download`` + ``--no-download-archive`` so
    the video body is NEVER re-downloaded (requirement 4.3 / 5.5).
  - long subprocess runs happen OUTSIDE any open DB transaction; short
    ``session_scope()`` blocks bracket the subprocess for state updates.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import Job, MetadataSnapshot, Video, utcnow
from app.services import expand as expand_svc
from app.services import jobs as jobs_svc
from app.services import storage
from app.services.command_builder import download_build_context, external_ctx
from app.services.ingest import (
    ingest_comments_from_info,
    link_collection_items,
    register_outputs,
    upsert_video_from_info,
)
from app.services.profiles import BuildContext, build_ytdlp_args, get_profile_spec
from app.services.urls import canonical_video_url, normalize_url
from app.services.ytdlp import extract_info, run_ytdlp

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #
def run_job(job_id: int) -> None:
    settings = get_settings()
    settings.ensure_dirs()

    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            logger.error("run_job: job %s not found", job_id)
            return
        if job.status == "canceled":
            logger.info("run_job: job %s is canceled; skipping", job_id)
            return
        jtype = job.type

    try:
        if jtype == "download":
            _run_download(settings, job_id)
        elif jtype == "expand":
            _run_expand(settings, job_id)
        elif jtype == "metadata_refresh":
            _run_metadata_refresh(settings, job_id)
        else:
            raise ValueError(f"unknown job type: {jtype!r}")
    except Exception as exc:  # noqa: BLE001 - we want to record every failure
        logger.exception("run_job: job %s raised", job_id)
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is not None and job.status not in ("success", "canceled"):
                jobs_svc.mark_failed(s, job, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def _run_download(settings: Settings, job_id: int) -> None:
    # Rate control: space out consecutive download jobs on a single worker so a
    # large expand does not hammer YouTube (helps avoid HTTP 429).
    if settings.download_job_delay_seconds and settings.download_job_delay_seconds > 0:
        time.sleep(settings.download_job_delay_seconds)

    with session_scope() as s:
        job = s.get(Job, job_id)
        url = job.url
        profile_name = job.profile_name or settings.default_profile
        jobs_svc.mark_running(s, job)

    parsed = normalize_url(url)
    youtube_video_id = parsed.video_id
    if not youtube_video_id:
        with session_scope() as s:
            jobs_svc.mark_failed(s, s.get(Job, job_id), f"no video id in URL {url!r}")
        return

    # Best-effort metadata extraction (Python API). The subprocess download is
    # the source of truth; this just lets us pick a sensible output directory.
    info: dict | None = None
    try:
        info = extract_info(url, settings=settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("download: metadata extract failed for %s: %s", url, exc)

    with session_scope() as s:
        spec = get_profile_spec(s, profile_name)
    media_mode = spec.media_mode

    with session_scope() as s:
        if info:
            video = upsert_video_from_info(
                s, info, settings, is_short=parsed.is_short, is_live=parsed.is_live
            )
        else:
            video = _upsert_minimal_video(s, parsed)
        job = s.get(Job, job_id)
        job.video_id = video.id
        video_pk = video.id
        channel_id = video.channel_id

    out_dir = storage.video_output_dir(settings, media_mode, channel_id, youtube_video_id)
    ctx = download_build_context(
        settings,
        spec,
        youtube_video_id=youtube_video_id,
        channel_id=channel_id,
        no_playlist=True,
    )
    argv = build_ytdlp_args(spec, ctx)
    log_dir = storage.job_log_dir(settings, job_id)
    run = run_ytdlp(
        argv, log_dir, url=url, settings=settings, timeout=settings.ytdlp_timeout or None
    )

    with session_scope() as s:
        job = s.get(Job, job_id)
        job.log_path = storage.log_relative(settings, log_dir)
        job.command_path = storage.log_relative(settings, run.command_path)
        video = s.get(Video, video_pk)

        # Register whatever yt-dlp produced, even on a non-zero exit: it often
        # writes info.json / description / subtitles before failing on one item
        # (e.g. a single subtitle language 429ing). This drives partial_success.
        counts = register_outputs(s, video, out_dir, profile_name, settings)
        # Link any collection_items that reference this video id (Phase 2B).
        link_collection_items(s, video)
        flags = spec.resolved_flags()
        if video.raw_info_json_path:
            disk_info = _load_json(
                storage.to_absolute(settings, video.raw_info_json_path)
            )
            if disk_info:
                upsert_video_from_info(s, disk_info, settings)
                if flags.get("write_comments"):
                    summary = ingest_comments_from_info(s, video, disk_info)
                    logger.info("download: comments %s", summary)

        produced = sum(counts.values()) > 0
        err_tail = _tail(run.stderr_path)
        rate_limited = ("HTTP Error 429" in err_tail) or ("Too Many Requests" in err_tail)
        if run.ok:
            jobs_svc.mark_success(s, job)
            logger.info("download: job %s success (%s)", job_id, youtube_video_id)
        elif produced:
            note = f"yt-dlp exited {run.returncode} but partial outputs were saved. {err_tail}"
            jobs_svc.mark_partial_success(s, job, note)
            if rate_limited:
                job.meta = {**(job.meta or {}), "retryable": True, "reason": "http_429"}
            logger.warning("download: job %s PARTIAL rc=%s", job_id, run.returncode)
        else:
            jobs_svc.mark_failed(s, job, f"yt-dlp exited {run.returncode}\n{err_tail}")
            # 429 with no output is a transient rate-limit: flag as retryable so
            # `archiver jobs retry` / the next crawl re-attempts it.
            if rate_limited:
                job.meta = {**(job.meta or {}), "retryable": True, "reason": "http_429"}
            logger.warning(
                "download: job %s failed rc=%s rate_limited=%s",
                job_id,
                run.returncode,
                rate_limited,
            )


# --------------------------------------------------------------------------- #
# expand (playlist / channel -> child download jobs)
# --------------------------------------------------------------------------- #
def _run_expand(settings: Settings, job_id: int) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        url = job.url
        profile_name = job.profile_name or settings.default_profile
        meta_in = job.meta or {}
        override = meta_in.get("max_items")
        detect_removed = bool(meta_in.get("detect_removed", True))
        jobs_svc.mark_running(s, job)

    cap = override if isinstance(override, int) and override > 0 else settings.expand_max_items
    log_dir = storage.job_log_dir(settings, job_id)

    # Flat extraction (subprocess -> command.txt/stdout/stderr in log_dir).
    try:
        info, entries, capped = expand_svc.flat_extract(settings, url, log_dir, cap)
    except Exception as exc:  # noqa: BLE001
        with session_scope() as s:
            job = s.get(Job, job_id)
            job.log_path = storage.log_relative(settings, log_dir)
            job.command_path = storage.log_relative(settings, log_dir / "command.txt")
            jobs_svc.mark_failed(s, job, f"flat extraction failed: {exc}")
        logger.warning("expand: job %s extraction failed: %s", job_id, exc)
        return

    with session_scope() as s:
        result = expand_svc.process_entries(
            s,
            settings,
            url=url,
            info=info,
            entries=entries,
            capped=capped,
            profile_name=profile_name,
            parent_job_id=job_id,
            detect_removed=detect_removed,
        )
        job = s.get(Job, job_id)
        job.collection_id = result.collection_id
        job.log_path = storage.log_relative(settings, log_dir)
        job.command_path = storage.log_relative(settings, log_dir / "command.txt")
        job.meta = {**(job.meta or {}), **result.as_meta()}
        child_ids = list(result.child_job_ids)
        jobs_svc.mark_success(s, job)

    submitted = _submit_children(child_ids)
    logger.info(
        "expand: job %s collection=%s discovered=%d created=%d skipped_existing=%d "
        "removed=%d capped=%s submitted_to_rq=%d",
        job_id,
        result.collection_id,
        result.discovered_count,
        result.created_jobs_count,
        result.skipped_existing_count,
        result.removed_count,
        result.capped,
        submitted,
    )


def _submit_children(child_ids: list[int]) -> int:
    """Submit child jobs to RQ. If Redis is unavailable they stay queued in DB."""
    if not child_ids:
        return 0
    try:
        for cid in child_ids:
            jobs_svc.submit_job(cid)
        return len(child_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "expand: could not submit children to RQ (%s); %d remain queued in DB",
            exc,
            len(child_ids),
        )
        return 0


# --------------------------------------------------------------------------- #
# metadata_refresh (comments/info only; never re-downloads the body)
# --------------------------------------------------------------------------- #
def _run_metadata_refresh(settings: Settings, job_id: int) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        video = s.get(Video, job.video_id) if job.video_id else None
        if video is None:
            jobs_svc.mark_failed(s, job, "metadata_refresh: job has no associated video")
            return
        youtube_video_id = video.youtube_video_id
        video_pk = video.id
        profile_name = job.profile_name or "comments_refresh_only"
        jobs_svc.mark_running(s, job)

    url = canonical_video_url(youtube_video_id)
    with session_scope() as s:
        spec = get_profile_spec(s, profile_name)

    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    work_dir = storage.snapshot_dir(settings, youtube_video_id) / stamp
    out_tpl = str(work_dir / "%(id)s.%(ext)s")
    ctx = BuildContext(
        output_template=out_tpl,
        download_archive=None,  # -> --no-download-archive (never skipped, never re-DL)
        no_playlist=True,
        default_sub_langs=settings.default_sub_langs,
        archive_sub_langs=settings.archive_sub_langs,
        max_comments=settings.ytdlp_max_comments,
        retry_sleep=settings.ytdlp_retry_backoff_seconds,
        **external_ctx(settings),
    )
    argv = build_ytdlp_args(spec, ctx)
    log_dir = storage.job_log_dir(settings, job_id)
    run = run_ytdlp(
        argv, log_dir, url=url, settings=settings, timeout=settings.ytdlp_timeout or None
    )

    with session_scope() as s:
        job = s.get(Job, job_id)
        job.log_path = storage.log_relative(settings, log_dir)
        job.command_path = storage.log_relative(settings, run.command_path)
        video = s.get(Video, video_pk)

        info_json = work_dir / f"{youtube_video_id}.info.json"
        got_info = info_json.is_file()
        if got_info:
            snap = MetadataSnapshot(
                video_id=video_pk,
                source="yt-dlp",
                snapshot_type="info_json",
                path=storage.to_relative(settings, info_json),
                fetched_at=utcnow(),
            )
            s.add(snap)
            s.flush()
            disk_info = _load_json(info_json)
            if disk_info:
                summary = ingest_comments_from_info(
                    s, video, disk_info, snapshot_id=snap.id
                )
                logger.info("metadata_refresh: job %s comments %s", job_id, summary)
            video.last_metadata_refresh_at = utcnow()

        if run.ok:
            jobs_svc.mark_success(s, job)
        elif got_info:
            # info/comments were captured despite a non-zero exit (e.g. a single
            # subtitle language failed) -> partial success, not a hard failure.
            jobs_svc.mark_partial_success(
                s,
                job,
                f"yt-dlp exited {run.returncode} but info/comments were saved. "
                f"{_tail(run.stderr_path)}",
            )
        else:
            jobs_svc.mark_failed(
                s, job, f"yt-dlp exited {run.returncode}\n{_tail(run.stderr_path)}"
            )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _upsert_minimal_video(session, parsed) -> Video:
    """Create a minimal Video row when metadata extraction was unavailable."""
    video = session.scalar(
        select(Video).where(Video.youtube_video_id == parsed.video_id)
    )
    if video is None:
        video = Video(
            youtube_video_id=parsed.video_id,
            url=parsed.canonical_url,
            is_short=parsed.is_short,
            is_live=parsed.is_live,
            first_seen_at=utcnow(),
        )
        session.add(video)
        session.flush()
    return video


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _tail(path: Path, max_chars: int = 4000) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]
