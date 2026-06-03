"""Phase 3B tests: search/subscriptions/playlists parse + import + API/CLI."""

from __future__ import annotations

import json
import zipfile

from fastapi.testclient import TestClient
from typer.testing import CliRunner
import pytest

from app.cli import app as cli_app
from app.main import app
from app.models import Collection, CollectionItem, SearchHistoryEvent, Video
from app.services import takeout

runner = CliRunner()

WATCH2 = [
    {"header": "YouTube", "title": "W1 を視聴しました",
     "titleUrl": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
     "subtitles": [{"name": "C"}], "time": "2026-05-30T10:00:00.000Z"},
    {"header": "YouTube", "title": "W2 を視聴しました",
     "titleUrl": "https://www.youtube.com/watch?v=9bZkp7q19f0",
     "subtitles": [{"name": "C"}], "time": "2026-05-30T11:00:00.000Z"},
]
SEARCH3 = [
    {"header": "YouTube", "title": "nuphy を検索しました",
     "titleUrl": "https://www.youtube.com/results?search_query=nuphy", "time": "2026-06-01T09:00:00.000Z"},
    {"header": "YouTube", "title": "keyboard を検索しました",
     "titleUrl": "https://www.youtube.com/results?search_query=keyboard", "time": "2026-06-01T09:01:00.000Z"},
    {"header": "YouTube", "title": "Searched for lofree",
     "titleUrl": "https://www.youtube.com/results?search_query=lofree", "time": "2026-06-01T09:02:00.000Z"},
]
SUBS = [("UC11111111111111111111aa", "Chan A"), ("UC22222222222222222222bb", "Chan B")]
PLAYLISTS = {"My List": ["dQw4w9WgXcQ", "9bZkp7q19f0"], "Second": ["kJQP7kiw5Fk"]}


def _make_zip(path):
    base = "Takeout/YouTube and YouTube Music"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{base}/history/watch-history.json", json.dumps(WATCH2))
        z.writestr(f"{base}/history/search-history.json", json.dumps(SEARCH3))
        subs = "Channel Id,Channel Url,Channel Title\n" + "\n".join(
            f"{cid},http://www.youtube.com/channel/{cid},{title}" for cid, title in SUBS
        )
        z.writestr(f"{base}/subscriptions/subscriptions.csv", subs)
        idx = "Playlist Id,Title\n" + "\n".join(f"PL{n.replace(' ','')}id,{n}" for n in PLAYLISTS)
        z.writestr(f"{base}/playlists/playlists.csv", idx)
        for name, vids in PLAYLISTS.items():
            content = "Video Id,Timestamp\n" + "\n".join(
                f"{v},2026-05-30T10:00:00+00:00" for v in vids
            )
            z.writestr(f"{base}/playlists/{name}-videos.csv", content)


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _zip(settings, name="t.zip"):
    p = settings.takeout_import_root / name
    _make_zip(p)
    return name


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def test_parse_search():
    evs = list(takeout.parse_search_history_json(SEARCH3))
    assert evs[0].query == "nuphy"  # suffix stripped
    assert evs[2].query == "lofree"  # "Searched for " prefix stripped
    assert evs[0].searched_at is not None


def test_parse_subscriptions():
    text = "Channel Id,Channel Url,Channel Title\nUC11111111111111111111aa,http://x,Title A"
    subs = list(takeout.parse_subscriptions_csv(text))
    assert subs[0].channel_id == "UC11111111111111111111aa"
    assert subs[0].channel_title == "Title A"


def test_parse_playlist_index_and_items():
    idx = takeout.parse_playlist_index_csv("Playlist Id,Title\nPLabc,My List")
    assert idx["My List"] == "PLabc"
    items = list(takeout.parse_playlist_items_csv("Video Id,Time\ndQw4w9WgXcQ,2026\nbad,2026"))
    assert len(items) == 1 and items[0].youtube_video_id == "dQw4w9WgXcQ"


# --------------------------------------------------------------------------- #
# Import: search
# --------------------------------------------------------------------------- #
def test_import_search_and_dedup(session, settings):
    name = _zip(settings, "s.zip")
    r = takeout.run_import_search(session, settings, name)
    assert r["imported_count"] == 3
    r2 = takeout.run_import_search(session, settings, name)
    assert r2["imported_count"] == 0 and r2["skipped_duplicate_count"] == 3
    assert session.query(SearchHistoryEvent).count() == 3


# --------------------------------------------------------------------------- #
# Import: subscriptions
# --------------------------------------------------------------------------- #
def test_import_subscriptions_and_dedup(session, settings):
    name = _zip(settings, "sub.zip")
    r = takeout.run_import_subscriptions(session, settings, name)
    assert r["imported_count"] == 2
    r2 = takeout.run_import_subscriptions(session, settings, name)
    assert r2["imported_count"] == 0 and r2["skipped_duplicate_count"] == 2
    chans = session.query(Collection).filter_by(type="channel").all()
    assert len(chans) == 2
    assert all(c.enabled is False and c.crawl_policy == "manual" for c in chans)


# --------------------------------------------------------------------------- #
# Import: playlists
# --------------------------------------------------------------------------- #
def test_import_playlists_creates_collections_items_videos(session, settings):
    name = _zip(settings, "pl.zip")
    r = takeout.run_import_playlists(session, settings, name)
    assert r["playlists_imported"] == 2
    assert r["items_imported"] == 3
    assert r["videos_created"] == 3
    assert session.query(Collection).filter_by(type="takeout_playlist").count() == 2
    assert session.query(CollectionItem).count() == 3
    assert session.query(Video).count() == 3
    # items linked to video stubs
    assert all(ci.video_id is not None for ci in session.query(CollectionItem).all())
    # re-import dedups items
    r2 = takeout.run_import_playlists(session, settings, name)
    assert r2["items_imported"] == 0 and r2["items_skipped"] == 3


def test_import_playlists_limit_items(session, settings):
    name = _zip(settings, "pli.zip")
    r = takeout.run_import_playlists(session, settings, name, limit_items=1)
    assert r["items_imported"] == 2  # 1 per playlist x 2 playlists


def test_import_playlists_dry_run(session, settings):
    name = _zip(settings, "pld.zip")
    r = takeout.run_import_playlists(session, settings, name, dry_run=True)
    assert r["playlists_imported"] == 2 and r["job_id"] is None
    assert session.query(Collection).filter_by(type="takeout_playlist").count() == 0


# --------------------------------------------------------------------------- #
# import-all
# --------------------------------------------------------------------------- #
def test_import_all_with_limits(session, settings):
    name = _zip(settings, "all.zip")
    r = takeout.run_import_all(
        session, settings, name,
        limit_watch=1, limit_search=2, limit_subscriptions=1, limit_playlists=1, limit_items=1,
    )
    assert r["watch_history"]["imported_count"] == 1
    assert r["search_history"]["imported_count"] == 2
    assert r["subscriptions"]["imported_count"] == 1
    assert r["playlists"]["playlists_imported"] == 1
    assert r["playlists"]["items_imported"] == 1


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_api_import_subscriptions_and_list(client, settings):
    name = _zip(settings, "apisub.zip")
    r = client.post("/api/takeout/import-subscriptions", json={"path": name})
    assert r.status_code == 200 and r.json()["imported_count"] == 2
    subs = client.get("/api/subscriptions").json()
    assert len(subs) == 2 and subs[0]["channel_id"].startswith("UC")


def test_api_import_playlists_and_preview(client, settings):
    name = _zip(settings, "applay.zip")
    r = client.post("/api/takeout/import-playlists", json={"path": name, "limit_items": 1})
    assert r.status_code == 200 and r.json()["playlists_imported"] == 2 and r.json()["items_imported"] == 2
    pv = client.get("/api/takeout/playlists/preview", params={"path": name}).json()
    assert len(pv["playlists"]) == 2


def test_api_search_history_raw_default_null(client, settings):
    name = _zip(settings, "sh.zip")
    client.post("/api/takeout/import-all", json={"path": name})
    lst = client.get("/api/search-history").json()
    assert len(lst) == 3 and all(e["raw_json"] is None for e in lst)
    raw = client.get("/api/search-history", params={"include_raw": True, "limit": 1}).json()
    assert raw[0]["raw_json"] is not None
    assert client.get("/api/search-history/stats").json()["total"] == 3


def test_api_subscriptions_enqueue(client, settings):
    name = _zip(settings, "enq.zip")
    client.post("/api/takeout/import-subscriptions", json={"path": name})
    r = client.post(
        "/api/subscriptions/enqueue",
        json={"videos": True, "profile": "metadata_only", "limit": 1},
    )
    assert r.status_code == 200 and r.json()["jobs_created"] == 1
    assert client.post("/api/subscriptions/enqueue", json={}).status_code == 400


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_phase3b(settings):
    name = _zip(settings, "cli3b.zip")
    assert runner.invoke(cli_app, ["takeout", "import-subscriptions", name]).exit_code == 0
    assert runner.invoke(cli_app, ["takeout", "import-playlists", name, "--limit-items", "1"]).exit_code == 0
    r = runner.invoke(cli_app, ["takeout", "import-all", name, "--limit-search", "2"])
    assert r.exit_code == 0
    assert "total" in runner.invoke(cli_app, ["search-history", "stats"]).stdout
    assert runner.invoke(cli_app, ["subscriptions", "list"]).exit_code == 0
    assert runner.invoke(cli_app, ["takeout", "playlists", name]).exit_code == 0
