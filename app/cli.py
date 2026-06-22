"""Command-line interface (requirement 10).

Phase 0-1 commands are fully implemented. Later-phase commands (Takeout import,
diary generation, channel post-tab crawling) are intentionally absent rather
than left as TODO stubs.

Examples:
    archiver init
    archiver server
    archiver worker
    archiver profiles list
    archiver download enqueue "https://youtu.be/VIDEO" --profile video_best_archive
    archiver download enqueue "https://youtu.be/VIDEO" --now      # run inline, no Redis
    archiver download run                                          # drain queued jobs inline
    archiver source add-url "https://www.youtube.com/playlist?list=..."
    archiver source add-channel "https://www.youtube.com/@ex" --videos --shorts
    archiver comments refresh --video-id VIDEO_ID --now
    archiver jobs list --status failed
    archiver jobs retry 12
"""

from __future__ import annotations

import sys
import time

import typer
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.logging_setup import get_logger
from app.models import Job, Video
from app.services import jobs as jobs_svc
from app.services.profiles import get_profile_spec, list_profile_names
from app.services.urls import UrlError, normalize_url

logger = get_logger(__name__)

app = typer.Typer(add_completion=False, help="YouTube Archiver CLI (Phase 0-1).")
source_app = typer.Typer(help="Register sources (URLs, playlists, channels).")
download_app = typer.Typer(help="Enqueue and run download jobs.")
jobs_app = typer.Typer(help="Inspect and control jobs.")
comments_app = typer.Typer(help="Comment / metadata refresh (no body re-download).")
live_chat_app = typer.Typer(help="Live chat refresh (no body re-download).")
subtitles_app = typer.Typer(help="Subtitles-only refresh (no body re-download).")
profiles_app = typer.Typer(help="Download profiles.")
collections_app = typer.Typer(help="Inspect and re-crawl playlist/channel collections.")
scheduler_app = typer.Typer(help="Run the collection re-crawl scheduler.")
scheduler_runs_app = typer.Typer(help="Scheduler run history (Phase 7E).")
takeout_app = typer.Typer(help="Google Takeout preview / import.")
takeout_sessions_app = typer.Typer(help="Takeout import session history (Phase 6C).")
watch_history_app = typer.Typer(help="Inspect imported watch history.")
search_history_app = typer.Typer(help="Inspect imported search history.")
subscriptions_app = typer.Typer(help="Takeout subscriptions (channels).")
liked_videos_app = typer.Typer(help="Takeout liked videos library.")
library_app = typer.Typer(help="Hybrid library bootstrap (Takeout + API).")
youtube_api_app = typer.Typer(help="YouTube Data API OAuth (differential liked sync).")
doctor_app = typer.Typer(help="Environment diagnostics (general + YouTube fetch stability).")
youtube_diag_app = typer.Typer(help="YouTube fetch-stability diagnostics (benchmark).")
queue_app = typer.Typer(help="Job queue health (Phase 7D).")
storage_app = typer.Typer(help="Storage / database stats (Phase 6E).")
system_app = typer.Typer(help="Build identity / preflight checks (Phase 6F).")
app.add_typer(source_app, name="source")
app.add_typer(download_app, name="download")
app.add_typer(jobs_app, name="jobs")
app.add_typer(comments_app, name="comments")
app.add_typer(live_chat_app, name="live-chat")
app.add_typer(subtitles_app, name="subtitles")
app.add_typer(profiles_app, name="profiles")
app.add_typer(collections_app, name="collections")
app.add_typer(scheduler_app, name="scheduler")
scheduler_app.add_typer(scheduler_runs_app, name="runs")
app.add_typer(takeout_app, name="takeout")
takeout_app.add_typer(takeout_sessions_app, name="sessions")
app.add_typer(watch_history_app, name="watch-history")
app.add_typer(search_history_app, name="search-history")
app.add_typer(subscriptions_app, name="subscriptions")
app.add_typer(liked_videos_app, name="liked-videos")
app.add_typer(library_app, name="library")
app.add_typer(youtube_api_app, name="youtube-api")
app.add_typer(doctor_app, name="doctor")
app.add_typer(youtube_diag_app, name="youtube-diagnostics")
app.add_typer(queue_app, name="queue")
app.add_typer(storage_app, name="storage")
app.add_typer(system_app, name="system")


# --------------------------------------------------------------------------- #
# top-level
# --------------------------------------------------------------------------- #
@app.command()
def init() -> None:
    """Create the schema (SQLite) and seed the built-in download profiles."""
    from app.bootstrap import init as bootstrap_init

    written = bootstrap_init(create_schema=True)
    typer.echo(f"Initialized. Built-in profiles seeded/updated: {written}")


@app.command()
def server(
    host: str = typer.Option("0.0.0.0", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
    reload: bool = typer.Option(False, help="Auto-reload (development)."),
) -> None:
    """Run the FastAPI web server (uvicorn)."""
    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


@app.command()
def worker() -> None:
    """Start an RQ worker that consumes download/refresh jobs."""
    import threading

    from redis import Redis
    from rq import Worker

    from app.services import build_info
    from app.worker.queue import get_redis

    settings = get_settings()
    settings.ensure_dirs()
    conn = get_redis()

    # Publish a short-TTL heartbeat (carrying this worker's build_id) so
    # `system preflight` can detect a STALE worker — one running older code than
    # web — before a large import. A dead worker's key expires automatically.
    #
    # IMPORTANT: the heartbeat uses its OWN Redis connection — never the one RQ's
    # blocking worker loop holds. Sharing a single client across the heartbeat
    # thread and RQ's listener corrupts RQ's connection ("Redis connection
    # timeout, quitting") and stops job processing.
    def _heartbeat_loop() -> None:
        hb_conn = Redis.from_url(settings.redis_url)
        while True:
            try:
                build_info.write_worker_heartbeat(hb_conn)
            except Exception:  # noqa: BLE001 - never let heartbeat kill the worker
                pass
            time.sleep(build_info.WORKER_HEARTBEAT_REFRESH_SECONDS)

    threading.Thread(target=_heartbeat_loop, daemon=True).start()

    typer.echo(
        f"Starting RQ worker on queue {settings.rq_queue!r} "
        f"(build_id={build_info.build_id()}) ..."
    )
    Worker([settings.rq_queue], connection=conn).work(with_scheduler=False)


@doctor_app.callback(invoke_without_command=True)
def doctor_main(ctx: typer.Context) -> None:
    """Diagnose storage writability, tool versions, and DB/Redis connectivity."""
    if ctx.invoked_subcommand is not None:
        return
    from app.services.doctor import run_diagnostics

    result = run_diagnostics(get_settings())
    for c in result["checks"]:
        mark = "OK  " if c["ok"] else "FAIL"
        typer.echo(f"[{mark}] {c['name']:<14} {c['detail']}")
    typer.echo(f"\noverall: {'OK' if result['ok'] else 'PROBLEMS DETECTED'}")
    if not result["ok"]:
        raise typer.Exit(code=1)


@doctor_app.command("youtube")
def doctor_youtube(
    test_url: str = typer.Option("", "--test-url", help="Run a live test against this URL."),
    profile: str = typer.Option("", "--profile", help="Video profile for the optional video test."),
    video: bool = typer.Option(False, "--video", help="Also run a small video download test (default off)."),
    timeout: int = typer.Option(0, "--timeout"),
) -> None:
    """YouTube fetch-stability check (static env) + optional live test (no secrets shown)."""
    from app.services import youtube_doctor as yd

    s = get_settings()
    st = yd.static_checks(s)
    typer.echo("== static checks ==")
    for c in st["checks"]:
        mark = {"ok": "OK  ", "warning": "WARN", "failed": "FAIL"}.get(c["status"], "?")
        typer.echo(f"[{mark}] {c['name']:<26} {c['detail']}")
    typer.echo(f"\ncookies: configured={st['cookies']['configured']} file_exists={st['cookies']['file_exists']} "
               f"readable={st['cookies']['readable']}")
    typer.echo(f"browser_cookies={st['browser_cookies_configured']} po_token={st['po_token_configured']} "
               f"visitor_data={st['visitor_data_configured']} curl_cffi={st['curl_cffi_installed']} "
               f"impersonate_targets={st['impersonate_targets']}")
    typer.echo("recommendations:")
    for r in st["recommendations"]:
        typer.echo(f"  - {r}")
    if test_url:
        typer.echo("\n== live test (downloads into a temp dir; nothing persisted) ==")
        report = yd.run_diagnostics(
            s, test_url, profile=(profile or None), include_video_download=video,
            timeout=(timeout or None),
        )
        typer.echo(f"overall: {report['overall']}")
        for step in report["steps"]:
            typer.echo(f"  {step['name']:<16} {step['status']:<8} {step['duration_seconds']}s "
                       f"reasons={step['classification']['reasons']} media_body_created={step['media_body_created']}")
        typer.echo("recommendations:")
        for r in report["recommendations"]:
            typer.echo(f"  - {r}")


@youtube_diag_app.command("run")
def youtube_diagnostics_run(
    url: str = typer.Option(..., "--url", help="Video URL to test."),
    profile: str = typer.Option("", "--profile", help="Video profile for the video test."),
    video: bool = typer.Option(False, "--video", help="Include a small video download test (default off)."),
    timeout: int = typer.Option(0, "--timeout"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Create a youtube_diagnostic job (metadata + subtitles + optional video)."""
    with session_scope() as s:
        job = jobs_svc.create_youtube_diagnostic_job(
            s, url, profile=(profile or None), include_video_download=video,
            timeout=(timeout or None),
        )
        job_id = job.id
    typer.echo(f"Created youtube_diagnostic job #{job_id} for {url}")
    _dispatch(job_id, now)
    if now:
        with session_scope() as s:
            j = s.get(Job, job_id)
            rep = (j.meta or {}).get("diagnostic") or {}
            typer.echo(f"overall: {rep.get('overall')}")
            for step in rep.get("steps", []):
                typer.echo(f"  {step['name']:<16} {step['status']:<8} reasons={step['classification']['reasons']} "
                           f"media_body_created={step['media_body_created']}")
            for r in (j.meta or {}).get("recommendations", []):
                typer.echo(f"  - {r}")


# --------------------------------------------------------------------------- #
# profiles
# --------------------------------------------------------------------------- #
@profiles_app.command("list")
def profiles_list() -> None:
    """List download profiles."""
    from app.models import DownloadProfile

    with session_scope() as s:
        rows = list(s.scalars(select(DownloadProfile).order_by(DownloadProfile.name)))
        if not rows:
            typer.echo("No profiles in DB. Run `archiver init`. Built-ins:")
            for name in list_profile_names():
                typer.echo(f"  - {name}")
            return
        for r in rows:
            typer.echo(f"  {r.name:<24} {r.media_mode:<9} {r.quality_mode or '-':<8} {r.description}")


@profiles_app.command("command")
def profiles_command(
    profile: str = typer.Argument(..., help="Profile name."),
    url: str = typer.Argument(..., help="YouTube URL."),
) -> None:
    """Dry-run: print the yt-dlp command this profile would run (no execution).

    Cookie/secret paths are masked.
    """
    from app.services.command_builder import dry_run_command
    from app.services.urls import UrlError

    with session_scope() as s:
        try:
            result = dry_run_command(s, get_settings(), profile, url)
        except KeyError:
            raise typer.BadParameter(
                f"unknown profile {profile!r}. Known: {', '.join(list_profile_names())}"
            )
        except UrlError as exc:
            raise typer.BadParameter(str(exc))
    typer.echo(result["command"])


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
@download_app.command("enqueue")
def download_enqueue(
    url: str = typer.Argument(..., help="Video / playlist / channel URL."),
    profile: str = typer.Option(None, "--profile", "-p", help="Download profile."),
    now: bool = typer.Option(False, "--now", help="Run inline immediately (no Redis)."),
    priority: int = typer.Option(0, help="Higher runs first."),
) -> None:
    """Create a job for a URL and either submit it to RQ or run it inline."""
    profile = profile or get_settings().default_profile
    _ensure_profile(profile)
    try:
        with session_scope() as s:
            job = jobs_svc.create_job_for_url(s, url, profile, priority=priority)
            job_id = job.id
            job_type = job.type
    except UrlError as exc:
        raise typer.BadParameter(str(exc))
    typer.echo(f"Created {job_type} job #{job_id} ({profile}) for {url}")
    _dispatch(job_id, now)


@download_app.command("run")
def download_run(
    limit: int = typer.Option(0, help="Max jobs to run (0 = all queued)."),
) -> None:
    """Drain queued jobs by running them inline (no Redis required)."""
    from app.worker.tasks import run_job

    with session_scope() as s:
        stmt = select(Job).where(Job.status == "queued").order_by(
            Job.priority.desc(), Job.id.asc()
        )
        if limit:
            stmt = stmt.limit(limit)
        ids = [j.id for j in s.scalars(stmt)]
    if not ids:
        typer.echo("No queued jobs.")
        return
    typer.echo(f"Running {len(ids)} queued job(s) inline ...")
    for jid in ids:
        typer.echo(f"  -> job #{jid}")
        run_job(jid)
    _print_job_summary(ids)


# --------------------------------------------------------------------------- #
# source
# --------------------------------------------------------------------------- #
@source_app.command("add-url")
def source_add_url(
    url: str = typer.Argument(..., help="Any YouTube URL."),
    profile: str = typer.Option(None, "--profile", "-p"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Register and enqueue any URL (alias of `download enqueue`)."""
    download_enqueue(url=url, profile=profile, now=now, priority=0)


@source_app.command("add-playlist")
def source_add_playlist(
    url: str = typer.Argument(..., help="Playlist URL."),
    profile: str = typer.Option(None, "--profile", "-p"),
    now: bool = typer.Option(False, "--now"),
    max_items: int = typer.Option(0, "--max-items", help="Cap items (0 = EXPAND_MAX_ITEMS)."),
) -> None:
    """Register a playlist and enqueue an expand job."""
    profile = profile or get_settings().default_profile
    _ensure_profile(profile)
    try:
        parsed = normalize_url(url)
    except UrlError as exc:
        raise typer.BadParameter(str(exc))
    if parsed.kind != "playlist":
        raise typer.BadParameter("not a playlist URL")
    _expand_and_dispatch(url, profile, now, max_items)


@source_app.command("expand")
def source_expand(
    url: str = typer.Argument(..., help="Playlist or channel(-tab) URL."),
    profile: str = typer.Option(None, "--profile", "-p"),
    now: bool = typer.Option(False, "--now"),
    max_items: int = typer.Option(0, "--max-items", help="Cap items (0 = EXPAND_MAX_ITEMS)."),
) -> None:
    """Expand a playlist/channel URL into child download jobs (diff-aware)."""
    profile = profile or get_settings().default_profile
    _ensure_profile(profile)
    try:
        parsed = normalize_url(url)
    except UrlError as exc:
        raise typer.BadParameter(str(exc))
    if parsed.kind not in ("playlist", "channel"):
        raise typer.BadParameter("not an expandable (playlist/channel) URL")
    _expand_and_dispatch(url, profile, now, max_items)


@source_app.command("add-channel")
def source_add_channel(
    url: str = typer.Argument(..., help="Channel URL (/@handle, /channel/UC...)."),
    profile: str = typer.Option(None, "--profile", "-p"),
    videos: bool = typer.Option(False, "--videos", help="Crawl the Videos tab."),
    shorts: bool = typer.Option(False, "--shorts", help="Crawl the Shorts tab."),
    streams: bool = typer.Option(False, "--streams", help="Crawl the Streams tab."),
    now: bool = typer.Option(False, "--now"),
    max_items: int = typer.Option(0, "--max-items", help="Cap items per tab (0 = EXPAND_MAX_ITEMS)."),
) -> None:
    """Register a channel by enqueuing one expand job per requested tab.

    A channel ROOT URL with no --videos/--shorts/--streams flag is rejected
    (avoids an accidental full crawl). A /videos|/shorts|/streams tab URL works
    without flags.
    """
    from app.services.urls import channel_tab_url, resolve_channel_tabs

    profile = profile or get_settings().default_profile
    _ensure_profile(profile)
    try:
        parsed = normalize_url(url)
    except UrlError as exc:
        raise typer.BadParameter(str(exc))
    if parsed.kind != "channel":
        raise typer.BadParameter("not a channel URL")

    try:
        tabs = resolve_channel_tabs(parsed, videos, shorts, streams)
    except UrlError as exc:
        raise typer.BadParameter(str(exc))

    for tab in tabs:
        _expand_and_dispatch(channel_tab_url(parsed, tab), profile, now, max_items)


# --------------------------------------------------------------------------- #
# comments / metadata refresh
# --------------------------------------------------------------------------- #
@comments_app.command("refresh")
def comments_refresh(
    video_or_url: str = typer.Argument(..., help="YouTube video id or URL."),
    profile: str = typer.Option("comments_refresh_only", "--profile", "-p"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Refresh comments for a video WITHOUT re-downloading the body."""
    _comments_refresh_one(video_or_url, profile, now)


@comments_app.command("refresh-video")
def comments_refresh_video(
    video_id: str = typer.Argument(..., help="YouTube video id."),
    profile: str = typer.Option("comments_refresh_only", "--profile", "-p"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Refresh comments for a video id (alias of `refresh`)."""
    _comments_refresh_one(video_id, profile, now)


@comments_app.command("refresh-all")
def comments_refresh_all(
    profile: str = typer.Option("comments_refresh_only", "--profile", "-p"),
    limit_videos: int = typer.Option(
        50, "--limit-videos", help="Max videos to enqueue (safety cap; 0 = unlimited)."
    ),
    due_only: bool = typer.Option(
        True, "--due-only/--all",
        help="--due-only: only videos due per policy. --all: every non-frozen video.",
    ),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Enqueue comment refresh for DUE videos (``--due-only``) or all (``--all``)."""
    from datetime import datetime, timezone

    from app.services import comment_policy

    _ensure_profile(profile)
    job_ids: list[int] = []
    with session_scope() as s:
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        videos = comment_policy.select_refreshable_videos(
            s, now_dt, (limit_videos or None), due_only=due_only
        )
        for v in videos:
            job_ids.append(
                jobs_svc.create_comments_refresh_job(s, v, profile_name=profile).id
            )
        selected = len(videos)
    scope = "due" if due_only else "all"
    typer.echo(
        f"Selected {selected} {scope} video(s); created {len(job_ids)} comments_refresh job(s)."
    )
    for jid in job_ids:
        _dispatch(jid, now)


@comments_app.command("due")
def comments_due(
    limit: int = typer.Option(50, "--limit", "-n", help="Max videos to show."),
) -> None:
    """List videos currently DUE for a comment refresh (adaptive policy)."""
    from datetime import datetime, timezone

    from app.services import comment_policy

    with session_scope() as s:
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        videos = comment_policy.select_due_videos(s, now_dt, (limit or None))
        if not videos:
            typer.echo("No videos are due for a comment refresh.")
            return
        typer.echo(f"{len(videos)} due video(s):")
        for v in videos:
            reason = "never" if v.next_comments_refresh_at is None else "due"
            typer.echo(
                f"  {v.youtube_video_id}  [{reason:<5}] next={v.next_comments_refresh_at} "
                f"state={v.comments_state or '-'}  {(v.title or '')[:50]}"
            )


@comments_app.command("schedule")
def comments_schedule(
    video_id: str = typer.Argument(..., help="YouTube video id."),
    now: bool = typer.Option(
        False, "--now-due", help="Mark due immediately (next_comments_refresh_at = now)."
    ),
) -> None:
    """Recompute (or force) a video's next comment refresh time."""
    from datetime import datetime, timezone

    from app.services import comment_policy

    with session_scope() as s:
        video = s.scalar(select(Video).where(Video.youtube_video_id == video_id))
        if video is None:
            raise typer.BadParameter(f"video {video_id!r} is not in the DB")
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        if now:
            video.next_comments_refresh_at = now_dt
        else:
            video.next_comments_refresh_at = comment_policy.compute_next_comment_refresh(
                video, now_dt
            )
        nxt = video.next_comments_refresh_at
    typer.echo(f"{video_id}: next_comments_refresh_at = {nxt}")


@comments_app.command("list")
def comments_list(
    video_id: str = typer.Argument(..., help="YouTube video id."),
    limit: int = typer.Option(50, "--limit"),
    active_only: bool = typer.Option(False, "--active-only", help="Exclude missing/deleted."),
) -> None:
    """List comments for a video."""
    from app.models import Comment

    with session_scope() as s:
        video = s.scalar(select(Video).where(Video.youtube_video_id == video_id))
        if video is None:
            raise typer.BadParameter(f"video {video_id!r} is not in the DB")
        stmt = select(Comment).where(Comment.video_id == video.id)
        if active_only:
            stmt = stmt.where(Comment.is_deleted_or_missing.is_(False))
        stmt = stmt.order_by(Comment.like_count.desc().nulls_last(), Comment.id.asc()).limit(limit)
        rows = list(s.scalars(stmt))
        if not rows:
            typer.echo("No comments. Run `comments refresh` first.")
            return
        for c in rows:
            flag = " (missing)" if c.is_deleted_or_missing else ""
            typer.echo(f"  [{c.like_count or 0:>5}] {c.author_name or '-'}{flag}: {(c.text or '')[:60]}")


@comments_app.command("stats")
def comments_stats(video_id: str = typer.Argument(..., help="YouTube video id.")) -> None:
    """Show comment statistics for a video."""
    from sqlalchemy import func

    from app.models import Comment

    with session_scope() as s:
        video = s.scalar(select(Video).where(Video.youtube_video_id == video_id))
        if video is None:
            raise typer.BadParameter(f"video {video_id!r} is not in the DB")
        total = s.scalar(select(func.count(Comment.id)).where(Comment.video_id == video.id)) or 0
        missing = s.scalar(
            select(func.count(Comment.id)).where(
                Comment.video_id == video.id, Comment.is_deleted_or_missing.is_(True)
            )
        ) or 0
        distinct = s.scalar(
            select(func.count(func.distinct(Comment.author_channel_id))).where(
                Comment.video_id == video.id
            )
        ) or 0
    typer.echo(f"video            : {video_id}")
    typer.echo(f"total comments   : {total}")
    typer.echo(f"active           : {total - missing}")
    typer.echo(f"missing/deleted  : {missing}")
    typer.echo(f"distinct authors : {distinct}")
    typer.echo(f"comments_state   : {video.comments_state}")
    typer.echo(f"last refresh     : {video.last_comments_refresh_at}")
    typer.echo(f"next refresh     : {video.next_comments_refresh_at}")


@comments_app.command("snapshots")
def comments_snapshots(
    video_id: str = typer.Argument(..., help="YouTube video id."),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """List metadata snapshots captured for a video."""
    from app.models import MetadataSnapshot

    with session_scope() as s:
        video = s.scalar(select(Video).where(Video.youtube_video_id == video_id))
        if video is None:
            raise typer.BadParameter(f"video {video_id!r} is not in the DB")
        snaps = list(
            s.scalars(
                select(MetadataSnapshot)
                .where(MetadataSnapshot.video_id == video.id)
                .order_by(MetadataSnapshot.id.desc())
                .limit(limit)
            )
        )
        if not snaps:
            typer.echo("No snapshots.")
            return
        for sn in snaps:
            cs = sn.checksum[:12] if sn.checksum else "-"
            typer.echo(f"  #{sn.id} {sn.snapshot_type:<18} {sn.fetched_at}  {cs}  {sn.path}")


# --------------------------------------------------------------------------- #
# live chat
# --------------------------------------------------------------------------- #
@live_chat_app.command("refresh")
def live_chat_refresh(
    video_or_url: str = typer.Argument(..., help="YouTube video id or URL."),
    profile: str = typer.Option("live_chat_refresh_only", "--profile", "-p"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Refresh a video's live chat WITHOUT re-downloading the body."""
    _ensure_profile(profile)
    with session_scope() as s:
        video = jobs_svc.resolve_or_create_video(s, video_or_url)
        if video is None:
            raise typer.BadParameter(f"could not resolve video: {video_or_url!r}")
        job = jobs_svc.create_live_chat_refresh_job(s, video, profile_name=profile)
        job_id = job.id
    typer.echo(f"Created live_chat_refresh job #{job_id} for {video_or_url}")
    _dispatch(job_id, now)


@live_chat_app.command("refresh-all")
def live_chat_refresh_all(
    profile: str = typer.Option("live_chat_refresh_only", "--profile", "-p"),
    limit_videos: int = typer.Option(
        25, "--limit-videos", help="Max videos to enqueue (safety cap; 0 = unlimited)."
    ),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Enqueue live chat refresh for videos that have (or had) live chat and are due."""
    from datetime import datetime, timezone

    from app.services import comment_policy

    _ensure_profile(profile)
    job_ids: list[int] = []
    with session_scope() as s:
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        videos = comment_policy.select_due_live_chat_videos(s, now_dt, (limit_videos or None))
        for v in videos:
            job_ids.append(
                jobs_svc.create_live_chat_refresh_job(s, v, profile_name=profile).id
            )
        selected = len(videos)
    typer.echo(f"Selected {selected} video(s); created {len(job_ids)} live_chat_refresh job(s).")
    for jid in job_ids:
        _dispatch(jid, now)


@live_chat_app.command("list")
def live_chat_list(
    video_id: str = typer.Argument(..., help="YouTube video id."),
    limit: int = typer.Option(50, "--limit"),
    superchats_only: bool = typer.Option(False, "--superchats-only"),
) -> None:
    """List live chat messages for a video (most-recent-by-timestamp last)."""
    from app.models import LiveChatMessage

    with session_scope() as s:
        video = s.scalar(select(Video).where(Video.youtube_video_id == video_id))
        if video is None:
            raise typer.BadParameter(f"video {video_id!r} is not in the DB")
        stmt = select(LiveChatMessage).where(LiveChatMessage.video_id == video.id)
        if superchats_only:
            stmt = stmt.where(LiveChatMessage.is_superchat.is_(True))
        stmt = stmt.order_by(
            LiveChatMessage.timestamp_ms.asc().nulls_last(), LiveChatMessage.id.asc()
        ).limit(limit)
        rows = list(s.scalars(stmt))
        if not rows:
            typer.echo("No live chat messages. Run `live-chat refresh` first.")
            return
        for m in rows:
            money = f" [{m.amount_text}]" if m.amount_text else ""
            flag = " (missing)" if m.is_deleted_or_missing else ""
            typer.echo(
                f"  {m.time_text or '-':>8} {m.author_name or '-'}{money}{flag}: "
                f"{(m.message or '')[:60]}"
            )


@live_chat_app.command("stats")
def live_chat_stats(video_id: str = typer.Argument(..., help="YouTube video id.")) -> None:
    """Show live chat statistics for a video."""
    from sqlalchemy import func

    from app.models import LiveChatMessage

    with session_scope() as s:
        video = s.scalar(select(Video).where(Video.youtube_video_id == video_id))
        if video is None:
            raise typer.BadParameter(f"video {video_id!r} is not in the DB")

        def _count(*conds) -> int:
            return int(
                s.scalar(
                    select(func.count(LiveChatMessage.id)).where(
                        LiveChatMessage.video_id == video.id, *conds
                    )
                )
                or 0
            )

        total = _count()
        missing = _count(LiveChatMessage.is_deleted_or_missing.is_(True))
        superchats = _count(LiveChatMessage.is_superchat.is_(True))
        members = _count(LiveChatMessage.is_member_message.is_(True))
    typer.echo(f"video             : {video_id}")
    typer.echo(f"total messages    : {total}")
    typer.echo(f"active            : {total - missing}")
    typer.echo(f"missing/deleted   : {missing}")
    typer.echo(f"super chats       : {superchats}")
    typer.echo(f"member messages   : {members}")
    typer.echo(f"has_live_chat     : {video.has_live_chat}")
    typer.echo(f"live_chat_state   : {video.live_chat_state}")
    typer.echo(f"last refresh      : {video.last_live_chat_refresh_at}")
    typer.echo(f"next refresh      : {video.next_live_chat_refresh_at}")


# --------------------------------------------------------------------------- #
# subtitles (subtitle-only refresh; never re-downloads the body)
# --------------------------------------------------------------------------- #
@subtitles_app.command("refresh")
def subtitles_refresh(
    video_or_url: str = typer.Argument(..., help="YouTube video id or URL."),
    profile: str = typer.Option("subtitles_refresh_only", "--profile", "-p"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Re-fetch subtitles for a video WITHOUT re-downloading the body."""
    _ensure_profile(profile)
    with session_scope() as s:
        video = jobs_svc.resolve_or_create_video(s, video_or_url)
        if video is None:
            raise typer.BadParameter(f"could not resolve video: {video_or_url!r}")
        job = jobs_svc.create_subtitles_refresh_job(s, video, profile_name=profile)
        job_id = job.id
    typer.echo(f"Created subtitles_refresh job #{job_id} for {video_or_url}")
    _dispatch(job_id, now)


@subtitles_app.command("failed")
def subtitles_failed(limit: int = typer.Option(50, "--limit")) -> None:
    """List jobs whose subtitle download failed (candidates for a subtitles refresh)."""
    from app.services.job_classify import classify_job

    with session_scope() as s:
        rows = s.scalars(
            select(Job)
            .where(Job.status.in_(("failed", "partial_success")), Job.video_id.is_not(None))
            .order_by(Job.id.desc())
            .limit(500)
        )
        shown = 0
        for j in rows:
            c = classify_job(j)
            if "subtitles_failed" not in c["reasons"] and not (j.meta or {}).get("subtitles_failed"):
                continue
            typer.echo(
                f"  #{j.id:<5} {j.status:<15} {j.type:<18} video={j.video_id} "
                f"{(j.url or '')[:40]}"
            )
            shown += 1
            if shown >= limit:
                break
        if shown == 0:
            typer.echo("No jobs with failed subtitles.")


@subtitles_app.command("refresh-failed")
def subtitles_refresh_failed(
    limit: int = typer.Option(25, "--limit"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Create a subtitles_refresh job for each video that had a subtitles failure."""
    from app.services.job_classify import classify_job

    job_ids: list[int] = []
    with session_scope() as s:
        rows = s.scalars(
            select(Job)
            .where(Job.status.in_(("failed", "partial_success")), Job.video_id.is_not(None))
            .order_by(Job.id.desc())
            .limit(500)
        )
        seen: set[int] = set()
        for j in rows:
            if len(job_ids) >= limit:
                break
            c = classify_job(j)
            if "subtitles_failed" not in c["reasons"] and not (j.meta or {}).get("subtitles_failed"):
                continue
            if j.video_id in seen:
                continue
            seen.add(j.video_id)
            video = s.get(Video, j.video_id)
            if video is None:
                continue
            job_ids.append(jobs_svc.create_subtitles_refresh_job(s, video).id)
    typer.echo(f"Created {len(job_ids)} subtitles_refresh job(s).")
    for jid in job_ids:
        _dispatch(jid, now)


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
@jobs_app.command("list")
def jobs_list(
    status: str = typer.Option(None, "--status"),
    type: str = typer.Option(None, "--type"),
    limit: int = typer.Option(30, "--limit"),
) -> None:
    """List jobs (most recent first)."""
    with session_scope() as s:
        stmt = select(Job).order_by(Job.id.desc())
        if status:
            stmt = stmt.where(Job.status == status)
        if type:
            stmt = stmt.where(Job.type == type)
        rows = list(s.scalars(stmt.limit(limit)))
        if not rows:
            typer.echo("No jobs.")
            return
        for j in rows:
            err = f"  !! {j.error_message.splitlines()[0]}" if j.error_message else ""
            typer.echo(
                f"#{j.id:<5} {j.status:<9} {j.type:<16} {j.profile_name or '-':<22} "
                f"{(j.url or '')[:50]}{err}"
            )


@jobs_app.command("retry")
def jobs_retry(
    job_id: int = typer.Argument(...),
    now: bool = typer.Option(False, "--now"),
    force: bool = typer.Option(False, "--force", help="Bypass the retry-count cap."),
) -> None:
    """Reset a failed/canceled/partial job to queued and re-run/re-submit it."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise typer.BadParameter("job not found")
        if job.status not in ("failed", "canceled", "partial_success"):
            raise typer.BadParameter(f"cannot retry a {job.status} job")
        cap = get_settings().download_retry_max_attempts
        if not force and (job.retry_count or 0) >= cap:
            raise typer.BadParameter(
                f"retry cap reached ({job.retry_count}/{cap}); use --force to override"
            )
        jobs_svc.retry_job(s, job)
    typer.echo(f"Job #{job_id} reset to queued (retry #{job.retry_count}).")
    _dispatch(job_id, now)


@jobs_app.command("retryable")
def jobs_retryable(
    reason: str = typer.Option("", "--reason", help="filter by classification reason"),
    type: str = typer.Option("", "--type"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List failed/partial jobs that are classified retryable (under the attempt cap)."""
    from app.services.job_classify import classify_job

    cap = get_settings().download_retry_max_attempts
    with session_scope() as s:
        stmt = select(Job).where(Job.status.in_(("failed", "partial_success"))).order_by(Job.id.desc()).limit(500)
        if type:
            stmt = stmt.where(Job.type == type)
        shown = 0
        for j in s.scalars(stmt):
            if (j.retry_count or 0) >= cap:
                continue
            c = classify_job(j)
            if not c["retryable"]:
                continue
            if reason and reason not in c["reasons"]:
                continue
            typer.echo(
                f"  #{j.id:<5} {j.status:<15} {j.type:<18} retry={j.retry_count} "
                f"reasons={','.join(c['reasons']) or '-'}  {(j.url or '')[:40]}"
            )
            shown += 1
            if shown >= limit:
                break
        if shown == 0:
            typer.echo("No retryable jobs.")


@jobs_app.command("retry-all")
def jobs_retry_all(
    reason: str = typer.Option("", "--reason", help="only retry jobs with this reason"),
    type: str = typer.Option("", "--type"),
    limit: int = typer.Option(50, "--limit"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Re-queue all retryable jobs (optionally filtered by reason / type)."""
    from app.services.job_classify import classify_job

    cap = get_settings().download_retry_max_attempts
    job_ids: list[int] = []
    with session_scope() as s:
        stmt = select(Job).where(Job.status.in_(("failed", "partial_success"))).order_by(Job.id.desc()).limit(500)
        if type:
            stmt = stmt.where(Job.type == type)
        for j in s.scalars(stmt):
            if len(job_ids) >= limit:
                break
            if (j.retry_count or 0) >= cap:
                continue
            c = classify_job(j)
            if not c["retryable"] or (reason and reason not in c["reasons"]):
                continue
            jobs_svc.retry_job(s, j)
            job_ids.append(j.id)
    typer.echo(f"Re-queued {len(job_ids)} job(s).")
    for jid in job_ids:
        _dispatch(jid, now)


@jobs_app.command("cancel")
def jobs_cancel(job_id: int = typer.Argument(...)) -> None:
    """Cancel a queued/running job."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise typer.BadParameter("job not found")
        if job.status == "success":
            raise typer.BadParameter("cannot cancel a finished job")
        jobs_svc.mark_canceled(s, job)
    typer.echo(f"Job #{job_id} canceled.")


@jobs_app.command("logs")
def jobs_logs(
    job_id: int = typer.Argument(...),
    stdout: bool = typer.Option(False, "--stdout", help="Only the yt-dlp stdout log."),
    stderr: bool = typer.Option(False, "--stderr", help="Only the yt-dlp stderr log."),
    command: bool = typer.Option(False, "--command", help="Only the recorded command."),
    tail: int = typer.Option(0, "--tail", help="Show only the last N lines (0 = all)."),
) -> None:
    """Show a job's logs (command / stdout / stderr)."""
    from app.services import logs as logs_svc

    selected = [
        name
        for name, on in (("command", command), ("stdout", stdout), ("stderr", stderr))
        if on
    ] or ["command", "stdout", "stderr"]
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise typer.BadParameter("job not found")
        settings = get_settings()
        for stream in selected:
            if len(selected) > 1:
                typer.echo(f"===== {stream} =====")
            content = logs_svc.read_log(settings, job, stream, tail=tail or None)
            typer.echo(content if content is not None else f"(no {stream} log)")


@jobs_app.command("show")
def jobs_show(job_id: int = typer.Argument(...)) -> None:
    """Show detailed job info (status, paths, related video, profile)."""
    from app.models import DownloadProfile, Video
    from app.services import logs as logs_svc
    from app.services import storage

    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise typer.BadParameter("job not found")
        settings = get_settings()
        paths = logs_svc.relative_log_paths(settings, job)
        typer.echo(f"Job #{job.id}")
        typer.echo(f"  type        : {job.type}")
        typer.echo(f"  status      : {job.status}")
        typer.echo(f"  profile     : {job.profile_name}")
        typer.echo(f"  url         : {job.url}")
        typer.echo(f"  priority    : {job.priority}   progress: {job.progress}")
        typer.echo(f"  created_at  : {job.created_at}")
        typer.echo(f"  started_at  : {job.started_at}")
        typer.echo(f"  finished_at : {job.finished_at}")
        typer.echo(f"  log dir     : {job.log_path}")
        typer.echo(f"  command log : {paths['command']}")
        typer.echo(f"  stdout log  : {paths['stdout']}")
        typer.echo(f"  stderr log  : {paths['stderr']}")
        if job.video_id:
            v = s.get(Video, job.video_id)
            if v is not None:
                typer.echo(f"  video       : {v.youtube_video_id}  {v.title}")
                typer.echo(f"  channel     : {v.channel_title} ({v.channel_id})")
                if job.profile_name and job.type == "download":
                    prow = s.scalar(
                        select(DownloadProfile).where(
                            DownloadProfile.name == job.profile_name
                        )
                    )
                    if prow is not None:
                        out = storage.video_output_dir(
                            settings, prow.media_mode, v.channel_id, v.youtube_video_id
                        )
                        typer.echo(f"  output dir  : {storage.to_relative(settings, out)}")
        if job.meta:
            typer.echo(f"  meta        : {job.meta}")
        if job.error_message:
            typer.echo(f"  error/notes : {job.error_message.splitlines()[0]}")


# --------------------------------------------------------------------------- #
# collections
# --------------------------------------------------------------------------- #
@collections_app.command("list")
def collections_list() -> None:
    """List playlist/channel collections."""
    from sqlalchemy import func

    from app.models import Collection, CollectionItem

    with session_scope() as s:
        rows = list(s.scalars(select(Collection).order_by(Collection.id.desc())))
        if not rows:
            typer.echo("No collections.")
            return
        for c in rows:
            n = s.scalar(
                select(func.count(CollectionItem.id)).where(
                    CollectionItem.collection_id == c.id
                )
            ) or 0
            typer.echo(f"#{c.id:<4} {c.type:<16} items={n:<5} {c.title or c.url}")


@collections_app.command("show")
def collections_show(collection_id: int = typer.Argument(...)) -> None:
    """Show a collection's metadata and item counts."""
    from sqlalchemy import func

    from app.models import Collection, CollectionItem

    with session_scope() as s:
        c = s.get(Collection, collection_id)
        if c is None:
            raise typer.BadParameter("collection not found")
        total = s.scalar(
            select(func.count(CollectionItem.id)).where(
                CollectionItem.collection_id == c.id
            )
        ) or 0
        active = s.scalar(
            select(func.count(CollectionItem.id)).where(
                CollectionItem.collection_id == c.id,
                CollectionItem.removed_at.is_(None),
            )
        ) or 0
        typer.echo(f"Collection #{c.id}")
        typer.echo(f"  type        : {c.type}")
        typer.echo(f"  title       : {c.title}")
        typer.echo(f"  url         : {c.url}")
        typer.echo(f"  playlist_id : {c.youtube_playlist_id}")
        typer.echo(f"  channel_id  : {c.youtube_channel_id}")
        typer.echo(f"  profile_id  : {c.download_profile_id}")
        typer.echo(f"  items       : {total} (active {active}, removed {total - active})")


@collections_app.command("items")
def collections_items(
    collection_id: int = typer.Argument(...),
    include_removed: bool = typer.Option(False, "--include-removed"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List a collection's video items."""
    from app.models import Collection, CollectionItem

    with session_scope() as s:
        if s.get(Collection, collection_id) is None:
            raise typer.BadParameter("collection not found")
        stmt = select(CollectionItem).where(
            CollectionItem.collection_id == collection_id
        )
        if not include_removed:
            stmt = stmt.where(CollectionItem.removed_at.is_(None))
        stmt = stmt.order_by(CollectionItem.position, CollectionItem.id).limit(limit)
        items = list(s.scalars(stmt))
        if not items:
            typer.echo("No items.")
            return
        for it in items:
            flag = "  (removed)" if it.removed_at else ""
            typer.echo(f"  [{it.position}] {it.youtube_video_id}{flag}")


@collections_app.command("refresh")
def collections_refresh(
    collection_id: int = typer.Argument(...),
    now: bool = typer.Option(False, "--now"),
    max_items: int = typer.Option(0, "--max-items", help="Cap items (0 = EXPAND_MAX_ITEMS)."),
) -> None:
    """Re-crawl one collection (refresh semantics: detects removed items)."""
    from app.models import Collection, DownloadProfile

    with session_scope() as s:
        c = s.get(Collection, collection_id)
        if c is None:
            raise typer.BadParameter("collection not found")
        profile = get_settings().default_profile
        if c.download_profile_id:
            prow = s.get(DownloadProfile, c.download_profile_id)
            if prow is not None:
                profile = prow.name
        job = jobs_svc.create_job_for_url(
            s,
            c.url,
            profile,
            max_items=(max_items or None),
            extra_meta={
                "scheduled_by": "manual_refresh",
                "crawl_policy": c.crawl_policy or "new_only",
                "detect_removed": True,
                "collection_id": c.id,
            },
        )
        job_id = job.id
    typer.echo(f"Created refresh (expand) job #{job_id} for collection #{collection_id}")
    _dispatch(job_id, now)


@collections_app.command("refresh-all")
def collections_refresh_all(
    now: bool = typer.Option(False, "--now"),
    max_items: int = typer.Option(0, "--max-items"),
) -> None:
    """Re-crawl all enabled, non-manual collections (honours each crawl_policy)."""
    from app.services.scheduler import run_once

    summary = run_once(
        get_settings(),
        reason="manual_refresh_all",
        max_items=(max_items or None),
        do_comments=False,  # collections-only command
    )
    typer.echo(
        f"Refreshed: collections_checked={summary['collections_checked']} "
        f"jobs_created={summary['jobs_created']}"
    )
    if now:
        for jid in summary["job_ids"]:
            _dispatch(jid, now=True)


@collections_app.command("enable")
def collections_enable(collection_id: int = typer.Argument(...)) -> None:
    """Enable a collection for scheduled re-crawl."""
    _set_collection_enabled(collection_id, True)


@collections_app.command("disable")
def collections_disable(collection_id: int = typer.Argument(...)) -> None:
    """Disable a collection (scheduler will skip it)."""
    _set_collection_enabled(collection_id, False)


@collections_app.command("set-policy")
def collections_set_policy(
    collection_id: int = typer.Argument(...),
    policy: str = typer.Argument(..., help="manual | new_only | refresh"),
) -> None:
    """Set a collection's crawl policy."""
    from app.models import Collection

    if policy not in ("manual", "new_only", "refresh"):
        raise typer.BadParameter("policy must be one of: manual, new_only, refresh")
    with session_scope() as s:
        c = s.get(Collection, collection_id)
        if c is None:
            raise typer.BadParameter("collection not found")
        c.crawl_policy = policy
    typer.echo(f"Collection #{collection_id} crawl_policy set to {policy}")


# --------------------------------------------------------------------------- #
# scheduler
# --------------------------------------------------------------------------- #
@scheduler_app.command("run-once")
def scheduler_run_once(
    max_items: int = typer.Option(0, "--max-items"),
    collections: bool = typer.Option(False, "--collections", help="Run collection re-crawl."),
    comments: bool = typer.Option(False, "--comments", help="Run due comment refreshes."),
    liked_metadata: bool = typer.Option(False, "--liked-metadata", help="Enqueue metadata_only for liked videos (no body)."),
    liked_archive: bool = typer.Option(False, "--liked-archive", help="Enqueue a small liked BODY archive (downloads bodies!)."),
    liked_retry: bool = typer.Option(False, "--liked-retry", help="Re-queue retryable liked jobs (backoff respected)."),
    all_: bool = typer.Option(False, "--all", help="Run collections + comments + all liked passes."),
) -> None:
    """Run a single scheduler pass now (works even if SCHEDULER_ENABLED=false).

    Choose with ``--collections`` / ``--comments`` / ``--liked-metadata`` /
    ``--liked-archive`` / ``--liked-retry`` / ``--all``. With no flag, only
    collections + comments run (liked passes are opt-in for safety).
    """
    from app.services.scheduler import run_once

    liked_any = liked_metadata or liked_archive or liked_retry
    if all_:
        do_collections = do_comments = True
        liked_metadata = liked_archive = liked_retry = True
    elif liked_any:
        # liked pass(es) requested -> run only those (avoid surprise crawl work)
        do_collections = collections
        do_comments = comments
    elif not collections and not comments:
        do_collections = do_comments = True
    else:
        do_collections, do_comments = collections, comments

    summary = run_once(
        get_settings(),
        reason="manual",
        max_items=(max_items or None),
        do_collections=do_collections,
        do_comments=do_comments,
        do_liked_metadata=liked_metadata,
        do_liked_archive=liked_archive,
        do_liked_retry=liked_retry,
    )
    typer.echo(
        "scheduler run-once: "
        f"collections_checked={summary['collections_checked']} "
        f"collection_jobs={summary['collection_jobs_created']} "
        f"due_comment_videos={summary['due_comment_videos_checked']} "
        f"comment_jobs={summary['comments_jobs_created']} "
        f"liked_metadata={summary['liked_metadata_jobs_created']}/{summary['liked_metadata_selected']} "
        f"liked_archive={summary['liked_archive_jobs_created']}/{summary['liked_archive_selected']} "
        f"liked_retry={summary['liked_retry_jobs_requeued']}/{summary['liked_retry_selected']} "
        f"skipped_active={summary['skipped_active_jobs']} "
        f"skipped_dup={summary['skipped_duplicates']} "
        f"submitted={summary['submitted']}"
    )


@scheduler_app.command("run")
def scheduler_run() -> None:
    """Run the scheduler loop forever (used by the `scheduler` container)."""
    from app.services.scheduler import run_forever

    run_forever(get_settings())


@scheduler_runs_app.callback(invoke_without_command=True)
def scheduler_runs_list(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit"),
    run_type: str = typer.Option("", "--type", help="filter by run_type"),
) -> None:
    """List recent scheduler runs (Phase 7E)."""
    if ctx.invoked_subcommand is not None:
        return
    from app.services import scheduler as sch

    with session_scope() as s:
        runs = sch.list_runs(s, run_type=(run_type or None), limit=limit)
        if not runs:
            typer.echo("No scheduler runs recorded yet.")
            return
        for r in runs:
            typer.echo(
                f"  {r.run_id}  {r.run_type:<16} {r.status:<16} "
                f"created={r.jobs_created} skipped(active/dup/backoff)="
                f"{r.skipped_active_jobs}/{r.skipped_duplicates}/{r.skipped_backoff} "
                f"body {r.body_count_before}->{r.body_count_after}  {r.started_at}"
            )


@scheduler_runs_app.command("show")
def scheduler_runs_show(run_id: str = typer.Argument(...)) -> None:
    """Show one scheduler run + its jobs."""
    from app.services import scheduler as sch

    with session_scope() as s:
        r = sch.get_run(s, run_id)
        if r is None:
            typer.echo(f"run {run_id} not found")
            raise typer.Exit(code=1)
        typer.echo(f"== scheduler run {r.run_id} ==")
        typer.echo(f"  type={r.run_type} status={r.status} reason={r.reason}")
        typer.echo(f"  started={r.started_at} finished={r.finished_at}")
        typer.echo(f"  selected={r.selected_count} created={r.jobs_created} submitted={r.jobs_submitted}")
        typer.echo(f"  skipped active/dup/backoff: {r.skipped_active_jobs}/{r.skipped_duplicates}/{r.skipped_backoff}")
        typer.echo(f"  body {r.body_count_before} -> {r.body_count_after}")
        typer.echo(f"  retryable/failed/partial/success(body): {r.retryable_count}/{r.failed_count}/{r.partial_count}/{r.success_count}")
        jobs = sch.run_jobs(s, run_id)
        typer.echo(f"  jobs ({len(jobs)}): " + ", ".join(f"#{j.id}({j.profile_name},{j.status})" for j in jobs[:20]))


@scheduler_app.command("stats")
def scheduler_stats_cmd(lookback: int = typer.Option(50, "--lookback")) -> None:
    """Aggregate recent scheduler runs (status rates, totals)."""
    from app.services import scheduler as sch

    with session_scope() as s:
        st = sch.scheduler_stats(s, lookback=lookback)
    typer.echo("== scheduler stats ==")
    typer.echo(f"  runs considered: {st['runs_considered']}")
    typer.echo(f"  by type: {st['by_type']}")
    typer.echo(f"  by status: {st['by_status']}")
    typer.echo(f"  jobs created/submitted: {st['jobs_created']}/{st['jobs_submitted']}")
    typer.echo(f"  skipped active/dup/backoff: {st['skipped_active_jobs']}/{st['skipped_duplicates']}/{st['skipped_backoff']}")
    typer.echo(f"  last run: {st['last_run_id']} ({st['last_run_type']}, {st['last_run_status']}) at {st['last_run_at']}")


@scheduler_app.command("recommend-settings")
def scheduler_recommend_settings_cmd(
    lookback: int = typer.Option(30, "--lookback"),
    env: bool = typer.Option(False, "--env", help="Output a copy-paste .env snippet (not applied)."),
    json_: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Suggest safe archive/retry limits from recent results (does NOT apply)."""
    from app.services import scheduler as sch

    with session_scope() as s:
        rec = sch.recommend_settings(s, get_settings(), lookback=lookback)
    if env or json_:
        # export-only output (no extra chatter so it's clean to copy/redirect)
        typer.echo(sch.recommend_export(rec, "json" if json_ else "env"))
        return
    typer.echo("== recommended settings (suggestion only — not applied) ==")
    typer.echo(f"  based on: {rec['based_on']}")
    typer.echo(f"  rates: success={rec['rates'].get('success_rate')} throttle={rec['rates'].get('throttle_rate')}")
    cur, rc = rec["current"], rec["recommended"]
    for k in rc:
        flag = "  <= CHANGE" if rc[k] != cur.get(k) else ""
        typer.echo(f"  {k}: {cur.get(k)} -> {rc[k]}{flag}")
    typer.echo("  reasons:")
    for r in rec["reasons"]:
        typer.echo(f"   - {r}")
    typer.echo(f"  {rec['note']}")


@scheduler_runs_app.command("cleanup")
def scheduler_runs_cleanup_cmd(
    keep_last: int = typer.Option(0, "--keep-last", help="Keep the N most-recent runs."),
    older_than_days: int = typer.Option(0, "--older-than-days", help="Only delete runs older than D days."),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview (default) vs actually delete."),
) -> None:
    """Prune old scheduler_runs. NEVER deletes jobs (job.meta.scheduler_run_id stays)."""
    from app.services import scheduler as sch

    if not keep_last and not older_than_days:
        typer.echo("Nothing to do: pass --keep-last N and/or --older-than-days D (safety).")
        return
    with session_scope() as s:
        res = sch.cleanup_runs(s, keep_last=keep_last, older_than_days=older_than_days, dry_run=dry_run)
        if not dry_run:
            s.commit()
    verb = "would delete" if dry_run else "deleted"
    typer.echo(
        f"scheduler runs cleanup ({'dry-run' if dry_run else 'applied'}): "
        f"total={res['total_runs']} {verb}={res['matched'] if dry_run else res['deleted']} kept={res['kept']} "
        f"(keep_last={keep_last}, older_than_days={older_than_days}). Jobs are NOT deleted."
    )


# --------------------------------------------------------------------------- #
# takeout
# --------------------------------------------------------------------------- #
@takeout_app.command("list-files")
def takeout_list_files(path: str = typer.Argument(..., help="ZIP under TAKEOUT_IMPORT_ROOT.")) -> None:
    """List the YouTube-related files detected in a Takeout ZIP."""
    from app.services import takeout as tk

    try:
        zip_path = tk.resolve_takeout_path(get_settings(), path)
        with tk.open_archive(zip_path) as a:
            for f in a.list_files():
                typer.echo(f"  {f.kind:<16} {f.format:<5} {f.size:>10}  {f.name}")
    except tk.TakeoutError as exc:
        raise typer.BadParameter(str(exc))


@takeout_app.command("discover")
def takeout_discover(
    deep: bool = typer.Option(False, "--deep", help="Also parse a liked-count hint (slower)."),
) -> None:
    """Classify every ZIP under TAKEOUT_IMPORT_ROOT (youtube / my_activity / index / unknown)."""
    from app.services import takeout as tk

    rows = tk.discover(get_settings(), deep=deep)
    if not rows:
        typer.echo("No ZIPs under TAKEOUT_IMPORT_ROOT.")
        return
    for d in rows:
        liked = f" liked={d['liked_count']}" if d.get("liked_count") is not None else ""
        ma = " [my-activity-yt]" if d.get("my_activity_youtube_path") else ""
        typer.echo(f"  {d.get('archive_kind','?'):<20}{liked}{ma}  {d['name']}")


@takeout_app.command("inspect")
def takeout_inspect(
    path: str = typer.Argument(..., help="ZIP under TAKEOUT_IMPORT_ROOT."),
    deep: bool = typer.Option(False, "--deep", help="Also list the structured source registry."),
) -> None:
    """Show the structural classification of one Takeout ZIP."""
    from app.services import takeout as tk

    try:
        zip_path = tk.resolve_takeout_path(get_settings(), path)
        with tk.open_archive(zip_path) as a:
            info = a.inspect()
            registry = a.registry() if deep else []
    except tk.TakeoutError as exc:
        raise typer.BadParameter(str(exc))
    typer.echo(f"archive_kind        : {info['archive_kind']}")
    typer.echo(f"has_youtube_takeout : {info['has_youtube_takeout']}")
    typer.echo(f"my_activity_yt_path : {info['my_activity_youtube_path']}")
    typer.echo(f"has_index           : {info['has_index']}")
    typer.echo(f"member_count        : {info['member_count']}")
    typer.echo(f"liked_source_kind   : {info['liked_source_kind']}")
    typer.echo(f"liked_detected_path : {info['liked_detected_path']}")
    if deep:
        typer.echo("registry (detected sources):")
        for r in registry:
            typer.echo(f"  - {r['kind']:<30} [{r['format']}] {r['member']}")
        if not registry:
            typer.echo("  (none)")


@takeout_app.command("preview")
def takeout_preview(path: str = typer.Argument(...)) -> None:
    """Preview a Takeout ZIP (counts + samples) WITHOUT importing."""
    from app.services import takeout as tk

    try:
        zip_path = tk.resolve_takeout_path(get_settings(), path)
        with tk.open_archive(zip_path) as a:
            pv = a.preview(sample=5)
    except tk.TakeoutError as exc:
        raise typer.BadParameter(str(exc))
    typer.echo(f"archive_kind={pv.get('archive_kind')} liked_source={pv.get('liked_source_kind')}")
    typer.echo(
        f"watch_history={pv['watch_history_count']} search_history={pv['search_history_count']} "
        f"likes={pv['likes_count']} subscriptions={pv['subscriptions_count']} "
        f"playlists={pv['playlists_count']}"
    )
    if pv.get("liked_samples"):
        typer.echo("liked samples:")
        for s in pv["liked_samples"]:
            typer.echo(f"  {s['youtube_video_id'] or '-'}  {(s['title'] or '')[:50]}  | {s['liked_at']}")
    typer.echo("samples:")
    for s in pv["samples"]:
        typer.echo(f"  {s['youtube_video_id'] or '-'}  {(s['title'] or '')[:50]}  | {s['channel_title']}  | {s['watched_at']}")
    if pv["warnings"]:
        typer.echo(f"warnings: {pv['warnings'][:5]}")


@takeout_app.command("import")
def takeout_import(
    path: str = typer.Argument(...),
    limit: int = typer.Option(0, "--limit", help="Max events to scan (0 = all)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do not write to the DB."),
) -> None:
    """Import watch history from a Takeout ZIP into watch_history_events."""
    from app.services import takeout as tk

    with session_scope() as s:
        try:
            result = tk.run_import(
                s, get_settings(), path, limit=(limit or None), dry_run=dry_run
            )
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
    typer.echo(
        f"imported={result['imported_count']} skipped_duplicate={result['skipped_duplicate_count']} "
        f"failed={result['failed_count']} scanned={result['scanned']} "
        f"dry_run={result['dry_run']} job_id={result['job_id']}"
    )
    if result["warnings"]:
        typer.echo(f"warnings: {result['warnings'][:5]}")


def _echo_import_result(result: dict, kind: str) -> None:
    typer.echo(
        f"[{kind}] imported={result.get('imported_count')} "
        f"skipped_duplicate={result.get('skipped_duplicate_count')} "
        f"updated={result.get('updated_count', 0)} failed={result.get('failed_count')} "
        f"scanned={result.get('scanned')} dry_run={result.get('dry_run')} "
        f"duration={result.get('duration_seconds')}s session={result.get('session_id')}"
    )


def _maybe_import_job(import_kind: str, path: str, limit: int, dry_run: bool, job: bool, now: bool,
                      store_raw_json: bool = True) -> bool:
    """If --job, create a background takeout_import job. Returns True if handled."""
    if not job:
        return False
    from app.services import takeout as tk

    with session_scope() as s:
        j, row = tk.create_import_job(s, get_settings(), import_kind=import_kind,
                                      path=path, limit=(limit or None), dry_run=dry_run,
                                      store_raw_json=store_raw_json)
        s.commit()
        jid, sid = j.id, row.session_id
    typer.echo(f"Created takeout_import job #{jid} (session {sid}, kind={import_kind}, "
               f"dry_run={dry_run}, store_raw_json={store_raw_json}).")
    _dispatch(jid, now)
    return True


def _safe_large_defaults(safe_large: bool, limit: int, apply: bool, job: bool, dry_run: bool,
                         no_raw_json: bool) -> tuple[bool, bool, bool, bool]:
    """Resolve --safe-large preset: job=True, dry_run unless --apply, no_raw_json=True.

    Returns (job, dry_run, store_raw_json, benchmark_only). benchmark_only is True
    when --safe-large is set without an explicit --limit (run benchmark first).
    """
    if not safe_large:
        return job, dry_run, (not no_raw_json), False
    benchmark_only = not limit
    return True, (not apply), False, benchmark_only


def _safe_large_or_job(import_kind: str, path: str, limit: int, dry_run: bool, job: bool, now: bool,
                       no_raw_json: bool, safe_large: bool, apply: bool) -> bool:
    """Handle --safe-large / --job. Returns True if the import was handled here."""
    job2, dry2, store_raw, bench_only = _safe_large_defaults(safe_large, limit, apply, job, dry_run, no_raw_json)
    if safe_large and bench_only:
        from app.services import takeout as tk
        with session_scope() as s:
            b = tk.benchmark(s, get_settings(), path, kind=import_kind, limit=None, dry_run=True)
        typer.echo(
            f"[safe-large benchmark] {import_kind}: scanned={b['scanned']} eps={b['entries_per_second']} "
            f"peak_mem={b['peak_memory_mb']}MB parser={b['parser_backend']}. "
            f"Re-run with --safe-large --limit N (then --apply) to import as a no-raw-json job."
        )
        return True
    return _maybe_import_job(import_kind, path, limit, dry2, job2, now, store_raw_json=store_raw)


@takeout_app.command("import-watch-history")
def takeout_import_watch_history(
    path: str = typer.Argument(...),
    limit: int = typer.Option(0, "--limit", help="Max events (0 = all)."),
    incremental: bool = typer.Option(True, "--incremental/--full", help="Dedup vs existing (always on)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    job: bool = typer.Option(False, "--job", help="Run as a background job (large imports)."),
    now: bool = typer.Option(False, "--now", help="With --job: run inline instead of via RQ."),
    no_raw_json: bool = typer.Option(False, "--no-raw-json", help="Do not persist raw activity blobs (DB size)."),
    safe_large: bool = typer.Option(False, "--safe-large", help="Preset: job + no-raw-json + dry-run unless --apply."),
    apply: bool = typer.Option(False, "--apply", help="With --safe-large: actually write (not dry-run)."),
) -> None:
    """Import watch history (incremental: dedup vs existing). Streams large JSON."""
    from app.services import takeout as tk

    if _safe_large_or_job("watch_history", path, limit, dry_run, job, now, no_raw_json, safe_large, apply):
        return
    with session_scope() as s:
        try:
            result = tk.run_import(s, get_settings(), path, limit=(limit or None), dry_run=dry_run, store_raw_json=not no_raw_json)
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
    _echo_import_result(result, "watch_history")


@takeout_app.command("import-search-history")
def takeout_import_search_history(
    path: str = typer.Argument(...),
    limit: int = typer.Option(0, "--limit", help="Max events (0 = all)."),
    incremental: bool = typer.Option(True, "--incremental/--full", help="Dedup vs existing (always on)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    job: bool = typer.Option(False, "--job", help="Run as a background job (large imports)."),
    now: bool = typer.Option(False, "--now"),
    no_raw_json: bool = typer.Option(False, "--no-raw-json", help="Do not persist raw activity blobs (DB size)."),
    safe_large: bool = typer.Option(False, "--safe-large"),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Import search history (incremental: dedup vs existing). Streams large JSON."""
    from app.services import takeout as tk

    if _safe_large_or_job("search_history", path, limit, dry_run, job, now, no_raw_json, safe_large, apply):
        return
    with session_scope() as s:
        try:
            result = tk.run_import_search(s, get_settings(), path, limit=(limit or None), dry_run=dry_run, store_raw_json=not no_raw_json)
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
    _echo_import_result(result, "search_history")


@takeout_sessions_app.callback(invoke_without_command=True)
def takeout_sessions_list(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit"),
    import_kind: str = typer.Option("", "--kind", help="filter by import_kind"),
) -> None:
    """List recent Takeout import sessions (counts only; no PII)."""
    if ctx.invoked_subcommand is not None:
        return
    from app.services import takeout as tk

    with session_scope() as s:
        rows = tk.list_import_sessions(s, import_kind=(import_kind or None), limit=limit)
        if not rows:
            typer.echo("No import sessions recorded yet.")
            return
        for r in rows:
            typer.echo(
                f"  {r.session_id}  {r.import_kind:<14} {r.status:<8} "
                f"imported={r.imported} skipped={r.skipped_duplicate} updated={r.updated} "
                f"failed={r.failed} scanned={r.scanned} dry_run={r.dry_run}  {r.path_basename}  {r.started_at}"
            )


@takeout_sessions_app.command("progress")
def takeout_sessions_progress(session_id: str = typer.Argument(...)) -> None:
    """Show live progress for a (running) import session."""
    from app.services import takeout as tk

    with session_scope() as s:
        r = tk.get_import_session(s, session_id)
        if r is None:
            typer.echo(f"session {session_id} not found")
            raise typer.Exit(code=1)
        typer.echo(
            f"{r.session_id}  {r.import_kind}  status={r.status} phase={r.current_phase} "
            f"scanned={r.scanned} imported={r.imported} skipped={r.skipped_duplicate} "
            f"updated={r.updated} failed={r.failed} eps={r.entries_per_second} "
            f"cancel_requested={r.cancel_requested} job_id={r.job_id} last_update={r.last_update_at}"
        )


@takeout_sessions_app.command("cancel")
def takeout_sessions_cancel(session_id: str = typer.Argument(...)) -> None:
    """Request cancellation of a running import (stops at the next checkpoint)."""
    from app.services import takeout as tk

    with session_scope() as s:
        ok = tk.request_cancel(s, session_id)
        s.commit()
    typer.echo("cancel requested" if ok else "session not found or not running")


@takeout_app.command("benchmark")
def takeout_benchmark(
    path: str = typer.Argument(...),
    kind: str = typer.Option("liked_videos", "--kind", help="liked_videos | watch_history | search_history | all"),
    limit: int = typer.Option(0, "--limit", help="0 = all (caution on huge files)."),
    dry_run: bool = typer.Option(True, "--dry-run/--write", help="Measure without writing (default)."),
) -> None:
    """Benchmark parse/import throughput + peak memory for a Takeout source."""
    from app.services import takeout as tk

    with session_scope() as s:
        try:
            b = tk.benchmark(s, get_settings(), path, kind=kind, limit=(limit or None), dry_run=dry_run)
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
        if not dry_run:
            s.commit()
    typer.echo(
        f"[benchmark {b['kind']}] scanned={b['scanned']} imported={b['imported']} "
        f"skipped={b['skipped_duplicate']} updated={b['updated']} failed={b['failed']} "
        f"duration={b['duration_seconds']}s eps={b['entries_per_second']} "
        f"peak_mem={b['peak_memory_mb']}MB parser={b['parser_backend']} "
        f"dry_run={b['dry_run']} source_kind={b['source_kind']}"
    )


@takeout_sessions_app.command("cleanup")
def takeout_sessions_cleanup(
    keep_last: int = typer.Option(0, "--keep-last"),
    older_than_days: int = typer.Option(0, "--older-than-days"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview (default) vs delete."),
) -> None:
    """Prune old import sessions. Deletes ONLY session rows — never jobs / imported data."""
    from app.services import takeout as tk

    if not keep_last and not older_than_days:
        typer.echo("Nothing to do: pass --keep-last N and/or --older-than-days D (safety).")
        return
    with session_scope() as s:
        res = tk.cleanup_import_sessions(s, keep_last=keep_last, older_than_days=older_than_days, dry_run=dry_run)
        if not dry_run:
            s.commit()
    verb = "would delete" if dry_run else "deleted"
    typer.echo(
        f"sessions cleanup ({'dry-run' if dry_run else 'applied'}): total={res['total']} "
        f"{verb}={res['matched'] if dry_run else res['deleted']} kept={res['kept']} "
        f"jobs_preserved={res['jobs_preserved']}. Jobs and imported data are NOT deleted."
    )


@takeout_sessions_app.command("cleanup-auto")
def takeout_sessions_cleanup_auto(
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview (default) vs run now."),
) -> None:
    """Run the configured auto session-cleanup now (uses retention/keep_last config).

    Deletes ONLY session rows — never jobs / imported data. ``--apply`` forces a
    run regardless of the enabled flag and interval."""
    from app.services import takeout as tk

    s_settings = get_settings()
    keep_last = s_settings.takeout_import_session_keep_last
    retention = s_settings.takeout_import_session_retention_days
    if dry_run:
        with session_scope() as s:
            res = tk.cleanup_import_sessions(
                s, keep_last=keep_last, older_than_days=retention, dry_run=True
            )
        typer.echo(
            f"cleanup-auto (dry-run): enabled={s_settings.takeout_import_session_cleanup_enabled} "
            f"keep_last={keep_last} retention_days={retention} -> would delete={res['matched']} "
            f"of {res['total']} (jobs_preserved={res['jobs_preserved']}). Jobs/data NOT deleted."
        )
        return
    with session_scope() as s:
        res = tk.auto_cleanup_import_sessions(s, s_settings, force=True)
        s.commit()
    if res.get("ran"):
        r = res["result"]
        typer.echo(
            f"cleanup-auto (applied): deleted={r['deleted']} kept={r['kept']} "
            f"jobs_preserved={r['jobs_preserved']}. Jobs and imported data are NOT deleted."
        )
    else:
        typer.echo(f"cleanup-auto: did not run ({res.get('reason')}).")


@takeout_sessions_app.command("cleanup-status")
def takeout_sessions_cleanup_status() -> None:
    """Show auto session-cleanup config + last run result."""
    from app.services import takeout as tk

    st = tk.cleanup_status(get_settings())
    typer.echo("== takeout session cleanup status ==")
    typer.echo(
        f"  enabled={st['enabled']} interval_hours={st['interval_hours']} "
        f"keep_last={st['keep_last']} retention_days={st['retention_days']}"
    )
    typer.echo(f"  last_run_at={st['last_run_at'] or '-'}  next_due_at={st['next_due_at'] or '-'}")
    if st["last_result"]:
        r = st["last_result"]
        typer.echo(
            f"  last_result: deleted={r.get('deleted')} kept={r.get('kept')} "
            f"jobs_preserved={r.get('jobs_preserved')}"
        )


@takeout_app.command("benchmark-large")
def takeout_benchmark_large(
    path: str = typer.Argument(...),
    include_search: bool = typer.Option(False, "--include-search"),
) -> None:
    """Full-scan dry-run benchmark for liked + watch (+ optional search)."""
    from app.services import takeout as tk

    with session_scope() as s:
        try:
            bl = tk.benchmark_large(s, get_settings(), path, include_search=include_search)
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
    typer.echo("== benchmark-large (dry-run, full scan) ==")
    for kind, b in bl["results"].items():
        typer.echo(
            f"  {kind:<14} scanned={b['scanned']} eps={b['entries_per_second']} "
            f"peak_mem={b['peak_memory_mb']}MB est_full_import={b['estimated_full_import_time_seconds']}s "
            f"batch={b['recommended_batch_size']} parser={b['parser_backend']}"
        )
    typer.echo(f"  recommended_batch_size: {bl['recommended_batch_size']}")


@takeout_app.command("preflight-large")
def takeout_preflight_large(
    path: str = typer.Argument(...),
    kind: str = typer.Option("all", "--kind", help="liked_videos|watch_history|search_history|all"),
    sample_limit: int = typer.Option(5000, "--sample-limit"),
) -> None:
    """Go/no-go preflight for a large import (ZIP/parser/sample bench/DB counts)."""
    from app.services import takeout as tk

    with session_scope() as s:
        pl = tk.preflight_large(s, get_settings(), path, kind=kind, sample_limit=sample_limit)
    icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    typer.echo(f"== preflight-large ==  zip={pl['path_basename']} parser={pl['parser_backend']}")
    for c in pl["checks"]:
        typer.echo(f"  [{icon.get(c['status'], '?')}] {c['name']}: {c['detail']}")
    for k, r in pl["results"].items():
        typer.echo(
            f"  {k:<14} sample_scanned={r['sample_scanned']} eps={r['entries_per_second']} "
            f"peak={r['peak_memory_mb']}MB current_db_rows={r['current_db_count']}"
        )
    if pl["recommended_command"]:
        typer.echo(f"  next: {pl['recommended_command']}")
    typer.echo(f"  => {'PASS' if pl['ok'] else 'FAIL'}")
    if not pl["ok"]:
        raise typer.Exit(code=1)


@takeout_app.command("import-large")
def takeout_import_large(
    path: str = typer.Argument(...),
    kind: str = typer.Option("all", "--kind", help="liked_videos|watch_history|search_history|all"),
    limit: int = typer.Option(0, "--limit", help="Max entries per kind (0 = all)."),
    apply: bool = typer.Option(False, "--apply", help="Actually import (default dry-run)."),
    raw_json: bool = typer.Option(False, "--raw-json", help="Persist raw blobs (default OFF / no-raw-json)."),
    job: bool = typer.Option(True, "--job/--no-job", help="Run as background job (default on)."),
    skip_preflight: bool = typer.Option(False, "--skip-preflight", help="NOT recommended: bypass preflight."),
) -> None:
    """Safe production import runner. Defaults: dry-run + no-raw-json + job, with
    an automatic preflight (an --apply job is blocked if a stale worker is found)."""
    from app.services import takeout as tk

    with session_scope() as s:
        try:
            res = tk.import_large(
                s, get_settings(), path, kind=kind, limit=(limit or None), apply=apply,
                store_raw_json=raw_json, as_job=job, skip_preflight=skip_preflight,
            )
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
    typer.echo(
        f"== import-large == kind={res['kind']} dry_run={res['dry_run']} "
        f"store_raw_json={res['store_raw_json']} as_job={res['as_job']} preflight_ok={res['preflight_ok']}"
    )
    if not res["ok"]:
        typer.echo(f"  BLOCKED: {res['message']}")
        raise typer.Exit(code=1)
    for it in res["items"]:
        typer.echo(
            f"  {it['kind']:<14} session={it.get('session_id')} job={it.get('job_id')} "
            f"dry_run={it['dry_run']} rq_submitted={it.get('rq_submitted')}"
            + (f" would_import={it.get('would_import')}" if it["dry_run"] else "")
        )
    typer.echo(f"  {res['message']}")
    if res["recommended_progress_command"]:
        typer.echo(f"  progress: {res['recommended_progress_command']}")
        typer.echo(f"  db-stats: {res['recommended_db_stats_command']}")


@takeout_app.command("verify-import")
def takeout_verify_import(
    session_id: str = typer.Argument(None),
    latest: bool = typer.Option(False, "--latest", help="Verify the most recent session."),
    kind: str = typer.Option(None, "--kind", help="With --latest: filter by import kind."),
) -> None:
    """Post-import inspection: outcome + DB stats + raw_json blobs + leak grep."""
    from app.services import takeout as tk

    if not session_id and not latest:
        raise typer.BadParameter("pass a SESSION_ID or --latest")
    with session_scope() as s:
        v = tk.verify_import(s, get_settings(), session_id=session_id, latest=latest, kind=kind)
    if not v.get("session_id"):
        typer.echo("import session not found")
        raise typer.Exit(code=1)
    icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    typer.echo(f"== verify-import == session={v['session_id']} kind={v['import_kind']} status={v['status']}")
    typer.echo(
        f"  scanned={v['scanned']} imported={v['imported']} skipped={v['skipped_duplicate']} "
        f"updated={v['updated']} failed={v['failed']} eps={v['entries_per_second']} peak={v['peak_memory_mb']}MB"
    )
    typer.echo(
        f"  store_raw_json={v['store_raw_json']} raw_stored={v['raw_json_stored_count']} "
        f"raw_skipped={v['raw_json_skipped_count']} job=#{v['job_id']} job_status={v['job_status']}"
    )
    ds = v["db_stats"]
    typer.echo(
        f"  db: {ds.get('total_size_mb')}MB videos={ds.get('videos')} liked={ds.get('liked_videos')} "
        f"watch={ds.get('watch_history_events')} raw_json_real_blobs_total={ds.get('raw_json_stored_total')}"
    )
    for c in v["checks"]:
        typer.echo(f"  [{icon.get(c['status'], '?')}] {c['name']}: {c['detail']}")
    if v["worker_error"]:
        typer.echo(f"  worker_error: {v['worker_error']}")
    typer.echo(f"  => {'OK' if v['ok'] else 'ATTENTION'}")
    if not v["ok"]:
        raise typer.Exit(code=1)


@takeout_app.command("import-staged")
def takeout_import_staged(
    path: str = typer.Argument(...),
    kind: str = typer.Option("all", "--kind", help="liked_videos|watch_history|search_history|all"),
    apply: bool = typer.Option(False, "--apply", help="Execute stages (default = dry-run plan)."),
    raw_json: bool = typer.Option(False, "--raw-json", help="Persist raw blobs (default OFF / no-raw-json)."),
    job: bool = typer.Option(True, "--job/--no-job", help="Run each stage as a background job (default on)."),
    allow_full: bool = typer.Option(False, "--allow-full", help="Permit the FINAL full-import stage."),
    max_stage: int = typer.Option(0, "--max-stage", help="Stop after N stages (0 = all)."),
    skip_preflight: bool = typer.Option(False, "--skip-preflight", help="NOT recommended: bypass preflight."),
) -> None:
    """Staged production import: 100→1000→5000→full (liked) / 1000→10000→50000→full
    (watch), with verify + db-stats between stages. Safe defaults: dry-run +
    no-raw-json + job + preflight. Full stage requires --allow-full."""
    from app.services import takeout as tk

    res = tk.import_staged(
        get_settings(), path, kind=kind, apply=apply, store_raw_json=raw_json, as_job=job,
        skip_preflight=skip_preflight, allow_full=allow_full,
        max_stage=(max_stage or None),
    )
    typer.echo(
        f"== import-staged == kind={res['kind']} dry_run={res['dry_run']} "
        f"store_raw_json={res['store_raw_json']} as_job={res['as_job']} preflight_ok={res['preflight_ok']}"
    )
    for k, plan in res.get("plan", {}).items():
        typer.echo(f"  plan[{k}]: {plan}")
    if res.get("prior_sessions"):
        typer.echo(f"  prior sessions for this file/kind: {len(res['prior_sessions'])} "
                   f"(rerun is dedup-safe — duplicates are skipped, nothing is deleted)")
    if not res["ok"]:
        typer.echo(f"  BLOCKED/STOPPED: {res['message']}")
        raise typer.Exit(code=1)
    for st in res["stages"]:
        if st.get("stage") == "benchmark":
            typer.echo(f"  [bench {st['kind']:<14}] scanned={st['scanned']} would_import={st['would_import']} "
                       f"eps={st['eps']} peak={st['peak_memory_mb']}MB")
        elif st.get("status") == "skipped_needs_allow_full":
            typer.echo(f"  [stage {st['stage']} {st['kind']:<14}] limit=full SKIPPED (pass --allow-full)")
        else:
            typer.echo(
                f"  [stage {st['stage']} {st['kind']:<14}] limit={st['limit']} imported={st.get('imported')} "
                f"skipped={st.get('skipped')} raw_skipped={st.get('raw_json_skipped')} "
                f"db {st.get('db_size_mb_before')}->{st.get('db_size_mb_after')}MB "
                f"verify_ok={st.get('verify_ok')} status={st['status']}"
            )
    typer.echo(f"  {res['message']}")
    if res.get("recommended_next"):
        typer.echo(f"  next: {res['recommended_next']}")


@takeout_app.command("import-report")
def takeout_import_report(
    session_id: str = typer.Argument(None),
    latest: bool = typer.Option(False, "--latest", help="Report on the most recent session."),
    kind: str = typer.Option(None, "--kind", help="Filter by import kind."),
    recent: int = typer.Option(0, "--recent", help="List the N most recent reports."),
) -> None:
    """Operation report: import result + job + verify + db-stats + leak check +
    recommended next action. No raw_json / history body / secrets / host paths."""
    from app.services import takeout as tk

    with session_scope() as s:
        if recent:
            rl = tk.import_report(s, get_settings(), kind=kind, recent=recent)
            typer.echo(f"== import-report (recent {rl['count']}) ==")
            for r in rl["reports"]:
                typer.echo(
                    f"  {r['session_id']} {r['import_kind']:<14} status={r['status']} "
                    f"imported={r['imported']} raw_json={r['store_raw_json']} "
                    f"leak_ok={r['leak_check_ok']} ok={r['ok']}"
                )
            return
        if not session_id and not latest:
            raise typer.BadParameter("pass a SESSION_ID, --latest, or --recent N")
        r = tk.import_report(s, get_settings(), session_id=session_id, latest=latest, kind=kind)
    if not r.get("session_id"):
        typer.echo("import session not found")
        raise typer.Exit(code=1)
    typer.echo(f"== import-report == session={r['session_id']} kind={r['import_kind']} status={r['status']}")
    typer.echo(f"  file={r.get('path_basename')} started={r.get('started_at')} finished={r.get('finished_at')}")
    typer.echo(
        f"  scanned={r['scanned']} imported={r['imported']} skipped={r['skipped_duplicate']} "
        f"updated={r['updated']} failed={r['failed']} eps={r['entries_per_second']} peak={r['peak_memory_mb']}MB"
    )
    typer.echo(
        f"  store_raw_json={r['store_raw_json']} raw_stored={r['raw_json_stored_count']} "
        f"raw_skipped={r['raw_json_skipped_count']} job=#{r['job_id']} job_status={r['job_status']}"
    )
    ds = r["db_stats"]
    typer.echo(
        f"  db: {ds.get('total_size_mb')}MB watch={ds.get('watch_history_events')} liked={ds.get('liked_videos')} "
        f"raw_json_real_blobs_total={ds.get('raw_json_stored_total')}"
    )
    typer.echo(f"  leak_check: {'clean' if r['leak_check_ok'] else 'LEAK ' + str(r['leak_findings'])}")
    typer.echo(f"  => next: {r['recommended_next_action']}")


@storage_app.command("db-stats")
def storage_db_stats() -> None:
    """Show DB row counts + approximate sizes (raw_json growth)."""
    from app.services import db_stats as dbs

    with session_scope() as s:
        st = dbs.db_stats(s)
    typer.echo("== db stats ==")
    typer.echo(f"  dialect: {st['dialect']}  total size: {st['total_size_mb']} MB")
    typer.echo(f"  videos={st['videos']} liked={st['liked_videos']} watch={st['watch_history_events']} "
               f"search={st['search_history_events']} sessions={st['takeout_import_sessions']}")
    typer.echo(f"  raw_json stored: {st['raw_json_stored']} (total {st['raw_json_stored_total']})")
    if st["table_sizes_bytes"]:
        top = sorted(st["table_sizes_bytes"].items(), key=lambda kv: kv[1], reverse=True)[:5]
        typer.echo("  largest tables: " + ", ".join(f"{n}={round(b/1024/1024,2)}MB" for n, b in top))


# --------------------------------------------------------------------------- #
# system: build identity / preflight (Phase 6F)
# --------------------------------------------------------------------------- #
@system_app.command("build-info")
def system_build_info() -> None:
    """Show this process's build identity (version / build_id / schema head)."""
    from app.services import build_info as bi

    info = bi.build_info()
    typer.echo("== build info ==")
    typer.echo(f"  app_version : {info['app_version']}")
    typer.echo(f"  build_id    : {info['build_id']}")
    typer.echo(f"  git_commit  : {info['git_commit'] or '-'}")
    typer.echo(f"  build_time  : {info['build_time'] or '-'}")
    typer.echo(f"  schema_head : {info['schema_head'] or '-'}")


@system_app.command("preflight")
def system_preflight_cmd() -> None:
    """Go/no-go checks before a large import (DB/Redis/schema/worker build match).

    Exits non-zero if any check FAILS (e.g. a stale worker), so it can gate a
    scripted import.
    """
    from app.services import preflight as pf

    with session_scope() as s:
        report = pf.system_preflight(s, get_settings())
    icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    typer.echo(f"== system preflight ==  build_id={report['build_info']['build_id']}")
    for c in report["checks"]:
        typer.echo(f"  [{icon.get(c['status'], '?')}] {c['name']}: {c['detail']}")
    if report["workers"]:
        typer.echo("  workers:")
        for w in report["workers"]:
            typer.echo(
                f"    - {w['worker_id']} build_id={w['build_id']} "
                f"age={w['age_seconds']}s stale={w['stale']} takeout_import={w['takeout_import']}"
            )
    typer.echo(f"  => {'PASS' if report['ok'] else 'FAIL'}")
    if not report["ok"]:
        raise typer.Exit(code=1)


@takeout_sessions_app.command("show")
def takeout_sessions_show(session_id: str = typer.Argument(...)) -> None:
    """Show one Takeout import session."""
    from app.services import takeout as tk

    with session_scope() as s:
        r = tk.get_import_session(s, session_id)
        if r is None:
            typer.echo(f"session {session_id} not found")
            raise typer.Exit(code=1)
        typer.echo(f"== takeout import session {r.session_id} ==")
        typer.echo(f"  kind={r.import_kind} status={r.status} dry_run={r.dry_run}")
        typer.echo(f"  file={r.path_basename} source_kind={r.source_kind}")
        typer.echo(f"  started={r.started_at} finished={r.finished_at}")
        typer.echo(f"  scanned={r.scanned} imported={r.imported} skipped={r.skipped_duplicate} updated={r.updated} failed={r.failed}")


@takeout_app.command("import-subscriptions")
def takeout_import_subscriptions(
    path: str = typer.Argument(...),
    limit: int = typer.Option(0, "--limit", help="Max channels (0 = all)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Import subscribed channels as collections (type=channel, disabled)."""
    from app.services import takeout as tk

    with session_scope() as s:
        try:
            r = tk.run_import_subscriptions(
                s, get_settings(), path, limit=(limit or None), dry_run=dry_run
            )
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
    typer.echo(
        f"imported={r['imported_count']} skipped_duplicate={r['skipped_duplicate_count']} "
        f"failed={r['failed_count']} scanned={r['scanned']} dry_run={r['dry_run']} job_id={r['job_id']}"
    )


@takeout_app.command("import-playlists")
def takeout_import_playlists(
    path: str = typer.Argument(...),
    limit_playlists: int = typer.Option(0, "--limit-playlists", help="0 = all."),
    limit_items: int = typer.Option(0, "--limit-items", help="0 = all per playlist."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Import Takeout playlists as collections (type=takeout_playlist) + items + video stubs."""
    from app.services import takeout as tk

    with session_scope() as s:
        try:
            r = tk.run_import_playlists(
                s, get_settings(), path,
                limit_playlists=(limit_playlists or None),
                limit_items=(limit_items or None),
                dry_run=dry_run,
            )
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
    typer.echo(
        f"playlists_imported={r['playlists_imported']} items_imported={r['items_imported']} "
        f"items_skipped={r['items_skipped']} videos_created={r['videos_created']} "
        f"scanned_playlists={r['scanned_playlists']} dry_run={r['dry_run']} job_id={r['job_id']}"
    )


@takeout_app.command("playlists")
def takeout_playlists(
    path: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Preview Takeout playlists (title + item count), no import."""
    from app.services import takeout as tk

    try:
        zip_path = tk.resolve_takeout_path(get_settings(), path)
        with tk.open_archive(zip_path) as a:
            for i, p in enumerate(a.iter_playlists()):
                if i >= limit:
                    break
                typer.echo(f"  {len(p.items):>5}  {p.playlist_id or '-':<36}  {p.title}")
    except tk.TakeoutError as exc:
        raise typer.BadParameter(str(exc))


@takeout_app.command("import-liked-videos")
def takeout_import_liked_videos(
    path: str = typer.Argument(...),
    limit: int = typer.Option(0, "--limit", help="Max liked videos to scan (0 = all)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    job: bool = typer.Option(False, "--job", help="Run as a background job (large imports)."),
    now: bool = typer.Option(False, "--now", help="With --job: run inline instead of via RQ."),
    no_raw_json: bool = typer.Option(False, "--no-raw-json", help="Do not persist raw activity blobs (DB size)."),
    safe_large: bool = typer.Option(False, "--safe-large", help="Preset: job + no-raw-json + dry-run unless --apply."),
    apply: bool = typer.Option(False, "--apply", help="With --safe-large: actually write (not dry-run)."),
) -> None:
    """Import liked videos (Takeout 'Liked videos' playlist) into liked_videos."""
    from app.services import takeout as tk

    if _safe_large_or_job("liked_videos", path, limit, dry_run, job, now, no_raw_json, safe_large, apply):
        return
    with session_scope() as s:
        try:
            r = tk.run_import_liked_videos(
                s, get_settings(), path, limit=(limit or None), dry_run=dry_run, store_raw_json=not no_raw_json
            )
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
    typer.echo(
        f"liked_videos: imported={r['imported_count']} skipped={r['skipped_duplicate_count']} "
        f"updated={r.get('updated_count', 0)} failed={r['failed_count']} scanned={r['scanned']} "
        f"videos_created={r['videos_created']} store_raw_json={r.get('store_raw_json', True)} dry_run={r['dry_run']}"
    )


@takeout_app.command("import-all")
def takeout_import_all(
    path: str = typer.Argument(...),
    limit_watch: int = typer.Option(0, "--limit-watch"),
    limit_search: int = typer.Option(0, "--limit-search"),
    limit_subscriptions: int = typer.Option(0, "--limit-subscriptions"),
    limit_playlists: int = typer.Option(0, "--limit-playlists"),
    limit_items: int = typer.Option(0, "--limit-items"),
    limit_liked: int = typer.Option(0, "--limit-liked"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    no_raw_json: bool = typer.Option(False, "--no-raw-json", help="Do not persist raw activity blobs (DB size)."),
) -> None:
    """Import watch/search history, subscriptions, playlists and liked videos in order."""
    from app.services import takeout as tk

    with session_scope() as s:
        try:
            r = tk.run_import_all(
                s, get_settings(), path,
                limit_watch=(limit_watch or None),
                limit_search=(limit_search or None),
                limit_subscriptions=(limit_subscriptions or None),
                limit_playlists=(limit_playlists or None),
                limit_items=(limit_items or None),
                limit_liked=(limit_liked or None),
                dry_run=dry_run,
                store_raw_json=not no_raw_json,
            )
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
    w, se, su, pl, lv = (
        r["watch_history"], r["search_history"], r["subscriptions"], r["playlists"], r["liked_videos"]
    )
    typer.echo(f"dry_run={r['dry_run']}")
    typer.echo(f"  watch_history : imported={w['imported_count']} skipped={w['skipped_duplicate_count']} scanned={w['scanned']}")
    typer.echo(f"  search_history: imported={se['imported_count']} skipped={se['skipped_duplicate_count']} scanned={se['scanned']}")
    typer.echo(f"  subscriptions : imported={su['imported_count']} skipped={su['skipped_duplicate_count']} scanned={su['scanned']}")
    typer.echo(f"  playlists     : playlists={pl['playlists_imported']} items={pl['items_imported']} videos={pl['videos_created']} scanned={pl['scanned_playlists']}")
    typer.echo(f"  liked_videos  : imported={lv['imported_count']} skipped={lv['skipped_duplicate_count']} videos={lv['videos_created']} scanned={lv['scanned']}")


# --------------------------------------------------------------------------- #
# liked-videos
# --------------------------------------------------------------------------- #
@liked_videos_app.command("list")
def liked_videos_list(
    limit: int = typer.Option(50, "--limit"),
    offset: int = typer.Option(0, "--offset"),
    only_missing_metadata: bool = typer.Option(False, "--only-missing-metadata"),
) -> None:
    """List liked videos (most recently liked first)."""
    from app.models import LikedVideo, Video

    with session_scope() as s:
        rows = s.execute(
            select(LikedVideo, Video)
            .join(Video, Video.id == LikedVideo.video_id, isouter=True)
            .order_by(LikedVideo.liked_at.desc().nulls_last(), LikedVideo.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        shown = 0
        for lv, video in rows:
            fetched = bool(video is not None and video.title)
            if only_missing_metadata and fetched:
                continue
            title = (video.title if video and video.title else lv.title) or "(metadata not fetched)"
            flag = "" if fetched else "  [meta?]"
            typer.echo(f"  {lv.youtube_video_id or '-':12} {str(lv.liked_at)[:10]:10}{flag}  {title[:60]}")
            shown += 1
        if shown == 0:
            typer.echo("No liked videos. Import with `takeout import-liked-videos`.")


@liked_videos_app.command("stats")
def liked_videos_stats() -> None:
    """Show liked-videos statistics."""
    from sqlalchemy import func

    from app.models import LikedVideo, Video

    with session_scope() as s:
        total = s.scalar(select(func.count(LikedVideo.id))) or 0
        with_vid = s.scalar(
            select(func.count(LikedVideo.id)).where(LikedVideo.youtube_video_id.is_not(None))
        ) or 0
        linked = s.scalar(
            select(func.count(LikedVideo.id)).where(LikedVideo.video_id.is_not(None))
        ) or 0
        fetched = s.scalar(
            select(func.count(LikedVideo.id))
            .join(Video, Video.id == LikedVideo.video_id)
            .where(Video.title.is_not(None))
        ) or 0
    typer.echo(f"total liked       : {total}")
    typer.echo(f"with video id     : {with_vid}")
    typer.echo(f"linked to videos  : {linked}")
    typer.echo(f"metadata fetched  : {fetched}")


def _liked_filters(source: str, channel: str, title: str, *, missing_metadata=False, missing_body=False):
    from app.services.liked_archive import LikedFilters

    return LikedFilters(
        source=(source or None),
        channel=(channel or None),
        title=(title or None),
        missing_metadata=missing_metadata,
        missing_body=missing_body,
    )


@liked_videos_app.command("enqueue-metadata")
def liked_videos_enqueue_metadata(
    limit: int = typer.Option(20, "--limit", help="Max videos to enqueue."),
    profile: str = typer.Option("metadata_only", "--profile"),
    missing_only: bool = typer.Option(True, "--missing-only/--all", help="Only liked videos missing metadata."),
    source: str = typer.Option("", "--source", help="takeout_my_activity | youtube_data_api | all"),
    channel: str = typer.Option("", "--channel"),
    title: str = typer.Option("", "--title", help="title/channel/id contains"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    now: bool = typer.Option(False, "--now"),
    include_permanent: bool = typer.Option(
        False, "--include-permanent/--retry-permanent",
        help="NOT recommended: also enqueue private/deleted/unavailable (excluded by default).",
    ),
) -> None:
    """Enqueue metadata_only jobs for liked videos (NEVER downloads the body).
    Permanent failures (private/deleted/unavailable) are excluded by default."""
    from app.services import liked_archive as la

    _ensure_profile(profile)
    with session_scope() as s:
        r = la.enqueue_metadata(
            s, get_settings(),
            filters=_liked_filters(source, channel, title, missing_metadata=missing_only),
            limit=limit, profile=profile, dry_run=dry_run, submit=not now,
            include_permanent=include_permanent,
        )
        job_ids = list(r.job_ids)
        typer.echo(
            f"[metadata_only — body NOT downloaded] selected={r.selected_count} created={r.jobs_created} "
            f"skipped_existing={r.skipped_existing_job} skipped_has_metadata={r.skipped_already_has_metadata} "
            f"skipped_permanent={r.skipped_permanent}"
            + ("  (dry-run)" if dry_run else "")
        )
    if now and not dry_run:
        for jid in job_ids:
            _dispatch(jid, True)


@liked_videos_app.command("plan-archive")
def liked_videos_plan_archive(
    limit: int = typer.Option(0, "--limit", help="0 = use default limit."),
    profile: str = typer.Option("", "--profile", help="Body profile (default LIKED_ARCHIVE_DEFAULT_PROFILE)."),
    source: str = typer.Option("", "--source"),
    channel: str = typer.Option("", "--channel"),
    title: str = typer.Option("", "--title"),
) -> None:
    """Preview a liked-videos archive run (no jobs are created)."""
    from app.services import liked_archive as la

    with session_scope() as s:
        plan = la.archive_plan(
            s, get_settings(),
            filters=_liked_filters(source, channel, title),
            profile=(profile or None), limit=(limit or None),
        )
    typer.echo("== liked archive plan ==")
    typer.echo(f"  candidates:        {plan.total_candidates}")
    typer.echo(f"  missing metadata:  {plan.missing_metadata}")
    typer.echo(f"  missing body:      {plan.missing_body}")
    typer.echo(f"  already have body: {plan.has_body}")
    typer.echo(f"  active jobs:       {plan.existing_active_jobs}")
    typer.echo(f"  retryable (liked): {plan.existing_retryable}")
    typer.echo(f"  recommended limit: {plan.recommended_limit}")
    typer.echo(f"  recommended delay: {plan.recommended_delay_seconds}s")
    typer.echo(f"  body profile:      {plan.profile}")
    for n in plan.notes:
        typer.echo(f"  - {n}")


@liked_videos_app.command("enqueue-archive")
def liked_videos_enqueue_archive(
    limit: int = typer.Option(0, "--limit", help="0 = use LIKED_ARCHIVE_DEFAULT_LIMIT."),
    profile: str = typer.Option("", "--profile", help="Body profile (default LIKED_ARCHIVE_DEFAULT_PROFILE)."),
    missing_body_only: bool = typer.Option(True, "--missing-body-only/--all", help="Only liked videos without a saved body."),
    source: str = typer.Option("", "--source"),
    channel: str = typer.Option("", "--channel"),
    title: str = typer.Option("", "--title"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Enqueue a BODY archive for liked videos (DOWNLOADS the video body!)."""
    from app.services import liked_archive as la

    settings = get_settings()
    prof = profile or settings.liked_archive_default_profile
    _ensure_profile(prof)
    with session_scope() as s:
        r = la.enqueue_archive(
            s, settings,
            filters=_liked_filters(source, channel, title, missing_body=missing_body_only),
            limit=(limit or None), profile=prof, dry_run=dry_run, submit=not now,
        )
        job_ids = list(r.job_ids)
    typer.secho(
        f"[VIDEO BODY DOWNLOAD — profile {prof}] selected={r.selected_count} created={r.jobs_created} "
        f"skipped_existing={r.skipped_existing_job} skipped_has_body={r.skipped_already_has_body}"
        + ("  (dry-run, no jobs created)" if dry_run else ""),
        fg=typer.colors.YELLOW,
    )
    if now and not dry_run:
        for jid in job_ids:
            _dispatch(jid, True)


@liked_videos_app.command("failures")
def liked_videos_failures() -> None:
    """Phase 7H: failed/partial liked-archive jobs grouped by reason
    (private/deleted/unavailable/network/rate_limited/unknown). Counts only."""
    from app.services import liked_archive as la

    with session_scope() as s:
        fb = la.failure_breakdown(s)
    typer.echo("== liked-archive failure breakdown ==")
    typer.echo(
        f"  attempts: failed={fb['total_failed']} partial={fb['total_partial']} retryable={fb['retryable']} "
        f"permanent_attempts={fb['permanent']} | permanent_unique_videos={fb.get('permanent_unique_videos')}"
    )
    if fb["attempts_by_reason"]:
        typer.echo(f"  {'reason':<14} {'attempts':>9} {'unique_videos':>14}")
        uniq = fb.get("unique_videos_by_reason", {})
        for reason, n in fb["attempts_by_reason"].items():
            typer.echo(f"    {reason:<12} {n:>9} {uniq.get(reason, 0):>14}")
    else:
        typer.echo("    (no failed liked-archive jobs)")


@queue_app.command("status")
def queue_status_cmd() -> None:
    """Show queued/running jobs by type and source_action."""
    from app.services import queue_health

    with session_scope() as s:
        q = queue_health.queue_status(s)
    typer.echo("== queue status ==")
    typer.echo(f"  queued: {q['queued']}  running: {q['running']}  total active: {q['total_active']}")
    typer.echo(f"  by type: {q['by_type']}")
    typer.echo(f"  by source_action: {q['by_source_action']}")
    typer.echo(f"  oldest queued: job #{q['oldest_queued_job_id']} at {q['oldest_queued_at']}")
    typer.echo(f"  worker count: {q['worker_count']}")


@liked_videos_app.command("progress")
def liked_videos_progress(
    history: bool = typer.Option(False, "--history", help="Show progress snapshots over time."),
) -> None:
    """Show liked-archive progress (metadata/body saved, retryable, by source)."""
    from app.services import liked_archive as la

    if history:
        from app.services import scheduler as sch

        with session_scope() as s:
            points = sch.progress_history(s, limit=30)
        if not points:
            typer.echo("No progress history yet (run a scheduler pass first).")
            return
        typer.echo("== liked progress history (oldest -> newest) ==")
        for p in points:
            typer.echo(
                f"  {p['at']}  {p['run_type']:<16} meta {p['metadata_fetched']}/{p['total_liked']} "
                f"body {p['body_saved']}/{p['total_liked']} retryable={p['retryable_liked_jobs']}"
            )
        return

    with session_scope() as s:
        p = la.progress(s, get_settings())
    typer.echo("== liked archive progress ==")
    typer.echo(f"  total liked (unique): {p['total_liked']}")
    typer.echo(f"  metadata fetched:     {p['metadata_fetched']}  (broad: >=1 metadata media; missing {p['metadata_missing']})")
    typer.echo(
        f"    ├ info_json complete: {p.get('info_json_complete_count', 0)}  "
        f"(use THIS for full-metadata decisions, not the broad count)"
    )
    typer.echo(
        f"    └ description-only:   {p.get('description_only_count', 0)}  "
        f"(retryable partial {p.get('retryable_partial_count', 0)} — upgradeable via retry-metadata)"
    )
    typer.echo(
        f"  eligible missing:     {p['eligible_metadata_missing']}  "
        f"(skipped permanent {p['skipped_permanent_metadata']}; "
        f"permanent unique videos {p['permanent_unique_videos']} — kept, not retried)"
    )
    typer.echo(f"  body saved:           {p['body_saved']}  (missing {p['body_missing']})")
    typer.echo(f"  active archive jobs:  {p['active_archive_jobs']}")
    typer.echo(f"  retryable / failed / partial: {p['retryable_liked_jobs']} / {p['failed_liked_jobs']} / {p['partial_liked_jobs']}")
    typer.echo(f"  by source: {p['by_source']}")
    if p["by_channel"]:
        typer.echo("  top channels: " + ", ".join(f"{c['channel']}({c['count']})" for c in p["by_channel"][:5]))
    typer.echo(f"  last archive job at: {p['last_archive_job_at']}  | last success: {p['last_successful_archive_at']}")


@liked_videos_app.command("retryable")
def liked_videos_retryable(
    limit: int = typer.Option(50, "--limit"),
    reason: str = typer.Option("", "--reason", help="filter by classification reason"),
) -> None:
    """List retryable liked-archive jobs (failed/partial, under the attempt cap)."""
    from app.services import liked_archive as la

    with session_scope() as s:
        rows = la.retryable_liked(s, get_settings(), reason=(reason or None), limit=limit)
        if not rows:
            typer.echo("No retryable liked-archive jobs.")
            return
        for j, c in rows:
            typer.echo(
                f"  job #{j.id}  {j.status:<15} retry={j.retry_count or 0}  "
                f"reasons={c['reasons']}  {j.url}"
            )


@liked_videos_app.command("retry-failed")
def liked_videos_retry_failed(
    limit: int = typer.Option(20, "--limit"),
    reason: str = typer.Option("", "--reason", help="only retry this reason (e.g. rate_limited)"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Re-queue retryable liked-archive jobs (respects the attempt cap)."""
    from app.services import liked_archive as la

    with session_scope() as s:
        if now:
            rows = la.retryable_liked(s, get_settings(), reason=(reason or None), limit=limit)
            job_ids = [j.id for j, _c in rows]
            for j, _c in rows:
                jobs_svc.retry_job(s, j)
            s.commit()
        else:
            job_ids = la.retry_failed_liked(s, get_settings(), reason=(reason or None), limit=limit)
    typer.echo(f"Re-queued {len(job_ids)} liked-archive job(s): {job_ids}")
    if now:
        for jid in job_ids:
            _dispatch(jid, True)


@liked_videos_app.command("metadata-run")
def liked_videos_metadata_run(
    limit: int = typer.Option(100, "--limit", help="Max videos to attempt this run (ignored with --all)."),
    all_missing: bool = typer.Option(False, "--all", help="Fetch metadata for ALL missing (needs --confirm)."),
    confirm: bool = typer.Option(False, "--confirm", help="Required with --all."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only (no jobs, no body)."),
    include_permanent: bool = typer.Option(
        False, "--include-permanent/--retry-permanent", help="NOT recommended: also retry "
        "private/deleted/unavailable (excluded by default).",
    ),
) -> None:
    """Phase 7I: safe staged liked-metadata fetch. Loops capped batches, waits for
    the worker, and STOPS if the 429 (rate_limited) ratio gets too high. metadata
    only — never downloads the body. Permanent failures (private/deleted/
    unavailable) are excluded by default (use --include-permanent to retry)."""
    from app.services import liked_archive as la

    if all_missing and not confirm:
        raise typer.BadParameter("--all requires --confirm (fetches metadata for ALL eligible missing videos)")
    target = None if all_missing else max(1, limit)
    settings = get_settings()
    apply = not dry_run
    typer.echo(
        f"== metadata-run ==  apply={apply} target={'all' if target is None else target} "
        f"include_permanent={include_permanent} (cap/run={settings.liked_metadata_max_enqueue_per_run})"
    )
    sys.stdout.flush()

    def _emit_batch(b: dict) -> None:
        # Phase 7L: stream each batch AS IT COMPLETES (flushed) so detached runs
        # show live batch ratios in /logs/mr*.log instead of nothing until the end.
        typer.echo(
            f"  batch {b['batch']}: selected={b.get('selected')} skipped_permanent={b.get('skipped_permanent')} "
            f"attempted={b['attempted']} success={b['success']} rate_limited={b['rate_limited']} "
            f"ratio={b['ratio']} level={b['level']} by_reason={b['by_reason']}"
        )
        sys.stdout.flush()

    res = la.metadata_run(settings, target_limit=target, apply=apply,
                          include_permanent=include_permanent, on_batch=_emit_batch)
    if not res["ok"]:
        typer.echo(f"  BLOCKED: {res.get('message')}")
        raise typer.Exit(code=1)
    if not res["apply"]:
        typer.echo(
            f"  plan: missing={res['metadata_missing']} eligible={res.get('eligible_metadata_missing')} "
            f"permanent_unique={res.get('permanent_unique_videos')} "
            f"would-enqueue(first batch)={res.get('plan_selected')} skipped_permanent={res.get('skipped_permanent')}"
        )
        typer.echo(f"  {res.get('message')}")
        return
    # batches were streamed live via _emit_batch above.
    typer.echo(
        f"  enqueued={res['enqueued_total']} attempted={res['attempted']} rate_limited={res['rate_limited']} "
        f"skipped_permanent={res.get('skipped_permanent')} eligible_missing={res.get('eligible_metadata_missing')} "
        f"permanent_kept={res.get('permanent_unique_videos')}"
    )
    # Phase 7L: final level reflects the WORST batch + any STOP, not just the average.
    typer.echo(
        f"  LEVEL={res['level'].upper()}  (overall ratio={res.get('overall_ratio')} [{res.get('overall_level')}], "
        f"worst batch={res.get('worst_batch_ratio')} [{res.get('worst_batch_level')}], stopped={res['stopped_reason']})"
    )
    if res.get("batch_stop_triggered"):
        typer.echo(
            "  ⚠ a batch hit the STOP threshold — overall ratio may read OK but the run HALTED; "
            "do NOT scale up. Set PO-token / raise delay, then retry-metadata later."
        )
    typer.echo(
        f"  metadata_fetched(broad) {res['metadata_fetched_before']} -> {res['metadata_fetched_after']} "
        f"| info_json complete -> {res.get('info_json_complete_after')} "
        f"| db {res['db_size_mb_before']} -> {res['db_size_mb_after']} MB"
    )
    typer.echo(f"  next: {res['recommended_next']}")
    if res["level"] == "stop":
        raise typer.Exit(code=2)  # signal high rate-limit so scripts halt full runs


@liked_videos_app.command("retry-metadata")
def liked_videos_retry_metadata(
    limit: int = typer.Option(50, "--limit"),
    reason: str = typer.Option("", "--reason", help="only retry this reason (rate_limited|network)"),
    retryable: bool = typer.Option(False, "--retryable", help="retry all retryable reasons"),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Phase 7I: re-queue retryable liked METADATA jobs only. private/deleted/
    unavailable (permanent) are NEVER retried."""
    from app.services import liked_archive as la

    if not reason and not retryable:
        retryable = True  # default: all retryable reasons
    with session_scope() as s:
        job_ids = la.retry_failed_liked(
            s, get_settings(), reason=(reason or None), limit=limit, submit=not now, metadata_only=True
        )
    typer.echo(
        f"Re-queued {len(job_ids)} retryable metadata job(s)"
        + (f" (reason={reason})" if reason else " (all retryable)")
        + " — permanent (private/deleted/unavailable) excluded."
    )
    if now:
        for jid in job_ids:
            _dispatch(jid, True)


# --------------------------------------------------------------------------- #
# library (hybrid bootstrap)
# --------------------------------------------------------------------------- #
@library_app.command("bootstrap")
def library_bootstrap(
    youtube_takeout: str = typer.Option("", "--youtube-takeout", help="YouTube Takeout ZIP (under TAKEOUT_IMPORT_ROOT)."),
    myactivity_takeout: str = typer.Option("", "--myactivity-takeout", help="My Activity Takeout ZIP."),
    limit_liked: int = typer.Option(0, "--limit-liked"),
    limit_watch: int = typer.Option(0, "--limit-watch"),
    limit_search: int = typer.Option(0, "--limit-search"),
    limit_subscriptions: int = typer.Option(0, "--limit-subscriptions"),
    limit_playlists: int = typer.Option(0, "--limit-playlists"),
    limit_items: int = typer.Option(0, "--limit-items"),
    use_api: bool = typer.Option(False, "--use-api", help="Also top-up via the YouTube Data API (if configured)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """First-time hybrid build: YouTube Takeout + My Activity Takeout (+ optional API)."""
    from app.services import library as lib

    if not youtube_takeout and not myactivity_takeout and not use_api:
        raise typer.BadParameter("provide --youtube-takeout and/or --myactivity-takeout (or --use-api)")
    with session_scope() as s:
        r = lib.bootstrap(
            s, get_settings(),
            youtube_takeout_path=(youtube_takeout or None),
            myactivity_takeout_path=(myactivity_takeout or None),
            limit_watch=(limit_watch or None), limit_search=(limit_search or None),
            limit_subscriptions=(limit_subscriptions or None), limit_playlists=(limit_playlists or None),
            limit_items=(limit_items or None), limit_liked=(limit_liked or None),
            use_api=use_api, dry_run=dry_run,
        )
    typer.echo(f"dry_run={r['dry_run']}")
    if r.get("youtube_takeout"):
        yt = r["youtube_takeout"]
        if "error" in yt:
            typer.echo(f"  youtube_takeout : ERROR {yt['error']}")
        else:
            typer.echo(
                f"  youtube_takeout : watch={yt['watch_history']['imported_count']} "
                f"search={yt['search_history']['imported_count']} subs={yt['subscriptions']['imported_count']} "
                f"playlists={yt['playlists']['playlists_imported']} liked={yt['liked_videos']['imported_count']}"
            )
    if r.get("myactivity_takeout"):
        ma = r["myactivity_takeout"]
        if "error" in ma:
            typer.echo(f"  myactivity      : ERROR {ma['error']}")
        else:
            lv = ma["liked_videos"]
            typer.echo(
                f"  myactivity liked: imported={lv['imported_count']} skipped={lv['skipped_duplicate_count']} "
                f"videos={lv['videos_created']} scanned={lv['scanned']} source={lv.get('source_kind')}"
            )
    if r.get("api"):
        ap = r["api"]
        if "error" in ap:
            typer.echo(f"  api liked       : unavailable ({ap['error']})")
        else:
            lv = ap["liked_videos"]
            typer.echo(f"  api liked       : imported={lv['imported_count']} stopped_on_existing={lv['stopped_on_existing']}")


# --------------------------------------------------------------------------- #
# youtube-api (OAuth differential sync)
# --------------------------------------------------------------------------- #
@youtube_api_app.command("status")
def youtube_api_status() -> None:
    """Show YouTube Data API OAuth status (no secrets/paths shown)."""
    from app.services import youtube_api as yt

    st = yt.status_dict(get_settings())
    for k in ("enabled", "client_secret_present", "token_present", "configured", "method"):
        typer.echo(f"  {k:22}: {st[k]}")


@youtube_api_app.command("authorize")
def youtube_api_authorize() -> None:
    """Run the OAuth installed-app flow (needs a browser) and store the token."""
    from app.services import youtube_api as yt

    try:
        path = yt.authorize(get_settings())
    except yt.YouTubeApiError as exc:
        raise typer.BadParameter(f"[{exc.classification}] {exc.message}")
    typer.echo(f"OAuth token stored ({path.name}). You can now run `youtube-api sync-liked`.")


@youtube_api_app.command("sync-liked")
def youtube_api_sync_liked(
    limit: int = typer.Option(0, "--limit", help="Max liked videos to scan (0 = all available)."),
    method: str = typer.Option("", "--method", help="videos | playlist | auto"),
    stop_on_existing: bool = typer.Option(True, "--stop-on-existing/--no-stop-on-existing"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Differentially sync liked videos from the YouTube Data API (source=youtube_data_api)."""
    from app.services import youtube_api as yt

    with session_scope() as s:
        try:
            r = yt.sync_liked(
                s, get_settings(), method=(method or None),
                stop_on_existing=stop_on_existing, limit=(limit or None), dry_run=dry_run,
            )
        except yt.YouTubeApiError as exc:
            typer.echo(f"YouTube Data API unavailable: [{exc.classification}] {exc.message}")
            typer.echo("Hint: set YOUTUBE_API_ENABLED=true, provide a client secret, and run `youtube-api authorize`.")
            return
    typer.echo(
        f"sync-liked: imported={r['imported_count']} skipped={r['skipped_duplicate_count']} "
        f"videos_created={r['videos_created']} scanned={r['scanned']} "
        f"stopped_on_existing={r['stopped_on_existing']} dry_run={r['dry_run']}"
    )


# --------------------------------------------------------------------------- #
# search-history
# --------------------------------------------------------------------------- #
@search_history_app.command("list")
def search_history_list(
    limit: int = typer.Option(20, "--limit"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """List recent search-history events."""
    from app.models import SearchHistoryEvent

    with session_scope() as s:
        rows = list(
            s.scalars(
                select(SearchHistoryEvent)
                .order_by(SearchHistoryEvent.searched_at.desc(), SearchHistoryEvent.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        if not rows:
            typer.echo("No search-history events. Import a Takeout ZIP first.")
            return
        for r in rows:
            typer.echo(f"  {r.searched_at}  {r.query}")


@search_history_app.command("stats")
def search_history_stats() -> None:
    """Show search-history statistics."""
    from sqlalchemy import func

    from app.models import SearchHistoryEvent

    with session_scope() as s:
        total = s.scalar(select(func.count(SearchHistoryEvent.id))) or 0
        distinct = s.scalar(select(func.count(func.distinct(SearchHistoryEvent.query)))) or 0
        earliest = s.scalar(select(func.min(SearchHistoryEvent.searched_at)))
        latest = s.scalar(select(func.max(SearchHistoryEvent.searched_at)))
        top = s.execute(
            select(SearchHistoryEvent.query, func.count(SearchHistoryEvent.id))
            .where(SearchHistoryEvent.query.is_not(None))
            .group_by(SearchHistoryEvent.query)
            .order_by(func.count(SearchHistoryEvent.id).desc())
            .limit(10)
        ).all()
    typer.echo(f"total           : {total}")
    typer.echo(f"distinct queries: {distinct}")
    typer.echo(f"range           : {earliest}  ->  {latest}")
    typer.echo("top queries:")
    for q, n in top:
        typer.echo(f"  {n:>6}  {q}")


# --------------------------------------------------------------------------- #
# subscriptions
# --------------------------------------------------------------------------- #
@subscriptions_app.command("list")
def subscriptions_list(limit: int = typer.Option(30, "--limit")) -> None:
    """List imported subscriptions (channel collections)."""
    from app.models import Collection

    with session_scope() as s:
        rows = list(
            s.scalars(
                select(Collection).where(Collection.type == "channel").order_by(Collection.id).limit(limit)
            )
        )
        if not rows:
            typer.echo("No subscriptions. Run `takeout import-subscriptions` first.")
            return
        for c in rows:
            typer.echo(f"  #{c.id:<4} {c.youtube_channel_id or '-':<26}  {c.title}")


@subscriptions_app.command("enqueue")
def subscriptions_enqueue(
    videos: bool = typer.Option(False, "--videos"),
    shorts: bool = typer.Option(False, "--shorts"),
    streams: bool = typer.Option(False, "--streams"),
    profile: str = typer.Option(None, "--profile", "-p"),
    max_items: int = typer.Option(0, "--max-items"),
    limit: int = typer.Option(0, "--limit", help="Max channels to enqueue (0 = all)."),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Enqueue expand jobs for subscribed channels (selected tabs)."""
    from app.models import Collection
    from app.services.urls import UrlError, channel_tab_url, normalize_url

    profile = profile or get_settings().default_profile
    _ensure_profile(profile)
    tabs = [t for t, on in (("videos", videos), ("shorts", shorts), ("streams", streams)) if on]
    if not tabs:
        raise typer.BadParameter("specify at least one of --videos / --shorts / --streams")

    job_ids: list[int] = []
    with session_scope() as s:
        stmt = select(Collection).where(Collection.type == "channel").order_by(Collection.id)
        if limit:
            stmt = stmt.limit(limit)
        subs = list(s.scalars(stmt))
        for c in subs:
            url = c.url or (
                f"https://www.youtube.com/channel/{c.youtube_channel_id}"
                if c.youtube_channel_id
                else None
            )
            if not url:
                continue
            try:
                parsed = normalize_url(url)
            except UrlError:
                continue
            for tab in tabs:
                job = jobs_svc.create_job_for_url(
                    s, channel_tab_url(parsed, tab), profile, max_items=(max_items or None)
                )
                job_ids.append(job.id)
    typer.echo(f"Created {len(job_ids)} expand job(s) for {len(subs)} channel(s).")
    for jid in job_ids:
        _dispatch(jid, now)


# --------------------------------------------------------------------------- #
# watch-history
# --------------------------------------------------------------------------- #
@watch_history_app.command("list")
def watch_history_list(
    limit: int = typer.Option(20, "--limit"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """List recent watch-history events."""
    from app.models import WatchHistoryEvent

    with session_scope() as s:
        rows = list(
            s.scalars(
                select(WatchHistoryEvent)
                .order_by(WatchHistoryEvent.watched_at.desc(), WatchHistoryEvent.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        if not rows:
            typer.echo("No watch-history events. Import a Takeout ZIP first.")
            return
        for r in rows:
            typer.echo(
                f"  {r.watched_at}  {r.youtube_video_id or '-':<11}  "
                f"{(r.title or '')[:48]:<48}  {r.channel_title or ''}"
            )


@watch_history_app.command("stats")
def watch_history_stats() -> None:
    """Show watch-history statistics."""
    from sqlalchemy import func

    from app.models import WatchHistoryEvent

    with session_scope() as s:
        total = s.scalar(select(func.count(WatchHistoryEvent.id))) or 0
        with_vid = s.scalar(
            select(func.count(WatchHistoryEvent.id)).where(
                WatchHistoryEvent.youtube_video_id.is_not(None)
            )
        ) or 0
        distinct_videos = s.scalar(
            select(func.count(func.distinct(WatchHistoryEvent.youtube_video_id)))
        ) or 0
        distinct_channels = s.scalar(
            select(func.count(func.distinct(WatchHistoryEvent.channel_title)))
        ) or 0
        earliest = s.scalar(select(func.min(WatchHistoryEvent.watched_at)))
        latest = s.scalar(select(func.max(WatchHistoryEvent.watched_at)))
        top = s.execute(
            select(WatchHistoryEvent.channel_title, func.count(WatchHistoryEvent.id))
            .where(WatchHistoryEvent.channel_title.is_not(None))
            .group_by(WatchHistoryEvent.channel_title)
            .order_by(func.count(WatchHistoryEvent.id).desc())
            .limit(10)
        ).all()
    typer.echo(f"total            : {total}")
    typer.echo(f"with video id    : {with_vid}")
    typer.echo(f"distinct videos  : {distinct_videos}")
    typer.echo(f"distinct channels: {distinct_channels}")
    typer.echo(f"range            : {earliest}  ->  {latest}")
    typer.echo("top channels:")
    for name, n in top:
        typer.echo(f"  {n:>6}  {name}")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _set_collection_enabled(collection_id: int, enabled: bool) -> None:
    from app.models import Collection

    with session_scope() as s:
        c = s.get(Collection, collection_id)
        if c is None:
            raise typer.BadParameter("collection not found")
        c.enabled = enabled
    typer.echo(f"Collection #{collection_id} {'enabled' if enabled else 'disabled'}.")


def _expand_and_dispatch(url: str, profile: str, now: bool, max_items: int) -> None:
    mi = max_items if max_items and max_items > 0 else None
    with session_scope() as s:
        job = jobs_svc.create_job_for_url(s, url, profile, max_items=mi)
        job_id = job.id
    typer.echo(f"Created expand job #{job_id} ({profile}) for {url}")
    _dispatch(job_id, now)


def _comments_refresh_one(video_or_url: str, profile: str, now: bool) -> None:
    _ensure_profile(profile)
    with session_scope() as s:
        video = jobs_svc.resolve_or_create_video(s, video_or_url)
        if video is None:
            raise typer.BadParameter(f"could not resolve video: {video_or_url!r}")
        job = jobs_svc.create_comments_refresh_job(s, video, profile_name=profile)
        job_id = job.id
    typer.echo(f"Created comments_refresh job #{job_id} for {video_or_url}")
    _dispatch(job_id, now)


def _ensure_profile(name: str) -> None:
    with session_scope() as s:
        try:
            get_profile_spec(s, name)
        except KeyError:
            raise typer.BadParameter(
                f"unknown profile {name!r}. Known: {', '.join(list_profile_names())}"
            )


def _dispatch(job_id: int, now: bool) -> None:
    """Either run a job inline (``now``) or submit it to RQ."""
    if now:
        from app.worker.tasks import run_job

        run_job(job_id)
        _print_job_summary([job_id])
        return
    try:
        rq_id = jobs_svc.submit_job(job_id)
        typer.echo(f"Submitted job #{job_id} to RQ ({rq_id}).")
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"Could not reach Redis ({exc}). Job #{job_id} stays queued in the DB. "
            f"Run it now with: archiver download run  (or re-run with --now)."
        )


def _print_job_summary(job_ids: list[int]) -> None:
    with session_scope() as s:
        for jid in job_ids:
            job = s.get(Job, jid)
            if job is None:
                continue
            line = f"  job #{job.id}: {job.status}"
            if job.error_message:
                line += f"  ({job.error_message.splitlines()[0]})"
            typer.echo(line)


def run() -> None:
    """Console-script entrypoint (see pyproject ``[project.scripts]``)."""
    app()


if __name__ == "__main__":
    run()
