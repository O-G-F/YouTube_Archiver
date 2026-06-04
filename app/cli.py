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
profiles_app = typer.Typer(help="Download profiles.")
collections_app = typer.Typer(help="Inspect and re-crawl playlist/channel collections.")
scheduler_app = typer.Typer(help="Run the collection re-crawl scheduler.")
takeout_app = typer.Typer(help="Google Takeout preview / import.")
watch_history_app = typer.Typer(help="Inspect imported watch history.")
search_history_app = typer.Typer(help="Inspect imported search history.")
subscriptions_app = typer.Typer(help="Takeout subscriptions (channels).")
liked_videos_app = typer.Typer(help="Takeout liked videos library.")
library_app = typer.Typer(help="Hybrid library bootstrap (Takeout + API).")
youtube_api_app = typer.Typer(help="YouTube Data API OAuth (differential liked sync).")
app.add_typer(source_app, name="source")
app.add_typer(download_app, name="download")
app.add_typer(jobs_app, name="jobs")
app.add_typer(comments_app, name="comments")
app.add_typer(live_chat_app, name="live-chat")
app.add_typer(profiles_app, name="profiles")
app.add_typer(collections_app, name="collections")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(takeout_app, name="takeout")
app.add_typer(watch_history_app, name="watch-history")
app.add_typer(search_history_app, name="search-history")
app.add_typer(subscriptions_app, name="subscriptions")
app.add_typer(liked_videos_app, name="liked-videos")
app.add_typer(library_app, name="library")
app.add_typer(youtube_api_app, name="youtube-api")


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
    from rq import Worker

    from app.worker.queue import get_redis

    settings = get_settings()
    settings.ensure_dirs()
    conn = get_redis()
    typer.echo(f"Starting RQ worker on queue {settings.rq_queue!r} ...")
    Worker([settings.rq_queue], connection=conn).work(with_scheduler=False)


@app.command()
def doctor() -> None:
    """Diagnose storage writability, tool versions, and DB/Redis connectivity."""
    from app.services.doctor import run_diagnostics

    result = run_diagnostics(get_settings())
    for c in result["checks"]:
        mark = "OK  " if c["ok"] else "FAIL"
        typer.echo(f"[{mark}] {c['name']:<14} {c['detail']}")
    typer.echo(f"\noverall: {'OK' if result['ok'] else 'PROBLEMS DETECTED'}")
    if not result["ok"]:
        raise typer.Exit(code=1)


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
) -> None:
    """Reset a failed/canceled job to queued and re-run/re-submit it."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise typer.BadParameter("job not found")
        if job.status not in ("failed", "canceled", "partial_success"):
            raise typer.BadParameter(f"cannot retry a {job.status} job")
        jobs_svc.retry_job(s, job)
    typer.echo(f"Job #{job_id} reset to queued.")
    _dispatch(job_id, now)


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
    all_: bool = typer.Option(False, "--all", help="Run both collections and comments."),
) -> None:
    """Run a single scheduler pass now (works even if SCHEDULER_ENABLED=false).

    Choose what to run with ``--collections`` / ``--comments`` / ``--all``.
    With no flag, both parts run (same as ``--all``).
    """
    from app.services.scheduler import run_once

    if all_ or (not collections and not comments):
        do_collections = do_comments = True
    else:
        do_collections, do_comments = collections, comments

    summary = run_once(
        get_settings(),
        reason="manual",
        max_items=(max_items or None),
        do_collections=do_collections,
        do_comments=do_comments,
    )
    typer.echo(
        "scheduler run-once: "
        f"collections_checked={summary['collections_checked']} "
        f"collection_jobs={summary['collection_jobs_created']} "
        f"due_comment_videos={summary['due_comment_videos_checked']} "
        f"comment_jobs={summary['comments_jobs_created']} "
        f"skipped_frozen={summary['skipped_frozen']} "
        f"skipped_recent={summary['skipped_recent']} "
        f"submitted={summary['submitted']}"
    )


@scheduler_app.command("run")
def scheduler_run() -> None:
    """Run the scheduler loop forever (used by the `scheduler` container)."""
    from app.services.scheduler import run_forever

    run_forever(get_settings())


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
def takeout_inspect(path: str = typer.Argument(..., help="ZIP under TAKEOUT_IMPORT_ROOT.")) -> None:
    """Show the structural classification of one Takeout ZIP."""
    from app.services import takeout as tk

    try:
        zip_path = tk.resolve_takeout_path(get_settings(), path)
        with tk.open_archive(zip_path) as a:
            info = a.inspect()
    except tk.TakeoutError as exc:
        raise typer.BadParameter(str(exc))
    typer.echo(f"archive_kind        : {info['archive_kind']}")
    typer.echo(f"has_youtube_takeout : {info['has_youtube_takeout']}")
    typer.echo(f"my_activity_yt_path : {info['my_activity_youtube_path']}")
    typer.echo(f"has_index           : {info['has_index']}")
    typer.echo(f"member_count        : {info['member_count']}")
    typer.echo(f"liked_source_kind   : {info['liked_source_kind']}")
    typer.echo(f"liked_detected_path : {info['liked_detected_path']}")


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
) -> None:
    """Import liked videos (Takeout 'Liked videos' playlist) into liked_videos."""
    from app.services import takeout as tk

    with session_scope() as s:
        try:
            r = tk.run_import_liked_videos(
                s, get_settings(), path, limit=(limit or None), dry_run=dry_run
            )
        except tk.TakeoutError as exc:
            raise typer.BadParameter(str(exc))
    typer.echo(
        f"liked_videos: imported={r['imported_count']} skipped={r['skipped_duplicate_count']} "
        f"failed={r['failed_count']} scanned={r['scanned']} videos_created={r['videos_created']} "
        f"dry_run={r['dry_run']}"
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


@liked_videos_app.command("enqueue-metadata")
def liked_videos_enqueue_metadata(
    limit: int = typer.Option(20, "--limit", help="Max videos to enqueue (0 = all)."),
    profile: str = typer.Option("metadata_only", "--profile"),
    all_: bool = typer.Option(False, "--all", help="Include liked videos that already have metadata."),
    now: bool = typer.Option(False, "--now"),
) -> None:
    """Enqueue metadata_only jobs for liked videos (default: those missing metadata)."""
    from app.models import LikedVideo, Video

    _ensure_profile(profile)
    job_ids: list[int] = []
    with session_scope() as s:
        rows = s.execute(
            select(LikedVideo, Video)
            .join(Video, Video.id == LikedVideo.video_id, isouter=True)
            .where(LikedVideo.youtube_video_id.is_not(None))
            .order_by(LikedVideo.liked_at.desc().nulls_last(), LikedVideo.id.desc())
        ).all()
        seen: set[str] = set()
        for lv, video in rows:
            if limit and len(job_ids) >= limit:
                break
            if not all_ and video is not None and video.title:
                continue
            vid = lv.youtube_video_id
            if not vid or vid in seen:
                continue
            seen.add(vid)
            v = jobs_svc.resolve_or_create_video(s, vid)
            if v is None:
                continue
            job_ids.append(
                jobs_svc.create_job_for_url(
                    s, v.url, profile, extra_meta={"enqueued_by": "liked_videos"}
                ).id
            )
    typer.echo(f"Created {len(job_ids)} {profile} job(s) for liked videos.")
    for jid in job_ids:
        _dispatch(jid, now)


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
