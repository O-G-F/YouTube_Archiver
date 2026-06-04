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

import hashlib
import json
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import Job, MetadataSnapshot, Video, utcnow
from app.services import comment_policy
from app.services import expand as expand_svc
from app.services import jobs as jobs_svc
from app.services import live_chat as live_chat_svc
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
        elif jtype in ("metadata_refresh", "comments_refresh"):
            _run_metadata_refresh(settings, job_id)
        elif jtype == "live_chat_refresh":
            _run_live_chat_refresh(settings, job_id)
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
    """Handles both ``metadata_refresh`` and ``comments_refresh`` job types.

    Never re-downloads the video body (``--skip-download`` + the metadata/comment
    profiles). Writes an info.json snapshot to a per-run snapshot dir (the
    original video.info.json is never overwritten), normalizes comments with
    diff detection, and (for comments_refresh) updates adaptive scheduling.
    """
    with session_scope() as s:
        job = s.get(Job, job_id)
        video = s.get(Video, job.video_id) if job.video_id else None
        if video is None:
            jobs_svc.mark_failed(s, job, "refresh: job has no associated video")
            return
        youtube_video_id = video.youtube_video_id
        video_pk = video.id
        profile_name = job.profile_name or "comments_refresh_only"
        is_comments = job.type == "comments_refresh"
        jobs_svc.mark_running(s, job)

    # Rate control: space out consecutive comment refresh jobs (avoid 429).
    if is_comments and settings.comments_refresh_job_delay_seconds > 0:
        time.sleep(settings.comments_refresh_job_delay_seconds)

    max_comments = (
        settings.comment_refresh_max_comments if is_comments else settings.ytdlp_max_comments
    )
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
        max_comments=max_comments,
        retry_sleep=settings.ytdlp_retry_backoff_seconds,
        **external_ctx(settings),
    )
    argv = build_ytdlp_args(spec, ctx)
    log_dir = storage.job_log_dir(settings, job_id)
    run = run_ytdlp(
        argv, log_dir, url=url, settings=settings, timeout=settings.ytdlp_timeout or None
    )

    err_tail = _tail(run.stderr_path)
    rate_limited = ("HTTP Error 429" in err_tail) or ("Too Many Requests" in err_tail)
    state = comment_policy.classify_comment_state(err_tail)

    with session_scope() as s:
        job = s.get(Job, job_id)
        job.log_path = storage.log_relative(settings, log_dir)
        job.command_path = storage.log_relative(settings, run.command_path)
        video = s.get(Video, video_pk)

        info_json = work_dir / f"{youtube_video_id}.info.json"
        got_info = info_json.is_file()
        snapshot_id: int | None = None
        summary = {"fetched": 0, "new": 0, "updated": 0, "refound": 0,
                   "marked_missing": 0, "unchanged": 0}
        fetched = 0
        capped = False
        if got_info:
            snap = MetadataSnapshot(
                video_id=video_pk,
                source="yt-dlp",
                snapshot_type="comments_refresh" if is_comments else "metadata_refresh",
                path=storage.to_relative(settings, info_json),
                checksum=_sha256_file(info_json),
                fetched_at=utcnow(),
            )
            s.add(snap)
            s.flush()
            snapshot_id = snap.id
            disk_info = _load_json(info_json)
            if disk_info:
                # Refresh the video row's metadata (title/upload_date/availability)
                # from the fetched info.json (the original file is untouched).
                upsert_video_from_info(s, disk_info, settings)
                fetched = len(disk_info.get("comments") or [])
                capped = bool(max_comments and max_comments > 0 and fetched >= max_comments)
                # mark_missing only on a complete (not capped) successful comment
                # fetch with comments enabled -> avoids mislabeling real comments.
                mark_missing = is_comments and run.ok and not capped and state is None
                summary = ingest_comments_from_info(
                    s, video, disk_info, snapshot_id=snapshot_id, mark_missing=mark_missing
                )
            video.last_metadata_refresh_at = utcnow()

        # ----- adaptive comment scheduling / state (comments_refresh only) -----
        next_refresh = None
        if is_comments:
            now = utcnow()
            video.comments_state = state  # None when ok
            video.last_comments_refresh_at = now
            if rate_limited:
                # 429: back off (and bump failure count / max-retry handling).
                next_refresh = comment_policy.apply_comment_backoff(video, now, settings)
            else:
                video.comment_refresh_failures = 0
                next_refresh = comment_policy.compute_next_comment_refresh(video, now)
                video.next_comments_refresh_at = next_refresh

        job.meta = {
            **(job.meta or {}),
            "target_video_id": youtube_video_id,
            "fetched_comments_count": fetched,
            "inserted_count": summary["new"],
            "updated_count": summary["updated"],
            "marked_missing_count": summary["marked_missing"],
            "refound_count": summary["refound"],
            "snapshot_id": snapshot_id,
            "capped": capped,
            "comments_state": state,
            "rate_limited": rate_limited,
            "next_comments_refresh_at": next_refresh.isoformat() if next_refresh else None,
        }

        if run.ok:
            jobs_svc.mark_success(s, job)
            logger.info(
                "%s: job %s fetched=%d new=%d updated=%d missing=%d state=%s",
                job.type, job_id, fetched, summary["new"], summary["updated"],
                summary["marked_missing"], state,
            )
        elif got_info:
            jobs_svc.mark_partial_success(
                s,
                job,
                f"yt-dlp exited {run.returncode} but info/comments were saved. {err_tail}",
            )
            if rate_limited:
                job.meta = {**(job.meta or {}), "retryable": True, "reason": "http_429"}
        else:
            jobs_svc.mark_failed(
                s, job, f"yt-dlp exited {run.returncode}\n{err_tail}"
            )
            if rate_limited:
                job.meta = {**(job.meta or {}), "retryable": True, "reason": "http_429"}


# --------------------------------------------------------------------------- #
# live_chat_refresh (live chat only; never re-downloads the body)
# --------------------------------------------------------------------------- #
def _run_live_chat_refresh(settings: Settings, job_id: int) -> None:
    """Fetch & normalize a video's archived live chat.

    Uses the ``live_chat_refresh_only`` profile: ``--skip-download`` +
    ``--write-subs --sub-langs live_chat`` -> ``<id>.live_chat.json`` (JSONL).
    The video body is NEVER re-downloaded. A normal (non-live) video simply has
    no live chat track -> ``live_chat_state=not_available`` (not an error).
    """
    with session_scope() as s:
        job = s.get(Job, job_id)
        video = s.get(Video, job.video_id) if job.video_id else None
        if video is None:
            jobs_svc.mark_failed(s, job, "live_chat_refresh: job has no associated video")
            return
        youtube_video_id = video.youtube_video_id
        video_pk = video.id
        profile_name = job.profile_name or "live_chat_refresh_only"
        jobs_svc.mark_running(s, job)

    if settings.comments_refresh_job_delay_seconds > 0:
        time.sleep(settings.comments_refresh_job_delay_seconds)

    url = canonical_video_url(youtube_video_id)
    with session_scope() as s:
        spec = get_profile_spec(s, profile_name)

    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    work_dir = storage.snapshot_dir(settings, youtube_video_id) / f"livechat_{stamp}"
    out_tpl = str(work_dir / "%(id)s.%(ext)s")
    ctx = BuildContext(
        output_template=out_tpl,
        download_archive=None,  # never skipped, never re-DL
        no_playlist=True,
        default_sub_langs=settings.default_sub_langs,
        archive_sub_langs=settings.archive_sub_langs,
        retry_sleep=settings.ytdlp_retry_backoff_seconds,
        **external_ctx(settings),
    )
    argv = build_ytdlp_args(spec, ctx)
    log_dir = storage.job_log_dir(settings, job_id)
    run = run_ytdlp(
        argv, log_dir, url=url, settings=settings, timeout=settings.ytdlp_timeout or None
    )

    err_tail = _tail(run.stderr_path)
    rate_limited = ("HTTP Error 429" in err_tail) or ("Too Many Requests" in err_tail)
    unavail = comment_policy.classify_comment_state(err_tail)

    live_chat_file = work_dir / f"{youtube_video_id}.live_chat.json"
    info_json = work_dir / f"{youtube_video_id}.info.json"

    with session_scope() as s:
        job = s.get(Job, job_id)
        job.log_path = storage.log_relative(settings, log_dir)
        job.command_path = storage.log_relative(settings, run.command_path)
        video = s.get(Video, video_pk)

        if info_json.is_file():
            disk_info = _load_json(info_json)
            if disk_info:
                upsert_video_from_info(s, disk_info, settings)

        snapshot_id: int | None = None
        summary = {"fetched": 0, "new": 0, "updated": 0, "refound": 0,
                   "marked_missing": 0, "unchanged": 0, "capped": False}
        got_chat = live_chat_file.is_file()
        if got_chat:
            snap = MetadataSnapshot(
                video_id=video_pk,
                source="yt-dlp",
                snapshot_type="live_chat_refresh",
                path=storage.to_relative(settings, live_chat_file),
                checksum=_sha256_file(live_chat_file),
                fetched_at=utcnow(),
            )
            s.add(snap)
            s.flush()
            snapshot_id = snap.id
            text = _read_text(live_chat_file)
            messages = live_chat_svc.parse_live_chat_jsonl(text)
            summary = live_chat_svc.ingest_live_chat(
                s, video, messages,
                snapshot_id=snapshot_id,
                limit=settings.live_chat_max_messages or None,
                mark_missing=False,
            )

        # ----- live chat state -----
        if got_chat and summary["fetched"] > 0:
            live_chat_state = "available"
            video.has_live_chat = True
        elif unavail == "unavailable" and not info_json.is_file():
            live_chat_state = "unavailable"
        else:
            # got info but no chat track, or an empty chat -> normal video
            live_chat_state = "not_available"

        now = utcnow()
        video.live_chat_state = live_chat_state
        video.last_live_chat_refresh_at = now
        next_refresh = comment_policy.compute_next_live_chat_refresh(video, now, settings)
        if rate_limited and not got_chat:
            # 429: back off and retry later (don't freeze).
            next_refresh = now + timedelta(
                seconds=settings.comments_refresh_retry_backoff_seconds or 21600
            )
        video.next_live_chat_refresh_at = next_refresh

        job.meta = {
            **(job.meta or {}),
            "target_video_id": youtube_video_id,
            "fetched_messages_count": summary["fetched"],
            "inserted_count": summary["new"],
            "updated_count": summary["updated"],
            "marked_missing_count": summary["marked_missing"],
            "refound_count": summary["refound"],
            "snapshot_id": snapshot_id,
            "live_chat_state": live_chat_state,
            "capped": summary["capped"],
            "rate_limited": rate_limited,
            "next_live_chat_refresh_at": next_refresh.isoformat() if next_refresh else None,
        }

        if rate_limited and not got_chat:
            jobs_svc.mark_failed(
                s, job, f"yt-dlp exited {run.returncode} (rate limited)\n{err_tail}"
            )
            job.meta = {**(job.meta or {}), "retryable": True, "reason": "http_429"}
        else:
            # Lenient: a missing live chat track is normal, not a failure.
            jobs_svc.mark_success(s, job)
            logger.info(
                "live_chat_refresh: job %s state=%s fetched=%d new=%d",
                job_id, live_chat_state, summary["fetched"], summary["new"],
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


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail(path: Path, max_chars: int = 4000) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]
