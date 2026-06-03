"""Phase 3A tests: Takeout preview/import, dedup, limit, dry-run, traversal,
watch-history API/CLI."""

from __future__ import annotations

import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.main import app
from app.models import WatchHistoryEvent
from app.services import takeout

runner = CliRunner()

WATCH3 = [
    {
        "header": "YouTube",
        "title": "Title One を視聴しました",
        "titleUrl": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "subtitles": [{"name": "Chan A", "url": "u"}],
        "time": "2026-05-30T10:00:00.000Z",
    },
    {
        "header": "YouTube",
        "title": "Title Two を視聴しました",
        "titleUrl": "https://www.youtube.com/watch?v=9bZkp7q19f0",
        "subtitles": [{"name": "Chan B"}],
        "time": "2026-05-31T11:00:00.000Z",
    },
    {
        "header": "YouTube",
        "title": "https://www.youtube.com/watch?v=kJQP7kiw5Fk を視聴しました",
        "titleUrl": "https://www.youtube.com/watch?v=kJQP7kiw5Fk",
        "time": "2026-06-01T12:00:00.000Z",
    },
]
SEARCH2 = [
    {
        "header": "YouTube",
        "title": "nuphy を検索しました",
        "titleUrl": "https://www.youtube.com/results?search_query=nuphy",
        "time": "2026-06-01T09:00:00.000Z",
    },
    {
        "header": "YouTube",
        "title": "keyboard を検索しました",
        "titleUrl": "https://www.youtube.com/results?search_query=keyboard",
        "time": "2026-06-01T09:01:00.000Z",
    },
]


def _watch_html(entries) -> str:
    blocks = []
    for e in entries:
        url = e["titleUrl"]
        title = e["title"].replace(" を視聴しました", "")
        ch = (e.get("subtitles") or [{}])[0].get("name", "")
        blocks.append(
            f'<div class="outer-cell"><div class="content-cell">Watched '
            f'<a href="{url}">{title}</a><br>'
            f'<a href="https://www.youtube.com/channel/UCx">{ch}</a><br>'
            f"2026/05/30 10:00:00 JST</div></div>"
        )
    return "<html><body>" + "".join(blocks) + "</body></html>"


def _make_zip(path, *, watch=None, search=None, subs_rows=2, html=False) -> None:
    base = "Takeout/YouTube and YouTube Music"
    with zipfile.ZipFile(path, "w") as z:
        if watch is not None:
            if html:
                z.writestr(f"{base}/history/watch-history.html", _watch_html(watch))
            else:
                z.writestr(f"{base}/history/watch-history.json", json.dumps(watch))
        if search is not None:
            z.writestr(f"{base}/history/search-history.json", json.dumps(search))
        rows = "Channel Id,Channel Url,Channel Title\n" + "\n".join(
            f"UC{i},url,Chan{i}" for i in range(subs_rows)
        )
        z.writestr(f"{base}/subscriptions/subscriptions.csv", rows)
        z.writestr(f"{base}/playlists/My List-videos.csv", "Video Id,Time\nabc,2026")


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def test_parse_watch_history_json():
    evs = list(takeout.parse_watch_history_json(WATCH3))
    assert [e.youtube_video_id for e in evs] == ["dQw4w9WgXcQ", "9bZkp7q19f0", "kJQP7kiw5Fk"]
    assert evs[0].channel_title == "Chan A"
    assert evs[0].watched_at is not None
    assert "を視聴しました" not in (evs[0].title or "")  # suffix stripped


def test_parse_watch_history_html():
    evs = list(takeout.parse_watch_history_html(_watch_html(WATCH3)))
    assert len(evs) == 3
    assert evs[0].youtube_video_id == "dQw4w9WgXcQ"
    assert evs[0].watched_at is not None


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
def test_preview_counts(settings):
    zp = settings.takeout_import_root / "t.zip"
    _make_zip(zp, watch=WATCH3, search=SEARCH2)
    with takeout.open_archive(zp) as a:
        pv = a.preview()
    assert pv["watch_history_count"] == 3
    assert pv["search_history_count"] == 2
    assert pv["subscriptions_count"] == 2
    assert pv["playlists_count"] == 1
    assert pv["samples"][0]["youtube_video_id"] == "dQw4w9WgXcQ"


def test_preview_html(settings):
    zp = settings.takeout_import_root / "th.zip"
    _make_zip(zp, watch=WATCH3, html=True)
    with takeout.open_archive(zp) as a:
        pv = a.preview()
    assert pv["watch_history_count"] == 3


# --------------------------------------------------------------------------- #
# Import / dedup / limit / dry-run
# --------------------------------------------------------------------------- #
def test_import_and_dedup(session, settings):
    zp = settings.takeout_import_root / "imp.zip"
    _make_zip(zp, watch=WATCH3)
    r = takeout.run_import(session, settings, "imp.zip")
    assert r["imported_count"] == 3 and r["skipped_duplicate_count"] == 0
    assert r["job_id"] is not None
    r2 = takeout.run_import(session, settings, "imp.zip")
    assert r2["imported_count"] == 0 and r2["skipped_duplicate_count"] == 3
    assert session.query(WatchHistoryEvent).count() == 3
    # job recorded with counts in meta
    from app.models import Job

    job = session.get(Job, r["job_id"])
    assert job.type == "takeout_import" and job.status == "success"
    assert job.meta["imported_count"] == 3


def test_import_limit(session, settings):
    zp = settings.takeout_import_root / "lim.zip"
    _make_zip(zp, watch=WATCH3)
    r = takeout.run_import(session, settings, "lim.zip", limit=2)
    assert r["scanned"] == 2 and r["imported_count"] == 2
    assert session.query(WatchHistoryEvent).count() == 2


def test_import_dry_run_writes_nothing(session, settings):
    zp = settings.takeout_import_root / "dry.zip"
    _make_zip(zp, watch=WATCH3)
    r = takeout.run_import(session, settings, "dry.zip", dry_run=True)
    assert r["imported_count"] == 3 and r["dry_run"] is True and r["job_id"] is None
    assert session.query(WatchHistoryEvent).count() == 0


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #
def test_path_traversal_rejected(settings, tmp_path):
    outside = tmp_path / "evil.zip"
    _make_zip(outside, watch=WATCH3)
    with pytest.raises(takeout.TakeoutError):
        takeout.resolve_takeout_path(settings, str(outside))
    with pytest.raises(takeout.TakeoutError):
        takeout.resolve_takeout_path(settings, "../../etc/passwd.zip")


def test_resolve_rejects_non_zip(settings):
    (settings.takeout_import_root / "notzip.txt").write_text("x")
    with pytest.raises(takeout.TakeoutError):
        takeout.resolve_takeout_path(settings, "notzip.txt")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_takeout_preview_api(client, settings):
    _make_zip(settings.takeout_import_root / "pv.zip", watch=WATCH3, search=SEARCH2)
    r = client.post("/api/takeout/preview", json={"path": "pv.zip"})
    assert r.status_code == 200
    assert r.json()["watch_history_count"] == 3


def test_takeout_import_api_and_watch_history(client, settings):
    _make_zip(settings.takeout_import_root / "api.zip", watch=WATCH3)
    r = client.post("/api/takeout/import", json={"path": "api.zip"})
    assert r.status_code == 200 and r.json()["imported_count"] == 3

    listing = client.get("/api/watch-history?limit=10").json()
    assert len(listing) == 3
    assert all(e["raw_json"] is None for e in listing)  # raw excluded by default

    raw = client.get("/api/watch-history?limit=1&include_raw=true").json()
    assert raw[0]["raw_json"] is not None

    stats = client.get("/api/watch-history/stats").json()
    assert stats["total"] == 3 and stats["with_video_id"] == 3
    assert stats["distinct_videos"] == 3


def test_takeout_import_api_bad_path_400(client):
    r = client.post("/api/takeout/import", json={"path": "../../etc/x.zip"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_takeout_and_watch_history(settings):
    _make_zip(settings.takeout_import_root / "cli.zip", watch=WATCH3)
    res = runner.invoke(cli_app, ["takeout", "preview", "cli.zip"])
    assert res.exit_code == 0 and "watch_history=3" in res.stdout
    res2 = runner.invoke(cli_app, ["takeout", "import", "cli.zip", "--limit", "2"])
    assert res2.exit_code == 0 and "imported=2" in res2.stdout
    res3 = runner.invoke(cli_app, ["watch-history", "list", "--limit", "5"])
    assert res3.exit_code == 0
    res4 = runner.invoke(cli_app, ["watch-history", "stats"])
    assert res4.exit_code == 0 and "total" in res4.stdout
