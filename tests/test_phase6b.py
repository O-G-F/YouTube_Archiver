"""Phase 6B tests: My Activity liked parser, kind detection, hybrid bootstrap,
YouTube Data API differential sync (mocked), OAuth-off safety."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import LikedVideo, Video
from app.services import library as library_svc
from app.services import takeout as tk
from app.services import youtube_api


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _zip(settings, name: str, members: dict[str, str]) -> str:
    root = settings.takeout_import_root
    root.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for member, content in members.items():
            z.writestr(member, content)
    (root / name).write_bytes(buf.getvalue())
    return name


def _activity(title, vid, *, ch="Chan", ch_url="https://www.youtube.com/channel/UC0000000000000000000001"):
    return {
        "header": "YouTube",
        "title": title,
        "titleUrl": f"https://www.youtube.com/watch?v={vid}" if vid else "",
        "time": "2023-05-01T12:00:00.000Z",
        "subtitles": [{"name": ch, "url": ch_url}],
    }


# My Activity export with the 4 required marker cases.
_MA_ITEMS = [
    _activity("高く評価しました 三日月ステップ", "AAAAAAAAAAA"),
    _activity("Liked Some English Song", "BBBBBBBBBBB"),
    _activity("高評価を削除しました 何か", "CCCCCCCCCCC"),       # excluded
    _activity("テスト動画 を視聴しました", "DDDDDDDDDDD"),        # excluded
    _activity("Disliked Something", "EEEEEEEEEEE"),              # excluded
]
_MA_PATH = "Takeout/マイ アクティビティ/YouTube/マイアクティビティ.json"
_YT_LIKED = "Takeout/YouTube と YouTube Music/playlists/Liked videos.csv"


# --------------------------------------------------------------------------- #
# My Activity parser + markers
# --------------------------------------------------------------------------- #
def test_myactivity_marker_parsing():
    liked = list(tk.parse_myactivity_liked_json(json.dumps(_MA_ITEMS)))
    ids = [lv.youtube_video_id for lv in liked]
    assert ids == ["AAAAAAAAAAA", "BBBBBBBBBBB"]  # only the 2 like events
    a = liked[0]
    assert a.title == "三日月ステップ"  # prefix stripped
    assert a.channel_title == "Chan"
    assert a.channel_id == "UC0000000000000000000001"  # from subtitles[0].url
    assert a.source == "takeout_my_activity"
    assert liked[1].title == "Some English Song"  # "Liked " stripped


def test_is_like_activity_helpers():
    assert tk._is_like_activity("高く評価しました X") is True
    assert tk._is_like_activity("Liked Y") is True
    assert tk._is_like_activity("高評価を削除しました Z") is False
    assert tk._is_like_activity("W を視聴しました") is False
    assert tk._is_like_activity("Disliked V") is False


# --------------------------------------------------------------------------- #
# Archive kind detection
# --------------------------------------------------------------------------- #
def test_archive_kind_detection(settings):
    ma = _zip(settings, "ma.zip", {_MA_PATH: json.dumps(_MA_ITEMS)})
    yt = _zip(settings, "yt.zip", {_YT_LIKED: "Video ID,Timestamp\ndQw4w9WgXcQ,2023-01-01T00:00:00Z\n"})
    idx = _zip(settings, "idx.zip", {"Takeout/archive_browser.html": "<html>index</html>"})
    unk = _zip(settings, "unk.zip", {"Takeout/Other/file.txt": "x"})
    kinds = {}
    for n in (ma, yt, idx, unk):
        with tk.open_archive(tk.resolve_takeout_path(settings, n)) as a:
            kinds[n] = a.archive_kind()
    assert kinds["ma.zip"] == "my_activity_takeout"
    assert kinds["yt.zip"] == "youtube_takeout"
    assert kinds["idx.zip"] == "takeout_index"
    assert kinds["unk.zip"] == "unknown_takeout"


def test_discover_and_inspect_api(client, settings):
    _zip(settings, "ma.zip", {_MA_PATH: json.dumps(_MA_ITEMS)})
    _zip(settings, "idx.zip", {"Takeout/archive_browser.html": "<html></html>"})
    body = client.get("/api/takeout/discover").json()
    kinds = {a["name"]: a["archive_kind"] for a in body["archives"]}
    assert kinds["ma.zip"] == "my_activity_takeout"
    assert kinds["idx.zip"] == "takeout_index"
    insp = client.get("/api/takeout/inspect?path=ma.zip").json()
    assert insp["archive_kind"] == "my_activity_takeout"
    assert insp["my_activity_youtube_path"].endswith("マイアクティビティ.json")


# --------------------------------------------------------------------------- #
# Import: My Activity, dedup, dry-run, YouTube likes=0
# --------------------------------------------------------------------------- #
def test_import_myactivity_liked_with_stub_enrichment(settings):
    name = _zip(settings, "ma.zip", {_MA_PATH: json.dumps(_MA_ITEMS)})
    s_path = str(settings.takeout_import_root / name)
    with session_scope() as s:
        r = tk.run_import_liked_videos(s, settings, s_path)
        assert r["imported_count"] == 2 and r["videos_created"] == 2
        assert r["source_kind"] == "takeout_my_activity"
        assert r["detected_path"].endswith("マイアクティビティ.json")
        lv = s.query(LikedVideo).filter_by(youtube_video_id="AAAAAAAAAAA").one()
        assert lv.source == "takeout_my_activity"
        v = s.get(Video, lv.video_id)
        assert v.title == "三日月ステップ" and v.channel_id == "UC0000000000000000000001"
    # re-import dedupes (cross-source, by video id)
    with session_scope() as s:
        r2 = tk.run_import_liked_videos(s, settings, s_path)
        assert r2["imported_count"] == 0 and r2["skipped_duplicate_count"] == 2


def test_import_liked_dry_run_and_limit(settings):
    name = _zip(settings, "ma.zip", {_MA_PATH: json.dumps(_MA_ITEMS)})
    s_path = str(settings.takeout_import_root / name)
    with session_scope() as s:
        r = tk.run_import_liked_videos(s, settings, s_path, limit=1, dry_run=True)
        assert r["imported_count"] == 1 and r["scanned"] == 1 and r["dry_run"] is True
        assert s.query(LikedVideo).count() == 0


def test_youtube_takeout_likes_zero_is_fine(settings):
    name = _zip(settings, "yt.zip", {_YT_LIKED: "Video ID,Timestamp\n"})  # header only
    s_path = str(settings.takeout_import_root / name)
    with session_scope() as s:
        r = tk.run_import_liked_videos(s, settings, s_path)
        assert r["imported_count"] == 0 and r["source_kind"] == "takeout_youtube"


# --------------------------------------------------------------------------- #
# Hybrid bootstrap (dry-run)
# --------------------------------------------------------------------------- #
def test_library_bootstrap_dry_run(settings):
    ma = _zip(settings, "ma.zip", {_MA_PATH: json.dumps(_MA_ITEMS)})
    yt = _zip(
        settings, "yt.zip",
        {
            _YT_LIKED: "Video ID,Timestamp\n",
            "Takeout/YouTube と YouTube Music/登録チャンネル/登録チャンネル.csv":
                "Channel Id,Channel Url,Channel Title\nUC0000000000000000000009,http://x,Chan\n",
        },
    )
    with session_scope() as s:
        r = library_svc.bootstrap(
            s, settings, youtube_takeout_path=yt, myactivity_takeout_path=ma,
            limit_liked=10, dry_run=True,
        )
        assert r["dry_run"] is True
        assert r["myactivity_takeout"]["liked_videos"]["imported_count"] == 2
        assert r["youtube_takeout"]["subscriptions"]["imported_count"] == 1
        assert s.query(LikedVideo).count() == 0  # dry-run writes nothing


# --------------------------------------------------------------------------- #
# YouTube Data API differential sync (mocked fetcher)
# --------------------------------------------------------------------------- #
def test_api_sync_liked_dedup_and_stop_on_existing(settings):
    # Seed one liked video from Takeout; the API walk should stop when it reaches it.
    with session_scope() as s:
        s.add(LikedVideo(source="takeout_my_activity", youtube_video_id="EXISTINGvid"))
        s.flush()

    def fake_fetcher():
        yield {"video_id": "NEWvideo001", "title": "New One", "channel_title": "C1",
               "channel_id": "UCx", "published_at": "2023-05-01T00:00:00Z", "thumbnail_url": "http://t/1"}
        yield {"video_id": "NEWvideo002", "title": "New Two", "channel_title": "C2",
               "channel_id": "UCy", "published_at": "2022-01-02T00:00:00Z", "thumbnail_url": "http://t/2"}
        yield {"video_id": "EXISTINGvid", "title": "Already", "channel_title": "C3"}  # -> stop here
        yield {"video_id": "NEVERvideo3", "title": "Unreached"}

    with session_scope() as s:
        r = youtube_api.sync_liked(s, settings, fetcher=fake_fetcher, stop_on_existing=True)
        assert r["imported_count"] == 2 and r["stopped_on_existing"] is True
        assert r["source"] == "youtube_data_api"
        new = s.query(LikedVideo).filter_by(youtube_video_id="NEWvideo001").one()
        assert new.source == "youtube_data_api"
        v = s.get(Video, new.video_id)
        assert v.title == "New One" and v.channel_id == "UCx" and v.upload_date == "20230501"
        assert s.query(LikedVideo).filter_by(youtube_video_id="NEVERvideo3").count() == 0


# --------------------------------------------------------------------------- #
# OAuth-not-configured safety
# --------------------------------------------------------------------------- #
def test_oauth_off_status_and_sync_safe(client, settings):
    st = client.get("/api/youtube-api/status").json()
    assert st["enabled"] is False and st["configured"] is False
    # sync returns 200 with ok=false + classification (no crash)
    resp = client.post("/api/youtube-api/sync-liked", json={"limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and body["classification"] == "auth_required"


def test_sync_liked_raises_when_unconfigured(settings):
    with session_scope() as s:
        with pytest.raises(youtube_api.YouTubeApiError) as ei:
            youtube_api.sync_liked(s, settings)  # no fetcher, not configured
        assert ei.value.classification == "auth_required"
