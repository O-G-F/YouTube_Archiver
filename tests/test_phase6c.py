"""Phase 6C tests: incremental Takeout re-import, streaming, source registry,
and import-session history.

No raw_json / full paths / personal rows are stored in sessions; dry-run writes
nothing; re-import skips duplicates and can enrich stubs (updated).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import LikedVideo, TakeoutImportSession, Video, WatchHistoryEvent
from app.services import takeout as tk


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _myactivity_zip(root: Path, name: str, *, n_liked: int = 5, n_watch: int = 3) -> str:
    acts = []
    for i in range(n_liked):
        acts.append({
            "title": f"高く評価した動画: Liked {i}",
            "titleUrl": f"https://www.youtube.com/watch?v=liked{i:06d}",
            "time": f"2025-01-{i+1:02d}T00:00:00Z",
            "subtitles": [{"name": f"Chan{i}", "url": f"https://www.youtube.com/channel/UC{i:020d}"}],
        })
    for i in range(n_watch):
        acts.append({
            "title": f"視聴済み: Watched {i}",
            "titleUrl": f"https://www.youtube.com/watch?v=watch{i:06d}",
            "time": f"2025-02-{i+1:02d}T00:00:00Z",
        })
    root.mkdir(parents=True, exist_ok=True)
    zp = root / name
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("Takeout/マイ アクティビティ/YouTube/MyActivity.json", json.dumps(acts, ensure_ascii=False))
    return name


def _youtube_takeout_zip(root: Path, name: str) -> str:
    """A YouTube Takeout with watch-history.json + likes=0 + index."""
    root.mkdir(parents=True, exist_ok=True)
    zp = root / name
    watch = [{"title": "Watched A", "titleUrl": "https://www.youtube.com/watch?v=aaaa1111111",
              "time": "2025-03-01T00:00:00Z", "subtitles": [{"name": "ChanA"}]}]
    with zipfile.ZipFile(zp, "w") as z:
        base = "Takeout/YouTube and YouTube Music/"
        z.writestr(base + "history/watch-history.json", json.dumps(watch))
        z.writestr("Takeout/archive_browser.html", "<html>index</html>")
    return name


# --------------------------------------------------------------------------- #
# streaming + registry
# --------------------------------------------------------------------------- #
def test_streaming_liked_parse(settings):
    name = _myactivity_zip(settings.takeout_import_root, "ma.zip", n_liked=5)
    zp = tk.resolve_takeout_path(settings, name)
    with tk.open_archive(zp) as a:
        liked = list(a.iter_liked_videos())
    assert len(liked) == 5
    assert all(lv.source == "takeout_my_activity" and lv.youtube_video_id for lv in liked)


def test_registry_my_activity(settings):
    name = _myactivity_zip(settings.takeout_import_root, "ma2.zip")
    zp = tk.resolve_takeout_path(settings, name)
    with tk.open_archive(zp) as a:
        kinds = {r["kind"] for r in a.registry()}
    assert "my_activity_youtube_json" in kinds


def test_registry_youtube_takeout_index(settings):
    name = _youtube_takeout_zip(settings.takeout_import_root, "yt.zip")
    zp = tk.resolve_takeout_path(settings, name)
    with tk.open_archive(zp) as a:
        kinds = {r["kind"] for r in a.registry()}
        assert any(k.startswith("youtube_watch_history") for k in kinds)
        assert "takeout_index" in kinds
        # YouTube Takeout has no liked source (likes live in My Activity)
        assert a.liked_source_path()[0] != "takeout_my_activity"


# --------------------------------------------------------------------------- #
# incremental import + sessions
# --------------------------------------------------------------------------- #
def test_incremental_dup_skip_and_session(settings):
    name = _myactivity_zip(settings.takeout_import_root, "inc.zip", n_liked=5)
    with session_scope() as s:
        r1 = tk.run_import_liked_videos(s, get_settings(), name)
        s.commit()
        assert r1["imported_count"] == 5 and r1["skipped_duplicate_count"] == 0
        assert r1["session_id"]
    # re-import -> all duplicates skipped
    with session_scope() as s:
        r2 = tk.run_import_liked_videos(s, get_settings(), name)
        s.commit()
        assert r2["imported_count"] == 0 and r2["skipped_duplicate_count"] == 5
    with session_scope() as s:
        sessions = tk.list_import_sessions(s)
        assert len(sessions) == 2
        latest = sessions[0]
        assert latest.import_kind == "liked_videos" and latest.skipped_duplicate == 5
        assert latest.path_basename == "inc.zip"
        # no full path / raw_json BLOB in the stored row (count fields like
        # raw_json_stored_count are fine; the personal blob key "raw_json" is not)
        assert "/" not in (latest.path_basename or "")
        assert '"raw_json":' not in json.dumps(latest.meta or {})


def test_updated_count_enriches_stub(settings):
    # Pre-create a bare Video stub (no title) for a liked id, then import.
    name = _myactivity_zip(settings.takeout_import_root, "upd.zip", n_liked=1, n_watch=0)
    with session_scope() as s:
        s.add(Video(youtube_video_id="liked000000", url="https://www.youtube.com/watch?v=liked000000",
                    first_seen_at=__import__("datetime").datetime(2025, 1, 1)))
        # also a liked row so the import sees it as a duplicate (skip + enrich)
        s.add(LikedVideo(source="takeout_my_activity", youtube_video_id="liked000000",
                         url="https://youtu.be/liked000000"))
        s.commit()
    with session_scope() as s:
        r = tk.run_import_liked_videos(s, get_settings(), name)
        s.commit()
        assert r["skipped_duplicate_count"] == 1
        assert r["updated_count"] == 1  # the bare stub got a title from the re-import
        v = s.query(Video).filter_by(youtube_video_id="liked000000").one()
        assert v.title  # enriched


def test_dry_run_writes_nothing(settings):
    name = _myactivity_zip(settings.takeout_import_root, "dry.zip", n_liked=4)
    with session_scope() as s:
        r = tk.run_import_liked_videos(s, get_settings(), name, dry_run=True)
        s.commit()
        assert r["imported_count"] == 4 and r["dry_run"] is True
        assert s.query(LikedVideo).count() == 0  # nothing written
        # a dry-run session IS still recorded (for history)
        sess = tk.list_import_sessions(s)
        assert len(sess) == 1 and sess[0].dry_run is True and sess[0].imported == 4


def test_import_all_one_combined_session(settings):
    name = _myactivity_zip(settings.takeout_import_root, "all.zip", n_liked=3, n_watch=2)
    with session_scope() as s:
        r = tk.run_import_all(s, get_settings(), name)
        s.commit()
        assert "session_id" in r
        sessions = tk.list_import_sessions(s)
        # import-all records exactly ONE "all" session (not one per kind)
        all_sessions = [x for x in sessions if x.import_kind == "all"]
        assert len(all_sessions) == 1
        assert all_sessions[0].imported >= 3  # liked + watch aggregated


def test_youtube_takeout_likes_zero(settings):
    name = _youtube_takeout_zip(settings.takeout_import_root, "ytz.zip")
    with session_scope() as s:
        r = tk.run_import_liked_videos(s, get_settings(), name)
        s.commit()
        assert r["imported_count"] == 0  # likes live in My Activity, not YouTube Takeout


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_inspect_deep_registry_api(client, settings):
    name = _myactivity_zip(settings.takeout_import_root, "api.zip")
    shallow = client.get(f"/api/takeout/inspect?path={name}").json()
    assert shallow["registry"] == []  # not requested
    deep = client.get(f"/api/takeout/inspect?path={name}&deep=true").json()
    assert any(r["kind"] == "my_activity_youtube_json" for r in deep["registry"])


def test_import_sessions_api(client, settings):
    name = _myactivity_zip(settings.takeout_import_root, "apisess.zip", n_liked=3)
    client.post("/api/takeout/import-liked-videos", json={"path": name})
    rows = client.get("/api/takeout/import-sessions").json()
    assert len(rows) == 1
    assert rows[0]["import_kind"] == "liked_videos" and rows[0]["imported"] == 3
    # detail by session_id
    sid = rows[0]["session_id"]
    assert client.get(f"/api/takeout/import-sessions/{sid}").json()["session_id"] == sid
    assert client.get("/api/takeout/import-sessions/nope").status_code == 404
    # no raw_json BLOB / absolute path leaked (count fields are allowed)
    blob = client.get("/api/takeout/import-sessions").text
    assert '"raw_json":' not in blob and "/takeout" not in blob.replace("import-sessions", "")


def test_import_search_history_api(client, settings):
    # search history isn't in this fixture, but the endpoint must work + record a session
    name = _myactivity_zip(settings.takeout_import_root, "srch.zip")
    r = client.post("/api/takeout/import-search-history", json={"path": name, "dry_run": True}).json()
    assert "imported_count" in r and r["dry_run"] is True
