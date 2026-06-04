"""Phase 6A tests: Takeout liked-videos parser/import + library/search/API."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import Job, LikedVideo, Video
from app.services import takeout as tk


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _write_zip(settings, name: str, members: dict[str, str]) -> str:
    """Write a Takeout-style zip under TAKEOUT_IMPORT_ROOT; return its rel path."""
    root = settings.takeout_import_root
    root.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for member, content in members.items():
            z.writestr(member, content)
    (root / name).write_bytes(buf.getvalue())
    return name


_LIKED_EN = (
    "Video ID,Playlist Video Creation Timestamp\n"
    "dQw4w9WgXcQ,2023-05-01T12:00:00+00:00\n"
    "9bZkp7q19f0,2022-01-02T08:30:00+00:00\n"
)
_LIKED_JA = "Takeout/YouTube and YouTube Music/playlists/高く評価した動画.csv"


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def test_parser_classifies_and_parses_liked_csv(settings):
    rel = _write_zip(settings, "t1.zip", {_LIKED_JA: _LIKED_EN})
    path = tk.resolve_takeout_path(settings, rel)
    with tk.open_archive(path) as a:
        kinds = {f.kind for f in a.list_files()}
        assert "likes" in kinds
        liked = list(a.iter_liked_videos())
        assert [lv.youtube_video_id for lv in liked] == ["dQw4w9WgXcQ", "9bZkp7q19f0"]
        assert liked[0].liked_at is not None
        assert liked[0].url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        pv = a.preview()
        assert pv["likes_count"] == 2
        assert pv["importable"]["liked_videos"] is True
        assert len(pv["liked_samples"]) == 2


# --------------------------------------------------------------------------- #
# Import: stubs, dedup, dry-run
# --------------------------------------------------------------------------- #
def test_import_liked_creates_stubs_and_dedupes(settings):
    rel = _write_zip(settings, "t2.zip", {_LIKED_JA: _LIKED_EN})
    s_path = str(settings.takeout_import_root / rel)
    with session_scope() as s:
        r1 = tk.run_import_liked_videos(s, settings, s_path)
        assert r1["imported_count"] == 2
        assert r1["videos_created"] == 2
        # Video stubs created + linked
        lvs = s.query(LikedVideo).all()
        assert len(lvs) == 2
        assert all(lv.video_id is not None for lv in lvs)
        assert s.query(Video).filter_by(youtube_video_id="dQw4w9WgXcQ").count() == 1
    with session_scope() as s:
        r2 = tk.run_import_liked_videos(s, settings, s_path)
        assert r2["imported_count"] == 0 and r2["skipped_duplicate_count"] == 2


def test_import_liked_dry_run_writes_nothing(settings):
    rel = _write_zip(settings, "t3.zip", {_LIKED_JA: _LIKED_EN})
    s_path = str(settings.takeout_import_root / rel)
    with session_scope() as s:
        r = tk.run_import_liked_videos(s, settings, s_path, dry_run=True)
        assert r["imported_count"] == 2 and r["dry_run"] is True
        assert s.query(LikedVideo).count() == 0


def test_import_all_includes_liked(settings):
    rel = _write_zip(settings, "t4.zip", {_LIKED_JA: _LIKED_EN})
    s_path = str(settings.takeout_import_root / rel)
    with session_scope() as s:
        r = tk.run_import_all(s, settings, s_path)
        assert "liked_videos" in r
        assert r["liked_videos"]["imported_count"] == 2


# --------------------------------------------------------------------------- #
# API: list / stats / enqueue / library / search
# --------------------------------------------------------------------------- #
def test_api_liked_list_stats_and_raw_hidden(client, settings):
    rel = _write_zip(settings, "t5.zip", {_LIKED_JA: _LIKED_EN})
    assert client.post("/api/takeout/import-liked-videos", json={"path": rel}).status_code == 200

    rows = client.get("/api/liked-videos").json()
    assert len(rows) == 2
    assert all(r["raw_json"] is None for r in rows)  # privacy: hidden by default
    assert all(r["metadata_fetched"] is False for r in rows)  # stubs, no title yet
    raw = client.get("/api/liked-videos?include_raw=true").json()
    assert any(r["raw_json"] is not None for r in raw)

    stats = client.get("/api/liked-videos/stats").json()
    assert stats["total"] == 2 and stats["with_video_id"] == 2 and stats["metadata_fetched"] == 0

    lib = {c["key"]: c for c in client.get("/api/library/summary").json()["categories"]}
    assert lib["liked_videos"]["available"] is True and lib["liked_videos"]["count"] == 2

    hits = client.get("/api/search?q=dQw4&types=liked_video").json()["results"]
    assert any(h["type"] == "liked_video" and h["youtube_video_id"] == "dQw4w9WgXcQ" for h in hits)


def test_api_enqueue_metadata_uses_metadata_only_no_body(client, settings):
    rel = _write_zip(settings, "t6.zip", {_LIKED_JA: _LIKED_EN})
    client.post("/api/takeout/import-liked-videos", json={"path": rel})
    r = client.post("/api/liked-videos/enqueue-metadata", json={"limit": 5}).json()
    assert r["jobs_created"] == 2 and r["videos_selected"] == 2
    with session_scope() as s:
        jobs = s.query(Job).filter(Job.id.in_(r["job_ids"])).all()
        assert jobs and all(j.profile_name == "metadata_only" for j in jobs)
        assert all(j.type == "download" for j in jobs)  # download+metadata_only => skip-download
