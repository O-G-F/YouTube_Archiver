"""Phase 5A tests: admin-UI support APIs + log secret masking.

All run on the SQLite fixture DB via the FastAPI TestClient (no Redis).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import Collection, CollectionItem, Job, MediaFile, Video
from app.services import logs as logs_svc


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _mk_video(s, vid="dQw4w9WgXcQ", **kw):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}", title="T", **kw)
    s.add(v)
    s.flush()
    return v


# --------------------------------------------------------------------------- #
# Dashboard / job-stats
# --------------------------------------------------------------------------- #
def test_dashboard_shape(client):
    with session_scope() as s:
        _mk_video(s, vid="vid0000001a")
        s.add(Job(type="download", status="success", url="u"))
        s.add(Job(type="comments_refresh", status="failed", url="u2"))
    body = client.get("/api/dashboard").json()
    assert set(body) == {"health", "job_stats", "counts", "scheduler", "latest_jobs"}
    assert body["counts"]["videos"] >= 1
    assert body["job_stats"]["total"] >= 2
    assert body["job_stats"]["by_status"].get("failed", 0) >= 1
    assert "comments_due" in body["counts"]
    assert body["scheduler"]["comments_enabled"] in (True, False)
    assert len(body["latest_jobs"]) >= 2


def test_job_stats(client):
    with session_scope() as s:
        s.add(Job(type="download", status="queued", url="u"))
        s.add(Job(type="download", status="queued", url="u"))
    body = client.get("/api/job-stats").json()
    assert body["by_status"]["queued"] >= 2
    assert body["by_type"]["download"] >= 2


# --------------------------------------------------------------------------- #
# Videos list / detail / filters / related
# --------------------------------------------------------------------------- #
def test_videos_list_fields_and_media_count(client):
    with session_scope() as s:
        v = _mk_video(s, vid="withmedia01", comments_state=None, live_chat_state="available")
        s.add(MediaFile(video_id=v.id, media_type="video", path="youtube/videos/x/withmedia01/v.mkv"))
        _mk_video(s, vid="nomedia0001", live_chat_state="not_available")
    rows = client.get("/api/videos").json()
    by_id = {r["youtube_video_id"]: r for r in rows}
    assert by_id["withmedia01"]["media_files_count"] == 1
    assert by_id["withmedia01"]["live_chat_state"] == "available"
    assert by_id["nomedia0001"]["media_files_count"] == 0
    # new state fields present
    assert "next_comments_refresh_at" in by_id["withmedia01"]


def test_videos_filters(client):
    with session_scope() as s:
        v = _mk_video(s, vid="hasmedia002")
        s.add(MediaFile(video_id=v.id, media_type="video", path="a/b.mkv"))
        _mk_video(s, vid="frozen00002", comments_state="frozen")
    assert all(
        r["media_files_count"] > 0 for r in client.get("/api/videos?has_media=true").json()
    )
    assert all(
        r["media_files_count"] == 0 for r in client.get("/api/videos?has_media=false").json()
    )
    frozen = client.get("/api/videos?comments_state=frozen").json()
    assert all(r["comments_state"] == "frozen" for r in frozen)
    assert any(r["youtube_video_id"] == "frozen00002" for r in frozen)


def test_video_detail_and_related(client):
    with session_scope() as s:
        v = _mk_video(s, vid="detail00001")
        coll = Collection(type="playlist", url="https://www.youtube.com/playlist?list=PLx", title="PL")
        s.add(coll)
        s.flush()
        s.add(CollectionItem(collection_id=coll.id, video_id=v.id, youtube_video_id=v.youtube_video_id))
        s.add(Job(type="comments_refresh", status="success", url="u", video_id=v.id))
        vid_pk = v.id
    detail = client.get(f"/api/videos/{vid_pk}").json()
    assert detail["youtube_video_id"] == "detail00001"
    assert "live_chat_state" in detail and "media_files" in detail
    jobs = client.get(f"/api/videos/{vid_pk}/jobs").json()
    assert len(jobs) == 1 and jobs[0]["type"] == "comments_refresh"
    colls = client.get(f"/api/videos/{vid_pk}/collections").json()
    assert len(colls) == 1 and colls[0]["title"] == "PL"


def test_video_media_streaming_and_guard(client, settings, tmp_path):
    # real file on disk under ARCHIVE_ROOT
    rel = "youtube/videos/c/streamtest1/clip.mp4"
    abs_path = settings.archive_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"\x00\x01\x02fake-mp4")
    with session_scope() as s:
        v = _mk_video(s, vid="streamtest1")
        mf = MediaFile(video_id=v.id, media_type="video", path=rel, container="mp4")
        s.add(mf)
        s.flush()
        vid_pk, mf_id = v.id, mf.id
        other = _mk_video(s, vid="otherone001")
        other_pk = other.id
    r = client.get(f"/api/videos/{vid_pk}/media/{mf_id}")
    assert r.status_code == 200
    assert r.content == b"\x00\x01\x02fake-mp4"
    # media file does not belong to this video -> 404
    assert client.get(f"/api/videos/{other_pk}/media/{mf_id}").status_code == 404
    # unknown media id -> 404
    assert client.get(f"/api/videos/{vid_pk}/media/999999").status_code == 404


# --------------------------------------------------------------------------- #
# Takeout files (path restriction)
# --------------------------------------------------------------------------- #
def test_takeout_files_only_lists_within_root(client, settings, tmp_path):
    root = settings.takeout_import_root
    root.mkdir(parents=True, exist_ok=True)
    (root / "takeout-good.zip").write_bytes(b"PK\x03\x04zip")
    (root / "notes.txt").write_text("not a zip")  # must be ignored (only .zip)
    # a zip OUTSIDE the root must never appear
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"PK\x03\x04")
    body = client.get("/api/takeout/files").json()
    names = [f["name"] for f in body["files"]]
    assert "takeout-good.zip" in names
    assert "notes.txt" not in names
    assert all("outside" not in n for n in names)
    assert body["root"].endswith("takeout")


# --------------------------------------------------------------------------- #
# Settings (no secret leakage)
# --------------------------------------------------------------------------- #
def test_settings_hides_secrets(client, monkeypatch):
    body = client.get("/api/settings").json()
    blob = str(body)
    keys = {i["key"]: i["value"] for i in body["items"]}
    # cookie path must never be present; only a boolean indicator
    assert "cookies_configured" in keys
    assert "cookies_file" not in keys
    assert "/secrets/" not in blob
    # profiles included
    assert any(p["name"] == "comments_refresh_only" for p in body["profiles"])


def test_settings_masks_connection_url_password():
    from app.api.settings import _mask_url

    assert _mask_url("postgresql+psycopg2://archiver:secretpw@postgres:5432/db") == (
        "postgresql+psycopg2://archiver:***@postgres:5432/db"
    )
    assert _mask_url("redis://:mypassword@redis:6379/0") == "redis://:***@redis:6379/0"
    # sqlite has no credentials -> unchanged
    assert _mask_url("sqlite:////data/app.db") == "sqlite:////data/app.db"


# --------------------------------------------------------------------------- #
# Log secret masking
# --------------------------------------------------------------------------- #
def test_mask_secrets_redacts_cookies_tokens_and_paths():
    from app.config import get_settings

    s = get_settings()
    text = (
        "yt-dlp --cookies /secrets/cookies.txt --remote-components ejs:github\n"
        "Authorization: Bearer abc.def.ghi\n"
        "password=hunter2 and token: deadbeefcafebabe\n"
        f"loaded cookie file {s.cookies_file}\n"
        "normal log line stays intact"
    )
    out = logs_svc.mask_secrets(text, s)
    assert "/secrets/cookies.txt" not in out
    assert "hunter2" not in out
    assert "deadbeefcafebabe" not in out
    assert "abc.def.ghi" not in out
    assert "ejs:github" in out  # not a secret -> preserved
    assert "normal log line stays intact" in out
    assert logs_svc.MASK in out


def test_read_log_is_masked(client, settings):
    # craft a job whose command.txt contains a cookie path, then read via API.
    log_dir = settings.log_root / "jobs" / "9001"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "command.txt").write_text("yt-dlp --cookies /secrets/cookies.txt 'https://x'")
    (log_dir / "yt-dlp.stdout.log").write_text("ok")
    (log_dir / "yt-dlp.stderr.log").write_text("")
    with session_scope() as s:
        job = Job(type="download", status="success", url="u", log_path="jobs/9001")
        s.add(job)
        s.flush()
        jid = job.id
    body = client.get(f"/api/jobs/{jid}/logs").json()
    assert "/secrets/cookies.txt" not in body["command"]
    assert logs_svc.MASK in body["command"]
