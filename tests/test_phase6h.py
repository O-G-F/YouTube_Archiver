"""Phase 6H regression: real Takeout titles can exceed the title column's
VARCHAR(1024). PostgreSQL enforces the length (SQLite does not), so importers
clip title/channel_title/query to the column max — a single over-long title
must not fail the whole import batch.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import session_scope
from app.models import LikedVideo, SearchHistoryEvent, WatchHistoryEvent
from app.services import takeout as tk


def _zip_with_long_titles(root: Path, name: str) -> str:
    long_title = "あ" * 2000  # 2000 chars > 1024
    acts = [
        {"title": f"視聴済み: {long_title}",
         "titleUrl": "https://www.youtube.com/watch?v=p6hwlong001",
         "time": "2025-02-01T00:00:00Z",
         "subtitles": [{"name": "C" * 800}]},  # channel_title > 512
        {"title": f"高く評価した動画: {long_title}",
         "titleUrl": "https://www.youtube.com/watch?v=p6hllong001",
         "time": "2025-01-01T00:00:00Z",
         "subtitles": [{"name": "Chan"}]},
        {"title": "視聴済み: normal",
         "titleUrl": "https://www.youtube.com/watch?v=p6hwnorm001",
         "time": "2025-02-02T00:00:00Z"},
    ]
    searches = [{"title": "Searched for " + ("q" * 2000),
                 "titleUrl": "https://www.youtube.com/results?search_query=" + ("q" * 2000),
                 "time": "2025-03-01T00:00:00Z"}]
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / name, "w") as z:
        z.writestr("Takeout/マイ アクティビティ/YouTube/MyActivity.json", json.dumps(acts, ensure_ascii=False))
        z.writestr("Takeout/マイ アクティビティ/YouTube/MySearches.json", json.dumps(searches, ensure_ascii=False))
    return name


def test_watch_import_clips_long_title(settings, session):
    name = _zip_with_long_titles(settings.takeout_import_root, "long.zip")
    r = tk.run_import(session, settings, name, store_raw_json=False)
    session.commit()
    assert r["imported_count"] >= 1
    rows = session.query(WatchHistoryEvent).all()
    assert rows
    for ev in rows:
        assert ev.title is None or len(ev.title) <= 1024
        assert ev.channel_title is None or len(ev.channel_title) <= 512


def test_liked_import_clips_long_title(settings, session):
    name = _zip_with_long_titles(settings.takeout_import_root, "longl.zip")
    r = tk.run_import_liked_videos(session, settings, name, store_raw_json=False)
    session.commit()
    assert r["imported_count"] >= 1
    for lv in session.query(LikedVideo).all():
        assert lv.title is None or len(lv.title) <= 1024
        assert lv.channel_title is None or len(lv.channel_title) <= 512


def test_search_import_clips_long_query(settings, session):
    name = _zip_with_long_titles(settings.takeout_import_root, "longs.zip")
    tk.run_import_search(session, settings, name, store_raw_json=False)
    session.commit()
    for ev in session.query(SearchHistoryEvent).all():
        assert ev.query is None or len(ev.query) <= 512


def test_clip_helper():
    assert tk._clip(None, 10) is None
    assert tk._clip("abc", 10) == "abc"
    assert tk._clip("a" * 2000, 1024) == "a" * 1024
