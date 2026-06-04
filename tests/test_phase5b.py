"""Phase 5B tests: Range streaming, thumbnail, related, search, job classify."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import app
from app.models import Collection, CollectionItem, Comment, Job, LiveChatMessage, MediaFile, Video
from app.services.job_classify import classify_job


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _video(s, vid, **kw):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}", **kw)
    s.add(v)
    s.flush()
    return v


def _body(settings, rel: str, data: bytes) -> None:
    p = settings.archive_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


# --------------------------------------------------------------------------- #
# Range streaming
# --------------------------------------------------------------------------- #
def test_media_range_request(client, settings):
    rel = "youtube/videos/c/rangetest01/clip.mp4"
    payload = bytes(range(256)) * 16  # 4096 bytes
    _body(settings, rel, payload)
    with session_scope() as s:
        v = _video(s, "rangetest01", title="R")
        mf = MediaFile(video_id=v.id, media_type="video", path=rel, container="mp4")
        s.add(mf)
        s.flush()
        vid_pk, mf_id = v.id, mf.id

    full = client.get(f"/api/videos/{vid_pk}/media/{mf_id}")
    assert full.status_code == 200
    assert full.headers["content-type"] == "video/mp4"
    assert full.headers["accept-ranges"] == "bytes"
    assert int(full.headers["content-length"]) == len(payload)

    r = client.get(f"/api/videos/{vid_pk}/media/{mf_id}", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{len(payload)}"
    assert int(r.headers["content-length"]) == 100
    assert r.content == payload[100:200]


def test_media_guard_outside_archive_and_mismatch(client, settings):
    with session_scope() as s:
        v = _video(s, "guardtest01", title="G")
        # path that escapes ARCHIVE_ROOT must be refused
        mf = MediaFile(video_id=v.id, media_type="video", path="../../etc/passwd")
        s.add(mf)
        s.flush()
        vid_pk, mf_id = v.id, mf.id
        other = _video(s, "guardother1")
        other_pk = other.id
    assert client.get(f"/api/videos/{vid_pk}/media/{mf_id}").status_code == 404  # escapes root
    assert client.get(f"/api/videos/{other_pk}/media/{mf_id}").status_code == 404  # wrong video


def test_thumbnail_endpoint(client, settings):
    rel = "youtube/videos/c/thumbtest01/t.jpg"
    _body(settings, rel, b"\xff\xd8\xff\xe0jpegdata")
    with session_scope() as s:
        v = _video(s, "thumbtest01", title="T", thumbnail_path=rel)
        s.add(MediaFile(video_id=v.id, media_type="thumbnail", path=rel))
        s.flush()
        vid_pk = v.id
        v2 = _video(s, "nothumb0001", title="N")
        v2_pk = v2.id
    r = client.get(f"/api/videos/{vid_pk}/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/videos/{v2_pk}/thumbnail").status_code == 404


# --------------------------------------------------------------------------- #
# Videos list: channel filter / sort / has_thumbnail / channels
# --------------------------------------------------------------------------- #
def test_videos_channel_filter_sort_thumbnail(client, settings):
    with session_scope() as s:
        a = _video(s, "chA00000001", title="Banana", channel_id="UCa", channel_title="A",
                   first_seen_at=datetime(2024, 1, 1))
        s.add(MediaFile(video_id=a.id, media_type="thumbnail", path="youtube/x/a/t.jpg"))
        _video(s, "chA00000002", title="Apple", channel_id="UCa", channel_title="A",
               first_seen_at=datetime(2024, 1, 2))
        _video(s, "chB00000001", title="Cherry", channel_id="UCb", channel_title="B",
               first_seen_at=datetime(2024, 1, 3))

    only_a = client.get("/api/videos?channel_id=UCa").json()
    assert {v["youtube_video_id"] for v in only_a} == {"chA00000001", "chA00000002"}
    assert any(v["has_thumbnail"] for v in only_a)

    titles = [v["title"] for v in client.get("/api/videos?sort=title").json()]
    assert titles == sorted(titles, key=str.lower)

    channels = client.get("/api/videos/channels").json()
    by_id = {c["channel_id"]: c for c in channels}
    assert by_id["UCa"]["count"] == 2 and by_id["UCb"]["count"] == 1


def test_related_videos(client, settings):
    with session_scope() as s:
        v = _video(s, "rel00000001", title="main", channel_id="UCx")
        _video(s, "rel00000002", title="same channel", channel_id="UCx")
        other = _video(s, "rel00000003", title="in collection", channel_id="UCy")
        coll = Collection(type="playlist", title="PL", url="https://x")
        s.add(coll)
        s.flush()
        s.add(CollectionItem(collection_id=coll.id, video_id=v.id, youtube_video_id=v.youtube_video_id))
        s.add(CollectionItem(collection_id=coll.id, video_id=other.id, youtube_video_id=other.youtube_video_id))
        vid_pk = v.id
    body = client.get(f"/api/videos/{vid_pk}/related").json()
    assert "rel00000002" in {x["youtube_video_id"] for x in body["same_channel"]}
    assert "rel00000003" in {x["youtube_video_id"] for x in body["same_collection"]}


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def test_search_all_types_no_raw(client, settings):
    with session_scope() as s:
        v = _video(s, "search00001", title="Never Gonna", channel_id="UCs", channel_title="Rick")
        s.add(Comment(video_id=v.id, comment_id="c1", text="gonna give you up", author_name="Fan", like_count=3))
        s.add(LiveChatMessage(video_id=v.id, message_id="m1", message="gonna run around", author_name="Chat"))
        s.add(Collection(type="takeout_playlist", title="Gonna playlist", url="https://x"))

    body = client.get("/api/search?q=gonna").json()
    types = {r["type"] for r in body["results"]}
    assert {"video", "comment", "live_chat", "collection"} <= types
    # no raw_json anywhere in the payload
    assert "raw_json" not in str(body)
    # comment hit carries a snippet + links to the video
    chit = next(r for r in body["results"] if r["type"] == "comment")
    assert chit["video_id"] is not None and chit["snippet"]

    # type filter
    only_v = client.get("/api/search?q=gonna&types=video").json()
    assert all(r["type"] == "video" for r in only_v["results"])


def test_search_requires_query(client):
    assert client.get("/api/search").status_code == 422  # q is required


# --------------------------------------------------------------------------- #
# Library summary
# --------------------------------------------------------------------------- #
def test_library_summary(client, settings):
    with session_scope() as s:
        s.add(Collection(type="channel", title="chan", url="https://c"))
        s.add(Collection(type="takeout_playlist", title="pl", url="https://p"))
    body = client.get("/api/library/summary").json()
    cats = {c["key"]: c for c in body["categories"]}
    assert cats["liked_videos"]["available"] is False  # planned
    assert cats["subscriptions"]["count"] >= 1
    assert cats["playlists"]["count"] >= 1


# --------------------------------------------------------------------------- #
# Job classification
# --------------------------------------------------------------------------- #
def test_classify_job_rate_limited_from_meta_and_stderr():
    j1 = Job(type="comments_refresh", status="failed", meta={"rate_limited": True, "retryable": True})
    c1 = classify_job(j1)
    assert c1["rate_limited"] and c1["retryable"] and "429" in (c1["summary"] or "")

    j2 = Job(type="download", status="failed", error_message="ERROR: HTTP Error 429: Too Many Requests")
    assert classify_job(j2)["rate_limited"] is True

    j3 = Job(type="download", status="partial_success")
    c3 = classify_job(j3)
    assert c3["partial"] is True and c3["retryable"] is True

    j4 = Job(type="download", status="failed", error_message="WARNING: Could not find a suitable impersonate target")
    assert any("impersonation" in w or "impersonate" in w for w in classify_job(j4)["warnings"])

    j5 = Job(type="download", status="success")
    assert classify_job(j5) == {
        "rate_limited": False,
        "partial": False,
        "retryable": False,
        "warnings": [],
        "summary": "Success",
    }


def test_jobs_api_includes_classification(client):
    with session_scope() as s:
        s.add(Job(type="download", status="failed", url="u", error_message="HTTP Error 429: Too Many Requests"))
    rows = client.get("/api/jobs").json()
    assert rows and "classification" in rows[0]
    assert rows[0]["classification"]["rate_limited"] is True
    detail = client.get(f"/api/jobs/{rows[0]['id']}").json()
    assert detail["classification"]["rate_limited"] is True
