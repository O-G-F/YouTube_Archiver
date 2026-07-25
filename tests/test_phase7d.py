"""Phase 7D tests: liked-archive scheduler passes + progress + queue health.

Covers scheduler run-once liked metadata / archive / retry, the safety brakes
(active-job suppression, per-video dedup, next_retry_at backoff, attempt cap),
the progress + queue-status APIs, and job.meta scheduled_by/selected_by tags.
metadata_only never adds a body.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Job, LikedVideo, MediaFile, Video
from app.services import jobs as jobs_svc
from app.services import liked_archive as la
from app.services import queue_health
from app.services import scheduler as sch


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _video(s, vid, *, title=None, body=False, meta_file=False, channel="Chan"):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
              title=title, channel_title=channel, first_seen_at=datetime(2025, 1, 1))
    s.add(v)
    s.flush()
    if body:
        s.add(MediaFile(video_id=v.id, media_type="video", path=f"{vid}.mp4", profile="video_compressed_1080p"))
    if meta_file:
        s.add(MediaFile(video_id=v.id, media_type="info_json", path=f"{vid}.info.json"))
    s.flush()
    return v


def _liked(s, vid, *, source="takeout_my_activity", title=None, video=None, liked_at=None, channel="Chan"):
    lv = LikedVideo(source=source, youtube_video_id=vid, title=title, channel_title=channel,
                    url=f"https://youtu.be/{vid}", liked_at=liked_at or datetime(2025, 1, 1),
                    video_id=video.id if video else None)
    s.add(lv)
    s.flush()
    return lv


def _seed(s):
    va = _video(s, "aaaAAA11111", title="HasBody", body=True, meta_file=True, channel="Chan A")
    vb = _video(s, "bbbBBB22222", title="MetaOnly", meta_file=True, channel="Chan B")
    _liked(s, "aaaAAA11111", video=va, liked_at=datetime(2025, 1, 1), channel="Chan A")
    _liked(s, "bbbBBB22222", video=vb, liked_at=datetime(2025, 1, 2), channel="Chan B")
    _liked(s, "cccCCC33333", source="youtube_data_api", title="Nothing", liked_at=datetime(2025, 1, 3), channel="Chan C")
    s.commit()


# --------------------------------------------------------------------------- #
# scheduler liked passes (manual run-once)
# --------------------------------------------------------------------------- #
def test_scheduler_liked_metadata(settings):
    with session_scope() as s:
        _seed(s)
    r = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_metadata=True)
    assert r["liked_metadata_selected"] == 1  # only C missing metadata
    assert r["liked_metadata_jobs_created"] == 1
    with session_scope() as s:
        j = s.get(Job, r["job_ids"][0])
        assert j.profile_name == "metadata_only"
        assert j.meta["scheduled_by"] == "scheduler_liked_metadata"
        assert j.meta["selected_by"] == "missing_metadata"
        # metadata pass adds no body
        assert s.query(MediaFile).filter(MediaFile.media_type.in_(("video", "audio"))).count() == 1


def test_scheduler_liked_archive(settings):
    with session_scope() as s:
        _seed(s)
    r = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_archive=True)
    assert r["liked_archive_selected"] == 2  # B + C (A has body)
    assert r["liked_archive_jobs_created"] == 2
    with session_scope() as s:
        for jid in r["job_ids"]:
            j = s.get(Job, jid)
            assert j.profile_name == settings.effective_body_archive_profile  # Phase 9A: comments-light
            assert j.meta["scheduled_by"] == "scheduler_liked_archive"
            assert j.meta["selected_by"] == "missing_body"


def test_scheduler_archive_suppressed_when_active(settings):
    with session_scope() as s:
        _seed(s)
    # first archive run creates body jobs (queued)
    r1 = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_archive=True)
    assert r1["liked_archive_jobs_created"] == 2
    # second run is suppressed because body jobs are still active
    r2 = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_archive=True)
    assert r2["liked_archive_jobs_created"] == 0
    assert r2["skipped_active_jobs"] == 1


def test_scheduler_metadata_dedup(settings):
    with session_scope() as s:
        _seed(s)
    sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_metadata=True)
    # second metadata run: C already has an active metadata job -> dedup
    r2 = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_metadata=True)
    assert r2["liked_metadata_jobs_created"] == 0
    assert r2["skipped_duplicates"] == 1


def test_scheduler_liked_retry_respects_backoff_and_cap(settings):
    with session_scope() as s:
        _seed(s)
        r = la.enqueue_archive(s, settings, filters=la.LikedFilters(missing_body=True), limit=2,
                               profile="video_compressed_1080p", submit=False)
        future = datetime(2025, 1, 1) + timedelta(days=3650)
        _429 = "ERROR: HTTP Error 429: Too Many Requests"
        # job A: retryable, backoff elapsed (no next_retry_at)
        ja = s.get(Job, r.job_ids[0])
        jobs_svc.mark_failed(s, ja, _429)
        jobs_svc.apply_classification(s, ja, settings, _429)
        ja.next_retry_at = None
        # job B: retryable but still inside backoff window (future)
        jb = s.get(Job, r.job_ids[1])
        jobs_svc.mark_failed(s, jb, _429)
        jobs_svc.apply_classification(s, jb, settings, _429)
        jb.next_retry_at = future
        s.commit()
        ja_id, jb_id = ja.id, jb.id

    r = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_retry=True)
    assert r["liked_retry_jobs_requeued"] == 1  # only A (B is still in backoff)
    with session_scope() as s:
        assert s.get(Job, ja_id).status == "queued"
        assert s.get(Job, jb_id).status == "failed"  # untouched
        assert s.get(Job, ja_id).meta["scheduled_by"] == "scheduler_liked_retry"


def test_scheduler_liked_retry_skips_at_cap(settings):
    with session_scope() as s:
        _seed(s)
        r = la.enqueue_archive(s, settings, filters=la.LikedFilters(missing_body=True), limit=1,
                               profile="video_compressed_1080p", submit=False)
        j = s.get(Job, r.job_ids[0])
        jobs_svc.mark_failed(s, j, "ERROR: HTTP Error 429: Too Many Requests")
        j.retry_count = settings.download_retry_max_attempts
        jobs_svc.apply_classification(s, j, settings, "ERROR: HTTP Error 429: Too Many Requests")
        j.next_retry_at = None
        s.commit()
    r = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_retry=True)
    assert r["liked_retry_jobs_requeued"] == 0


def test_scheduler_disabled_by_default(settings):
    with session_scope() as s:
        _seed(s)
    # reason="scheduler" honours the enable flags (all default OFF)
    r = sch.run_once(settings, reason="scheduler")
    assert r["liked_metadata_jobs_created"] == 0
    assert r["liked_archive_jobs_created"] == 0


# --------------------------------------------------------------------------- #
# progress + queue APIs
# --------------------------------------------------------------------------- #
def test_progress_api_counts(client):
    with session_scope() as s:
        _seed(s)
    p = client.get("/api/liked-videos/progress").json()
    assert p["total_liked"] == 3
    assert p["metadata_fetched"] == 2 and p["metadata_missing"] == 1
    assert p["body_saved"] == 1 and p["body_missing"] == 2
    assert p["by_source"] == {"takeout_my_activity": 2, "youtube_data_api": 1}
    assert {c["channel"] for c in p["by_channel"]} >= {"Chan A", "Chan B", "Chan C"}
    # no personal data leaks
    assert "raw_json" not in client.get("/api/liked-videos/progress").text


def test_progress_reflects_jobs(client, settings):
    with session_scope() as s:
        _seed(s)
    client.post("/api/scheduler/run-once", json={"liked_archive": True})
    p = client.get("/api/liked-videos/progress").json()
    assert p["active_archive_jobs"] == 2


def test_queue_status_api(client):
    with session_scope() as s:
        _seed(s)
    client.post("/api/scheduler/run-once", json={"liked_metadata": True})
    q = client.get("/api/queue/status").json()
    assert q["queued"] >= 1
    assert q["by_type"].get("download", 0) >= 1
    assert q["by_source_action"].get("liked_archive", 0) >= 1
    assert q["total_active"] == q["queued"] + q["running"]


def test_scheduler_run_once_api_liked_only(client):
    with session_scope() as s:
        _seed(s)
    # requesting a liked pass must NOT also run collections/comments
    r = client.post("/api/scheduler/run-once", json={"liked_metadata": True}).json()
    assert r["liked_metadata_jobs_created"] == 1
    assert r["collection_jobs_created"] == 0
    assert r["comments_jobs_created"] == 0


def test_jobs_api_source_action_filter(client):
    with session_scope() as s:
        _seed(s)
    client.post("/api/scheduler/run-once", json={"liked_metadata": True})
    rows = client.get("/api/jobs?source_action=scheduler_liked_metadata").json()
    assert len(rows) == 1
    assert rows[0]["type"] == "download"
    # a non-matching tag returns nothing
    assert client.get("/api/jobs?source_action=does_not_exist").json() == []
