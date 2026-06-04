"""Hybrid library bootstrap (Phase 6B).

First-time DB construction combining:
  - YouTube Takeout  -> watch / search / subscriptions / playlists (+ liked CSV)
  - My Activity Takeout -> liked videos (the large historical backfill)
  - YouTube Data API (optional) -> differential liked top-up

Each part is optional, so the bootstrap works with API-only, Takeout-only, or
both. Liked dedup is cross-source by youtube_video_id (see takeout import).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_setup import get_logger
from app.services import takeout as tk
from app.services import youtube_api

logger = get_logger(__name__)


def bootstrap(
    session: Session,
    settings: Settings,
    *,
    youtube_takeout_path: str | None = None,
    myactivity_takeout_path: str | None = None,
    limit_watch: int | None = None,
    limit_search: int | None = None,
    limit_subscriptions: int | None = None,
    limit_playlists: int | None = None,
    limit_items: int | None = None,
    limit_liked: int | None = None,
    use_api: bool = False,
    dry_run: bool = False,
) -> dict:
    """Run the hybrid bootstrap. Any path may be omitted; returns per-source results."""
    result: dict = {
        "dry_run": dry_run,
        "youtube_takeout": None,
        "myactivity_takeout": None,
        "api": None,
    }

    if youtube_takeout_path:
        try:
            result["youtube_takeout"] = {
                "watch_history": tk.run_import(session, settings, youtube_takeout_path, limit=limit_watch, dry_run=dry_run),
                "search_history": tk.run_import_search(session, settings, youtube_takeout_path, limit=limit_search, dry_run=dry_run),
                "subscriptions": tk.run_import_subscriptions(session, settings, youtube_takeout_path, limit=limit_subscriptions, dry_run=dry_run),
                "playlists": tk.run_import_playlists(session, settings, youtube_takeout_path, limit_playlists=limit_playlists, limit_items=limit_items, dry_run=dry_run),
                "liked_videos": tk.run_import_liked_videos(session, settings, youtube_takeout_path, limit=limit_liked, dry_run=dry_run),
            }
        except tk.TakeoutError as exc:
            result["youtube_takeout"] = {"error": str(exc)}

    if myactivity_takeout_path:
        try:
            result["myactivity_takeout"] = {
                "liked_videos": tk.run_import_liked_videos(
                    session, settings, myactivity_takeout_path, limit=limit_liked, dry_run=dry_run
                ),
            }
        except tk.TakeoutError as exc:
            result["myactivity_takeout"] = {"error": str(exc)}

    if use_api:
        try:
            result["api"] = {
                "liked_videos": youtube_api.sync_liked(
                    session, settings, stop_on_existing=True, limit=limit_liked, dry_run=dry_run
                )
            }
        except youtube_api.YouTubeApiError as exc:
            result["api"] = {"error": exc.classification, "message": exc.message}

    logger.info("library bootstrap done (dry_run=%s)", dry_run)
    return result
