#!/usr/bin/env python3
"""Seed a *synthetic* demo dataset for screenshots / public preview.

This is NOT real data: every title, channel, id, comment and liked entry below
is invented. It exists only to make the UI look populated in an **isolated**
demo stack (see docker-compose.demo.yml) so the project can be shown without
exposing anyone's real archive.

Safety
------
The script refuses to run unless BOTH hold, so it can never touch a real archive:
  * env ``YA_DEMO_SEED_CONFIRM=yes`` is set, and
  * the target database has almost no videos (``<= 25``) — a populated archive
    aborts the run.

Run it inside the isolated demo stack only:
    docker compose -p ya-demo -f docker-compose.yml -f docker-compose.demo.yml \
        exec -e YA_DEMO_SEED_CONFIRM=yes web python scripts/demo_seed.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# Fixed base instant so the demo is deterministic (no wall-clock dependence).
BASE = datetime(2026, 6, 1, 9, 0, 0)


def _d(days: int = 0, hours: int = 0) -> datetime:
    return BASE - timedelta(days=days, hours=hours)


# 12 synthetic videos. availability: available | unavailable | private
VIDEOS = [
    # (vid, title, channel, dur, is_short, availability, archived, upload)
    ("demoAAA000001", "Building a Tiny Home Server from Scratch", "Homelab Corner", 1832, False, "available", True, "20260410"),
    ("demoAAA000002", "10-Minute Sourdough — No Fancy Tools", "Slow Kitchen", 641, False, "available", True, "20260415"),
    ("demoAAA000003", "How Rivers Shape Mountains (Explained)", "Field Notes", 903, False, "available", True, "20260420"),
    ("demoAAA000004", "Lo-Fi Beats for Focus — 1 Hour Mix", "Quiet Hours", 3600, False, "available", True, "20260101"),
    ("demoAAA000005", "Repairing a 40-Year-Old Cassette Deck", "Fix It Again", 2450, False, "available", True, "20260322"),
    ("demoAAA000006", "The Basics of Watercolor Shadows", "Studio Light", 1178, False, "available", False, "20260501"),
    ("demoAAA000007", "Quick Tip: Tie a Bowline in 5 Seconds", "Knot Today", 47, True, "available", False, "20260503"),
    ("demoAAA000008", "Trail Review — Foggy Ridge Loop", "Weekend Walks", 812, False, "available", False, "20260506"),
    ("demoAAA000009", "Why Bread Goes Stale (Food Science)", "Slow Kitchen", 1024, False, "unavailable", False, "20260210"),
    ("demoAAA000010", "Desk Setup Tour 2026", "Homelab Corner", 733, False, "private", False, "20260228"),
    ("demoAAA000011", "One-Pan Roasted Veg — Weeknight Dinner", "Slow Kitchen", 522, True, "available", False, "20260509"),
    ("demoAAA000012", "Reading the Night Sky Without an App", "Field Notes", 1360, False, "available", True, "20260118"),
]

# (video_index, author, text, likes)
COMMENTS = [
    (0, "quiet_maker", "This finally got my old NUC doing something useful. Thanks!", 42),
    (0, "runbook_rachel", "Would love a follow-up on backups.", 17),
    (0, "port_forwarder", "The section on reverse proxies was gold.", 9),
    (1, "flour_first", "Tried it this weekend — turned out great with rye.", 55),
    (1, "no_knead_ned", "No stand mixer needed, love it.", 6),
    (4, "solder_sam", "The belt replacement tip saved my deck.", 23),
]

# playlists (title, [video_indexes])
COLLECTIONS = [
    ("Weekend Projects", [0, 4, 5, 7]),
    ("Kitchen — Saved Recipes", [1, 8, 10]),
]

# liked entries (video_index or None for an unresolved fallback)
LIKED = [0, 1, 2, 3, 4, 5, 8, 11]


def main() -> int:
    if os.environ.get("YA_DEMO_SEED_CONFIRM") != "yes":
        print("refusing: set YA_DEMO_SEED_CONFIRM=yes to seed the demo dataset", file=sys.stderr)
        return 2

    # Import here so the safety check above runs even if the app import is heavy.
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import (
        Collection,
        CollectionItem,
        Comment,
        Job,
        LikedVideo,
        MediaFile,
        Video,
    )

    with session_scope() as db:
        existing = db.execute(select(func.count(Video.id))).scalar_one()
        if existing > 25:
            print(
                f"refusing: target DB already has {existing} videos — this does not "
                "look like an empty demo database. Aborting to protect real data.",
                file=sys.stderr,
            )
            return 3

        vids: list[Video] = []
        for i, (yid, title, chan, dur, is_short, avail, archived, upload) in enumerate(VIDEOS):
            v = Video(
                youtube_video_id=yid,
                title=title,
                channel_id=f"UCdemo{chan.replace(' ', '')[:12]}",
                channel_title=chan,
                url=f"https://example.invalid/watch?v={yid}",
                duration=dur,
                upload_date=upload,
                description="Synthetic demo entry — not a real video.",
                is_short=is_short,
                availability=avail,
                first_seen_at=_d(days=60 - i),
                last_metadata_refresh_at=_d(days=3, hours=i),
            )
            db.add(v)
            vids.append(v)
        db.flush()  # assign ids

        # a saved "body" (media file) for the archived subset
        for i, (yid, *_rest) in enumerate(VIDEOS):
            if VIDEOS[i][6]:  # archived
                db.add(
                    MediaFile(
                        video_id=vids[i].id,
                        media_type="video",
                        profile="video_compressed_1080p_light",
                        path=f"demo/{yid}/video.mp4",
                        container="mp4",
                        codec_video="h264",
                        codec_audio="aac",
                        width=1920,
                        height=1080,
                        fps=30.0,
                        filesize=120_000_000 + i * 5_000_000,
                    )
                )

        # jobs: a realistic mix of outcomes
        job_specs = [
            ("download", "success", 0), ("download", "success", 1),
            ("download", "success", 2), ("download", "success", 3),
            ("download", "success", 4), ("download", "partial_success", 11),
            ("download", "partial_success", 7), ("download", "failed", 9),
            ("download", "failed", 10), ("metadata_refresh", "success", 5),
            ("metadata_refresh", "success", 6), ("metadata_refresh", "success", 8),
            ("comments_refresh", "success", 0), ("comments_refresh", "partial_success", 1),
        ]
        for k, (jtype, status, vi) in enumerate(job_specs):
            started = _d(days=2, hours=k)
            db.add(
                Job(
                    type=jtype,
                    status=status,
                    url=vids[vi].url,
                    video_id=vids[vi].id,
                    profile_name="video_compressed_1080p_light" if jtype == "download" else None,
                    progress=1.0 if status == "success" else (0.6 if status == "partial_success" else 0.0),
                    started_at=started,
                    finished_at=started + timedelta(minutes=4),
                    error_message=None if status != "failed"
                    else ("video unavailable" if VIDEOS[vi][5] == "unavailable" else "private video — skipped"),
                    created_at=started,
                )
            )

        # 2 playlists
        for title, idxs in COLLECTIONS:
            col = Collection(
                type="playlist",
                youtube_playlist_id=f"PLdemo{title.replace(' ', '')[:16]}",
                title=title,
                url="https://example.invalid/playlist",
                crawl_policy="new_only",
            )
            db.add(col)
            db.flush()
            for pos, vi in enumerate(idxs):
                db.add(
                    CollectionItem(
                        collection_id=col.id,
                        video_id=vids[vi].id,
                        youtube_video_id=VIDEOS[vi][0],
                        position=pos,
                    )
                )

        # comments
        for vi, author, text, likes in COMMENTS:
            db.add(
                Comment(
                    video_id=vids[vi].id,
                    comment_id=f"demo-c-{vi}-{author}",
                    author_name=author,
                    text=text,
                    like_count=likes,
                    published_at=_d(days=5),
                    source="yt-dlp",
                )
            )

        # liked videos
        for j, vi in enumerate(LIKED):
            db.add(
                LikedVideo(
                    source="demo",
                    youtube_video_id=VIDEOS[vi][0],
                    title=VIDEOS[vi][1],
                    channel_title=VIDEOS[vi][2],
                    url=vids[vi].url,
                    liked_at=_d(days=10 + j),
                    video_id=vids[vi].id,
                )
            )

    print(
        f"seeded demo dataset: {len(VIDEOS)} videos, {len(job_specs)} jobs, "
        f"{len(COLLECTIONS)} collections, {len(COMMENTS)} comments, {len(LIKED)} liked."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
