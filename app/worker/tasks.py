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
from pathlib import Path

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import (
    Collection,
    CollectionItem,
    Job,
    MetadataSnapshot,
    Source,
    Video,
    utcnow,
)
from app.services import jobs as jobs_svc
from app.services import storage
from app.services.command_builder import download_build_context, external_ctx
from app.services.ingest import (
    ingest_comments_from_info,
    register_outputs,
    upsert_video_from_info,
)
from app.services.profiles import BuildContext, build_ytdlp_args, get_profile_spec
from app.services.urls import canonical_video_url, is_video_id, normalize_url
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
        if run.ok:
            jobs_svc.mark_success(s, job)
            logger.info("download: job %s success (%s)", job_id, youtube_video_id)
        elif produced:
            jobs_svc.mark_partial_success(
                s,
                job,
                f"yt-dlp exited {run.returncode} but partial outputs were saved. "
                f"{_tail(run.stderr_path)}",
            )
            logger.warning("download: job %s PARTIAL rc=%s", job_id, run.returncode)
        else:
            jobs_svc.mark_failed(
                s,
                job,
                f"yt-dlp exited {run.returncode}\n{_tail(run.stderr_path)}",
            )
            logger.warning("download: job %s failed rc=%s", job_id, run.returncode)


# --------------------------------------------------------------------------- #
# expand (playlist / channel -> child download jobs)
# --------------------------------------------------------------------------- #
def _run_expand(settings: Settings, job_id: int) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        url = job.url
        profile_name = job.profile_name or settings.default_profile
        jobs_svc.mark_running(s, job)

    parsed = normalize_url(url)
    info = extract_info(url, flat=True, settings=settings)
    entries = [e for e in (info.get("entries") or []) if e]
    total = len(entries)

    cap = settings.expand_max_items
    if cap and cap > 0 and total > cap:
        logger.warning(
            "expand: capping to %d of %d entries (EXPAND_MAX_ITEMS=%d)", cap, total, cap
        )
        entries = entries[:cap]

    with session_scope() as s:
        source = Source(
            type=parsed.kind,
            url=parsed.canonical_url,
            name=info.get("title"),
            api_source="manual",
        )
        s.add(source)
        s.flush()
        collection = Collection(
            source_id=source.id,
            type="playlist" if parsed.kind == "playlist" else "channel_videos",
            youtube_playlist_id=parsed.playlist_id,
            youtube_channel_id=parsed.channel_id or info.get("channel_id"),
            title=info.get("title"),
            url=parsed.canonical_url,
        )
        s.add(collection)
        s.flush()
        collection_id = collection.id

    child_ids: list[int] = []
    skipped = 0
    with session_scope() as s:
        for pos, entry in enumerate(entries):
            vid = entry.get("id")
            if not is_video_id(vid):
                skipped += 1
                continue
            s.add(
                CollectionItem(
                    collection_id=collection_id,
                    youtube_video_id=vid,
                    position=pos,
                    last_seen_at=utcnow(),
                    raw_json={
                        k: entry.get(k)
                        for k in ("id", "title", "url", "ie_key", "_type")
                    },
                )
            )
            child = jobs_svc.create_download_child_job(
                s, vid, profile_name, parent_job_id=job_id, collection_id=collection_id
            )
            child_ids.append(child.id)

    submitted = _submit_children(child_ids)

    with session_scope() as s:
        job = s.get(Job, job_id)
        job.collection_id = collection_id
        jobs_svc.mark_success(s, job)
    logger.info(
        "expand: job %s -> %d entries, %d children enqueued (%d submitted to RQ), "
        "%d skipped (non-video)",
        job_id,
        total,
        len(child_ids),
        submitted,
        skipped,
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
