"""Phase 7E tests: scheduler run history, progress time series, adaptive throttle.

Each scheduler run-once is recorded in scheduler_runs with a progress/queue
snapshot; created jobs carry meta.scheduler_run_id; backoff-skipped retries are
counted; recommend-settings suggests (never applies) safe limits.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Job, LikedVideo, MediaFile, SchedulerRun, Video
from app.services import jobs as jobs_svc
from app.services import liked_archive as la
from app.services import scheduler as sch


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _video(s, vid, *, title=None, body=False, meta_file=False):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
              title=title, channel_title="Chan", first_seen_at=datetime(2025, 1, 1))
    s.add(v); s.flush()
    if body:
        s.add(MediaFile(video_id=v.id, media_type="video", path=f"{vid}.mp4", profile="video_compressed_1080p"))
    if meta_file:
        s.add(MediaFile(video_id=v.id, media_type="info_json", path=f"{vid}.info.json"))
    s.flush()
    return v


def _seed(s):
    va = _video(s, "aaaAAA11111", title="HasBody", body=True, meta_file=True)
    vb = _video(s, "bbbBBB22222", title="MetaOnly", meta_file=True)
    s.add(LikedVideo(source="takeout_my_activity", youtube_video_id="aaaAAA11111", title="HasBody",
                     url="https://youtu.be/aaaAAA11111", liked_at=datetime(2025, 1, 1), video_id=va.id))
    s.add(LikedVideo(source="takeout_my_activity", youtube_video_id="bbbBBB22222", title="MetaOnly",
                     url="https://youtu.be/bbbBBB22222", liked_at=datetime(2025, 1, 2), video_id=vb.id))
    s.add(LikedVideo(source="youtube_data_api", youtube_video_id="cccCCC33333", title="Nothing",
                     url="https://youtu.be/cccCCC33333", liked_at=datetime(2025, 1, 3)))
    s.commit()


# --------------------------------------------------------------------------- #
# run recording
# --------------------------------------------------------------------------- #
def test_run_recorded_with_unique_run_id(settings):
    with session_scope() as s:
        _seed(s)
    r1 = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_metadata=True)
    r2 = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_archive=True)
    assert r1["run_id"] and r2["run_id"] and r1["run_id"] != r2["run_id"]
    with session_scope() as s:
        runs = sch.list_runs(s)
        assert len(runs) == 2
        types = {r.run_type for r in runs}
        assert types == {"liked_metadata", "liked_archive"}


def test_liked_metadata_run_fields(settings):
    with session_scope() as s:
        _seed(s)
    r = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_metadata=True)
    with session_scope() as s:
        run = sch.get_run(s, r["run_id"])
        assert run.run_type == "liked_metadata"
        assert run.jobs_created == 1  # only C missing metadata
        assert run.body_count_before == 1 and run.body_count_after == 1  # metadata adds no body
        assert run.meta["progress_after"]["total_liked"] == 3
        assert "progress_before" in run.meta and "queue_after" in run.meta


def test_archive_run_jobs_have_scheduler_run_id(settings):
    with session_scope() as s:
        _seed(s)
    r = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_archive=True)
    with session_scope() as s:
        jobs = sch.run_jobs(s, r["run_id"])
        assert len(jobs) == 2
        for j in jobs:
            assert j.meta["scheduler_run_id"] == r["run_id"]
            assert j.meta["scheduled_by"] == "scheduler_liked_archive"
            assert j.profile_name == settings.liked_archive_default_profile


def test_retry_run_records_backoff_skip(settings):
    with session_scope() as s:
        _seed(s)
        r = la.enqueue_archive(s, settings, filters=la.LikedFilters(missing_body=True), limit=2,
                               profile="video_compressed_1080p", submit=False)
        _429 = "ERROR: HTTP Error 429: Too Many Requests"
        future = datetime(2025, 1, 1) + timedelta(days=3650)
        for jid in r.job_ids:
            j = s.get(Job, jid)
            jobs_svc.mark_failed(s, j, _429)
            jobs_svc.apply_classification(s, j, settings, _429)
            j.next_retry_at = future  # both still inside backoff
        s.commit()
    run = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_retry=True)
    assert run["liked_retry_jobs_requeued"] == 0
    assert run["skipped_backoff"] == 2
    with session_scope() as s:
        rec = sch.get_run(s, run["run_id"])
        assert rec.run_type == "liked_retry"
        assert rec.skipped_backoff == 2


def test_run_recording_is_failsafe(settings, monkeypatch):
    # If run-recording blows up, the run still completes + jobs are created.
    with session_scope() as s:
        _seed(s)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sch, "_record_scheduler_run", boom)
    r = sch.run_once(settings, reason="manual", do_collections=False, do_comments=False, do_liked_metadata=True)
    assert r["liked_metadata_jobs_created"] == 1  # job creation unaffected
    with session_scope() as s:
        # the job exists even though run recording failed
        assert s.query(Job).filter(Job.profile_name == "metadata_only").count() == 1


# --------------------------------------------------------------------------- #
# APIs
# --------------------------------------------------------------------------- #
def test_runs_and_stats_api(client):
    with session_scope() as s:
        _seed(s)
    client.post("/api/scheduler/run-once", json={"liked_metadata": True})
    client.post("/api/scheduler/run-once", json={"liked_archive": True})
    runs = client.get("/api/scheduler/runs").json()
    assert len(runs) == 2
    rid = runs[0]["run_id"]
    detail = client.get(f"/api/scheduler/runs/{rid}").json()
    assert detail["run_id"] == rid and "meta" in detail
    jobs = client.get(f"/api/scheduler/runs/{rid}/jobs").json()
    assert all(j["type"] == "download" for j in jobs)
    stats = client.get("/api/scheduler/stats").json()
    assert stats["runs_considered"] == 2
    assert stats["by_type"].get("liked_metadata") == 1


def test_runs_detail_404(client):
    assert client.get("/api/scheduler/runs/nope").status_code == 404


def test_progress_history_api(client):
    with session_scope() as s:
        _seed(s)
    client.post("/api/scheduler/run-once", json={"liked_metadata": True})
    hist = client.get("/api/liked-videos/progress/history").json()
    assert len(hist["points"]) == 1
    p = hist["points"][0]
    assert p["total_liked"] == 3 and p["run_type"] == "liked_metadata"
    # no raw_json / personal data
    assert "raw_json" not in client.get("/api/liked-videos/progress/history").text


def test_recommend_settings_api_no_autochange(client, settings):
    with session_scope() as s:
        _seed(s)
    r = client.post("/api/scheduler/recommend-settings", json={"lookback": 30}).json()
    assert "current" in r and "recommended" in r
    assert "NOT changed automatically" in r["note"]
    # current archive limit is the configured default (unchanged on disk)
    assert r["current"]["scheduler_liked_archive_limit_per_run"] == settings.scheduler_liked_archive_limit_per_run


def test_recommend_throttle_lowers_limit(client, settings):
    # Seed several failed (429/incomplete) liked-archive jobs -> high throttle rate.
    with session_scope() as s:
        _seed(s)
        v = s.query(Video).filter_by(youtube_video_id="bbbBBB22222").one()
        _inc = "WARNING: Incomplete data received. Retrying"
        for i in range(4):
            j = Job(type="download", status="failed", url=f"https://www.youtube.com/watch?v=z{i}",
                    profile_name="video_compressed_1080p", video_id=v.id,
                    error_message=_inc,  # classify_job re-derives reasons from this
                    meta={"source_action": "liked_archive", "requested_profile": "video_compressed_1080p"})
            s.add(j); s.flush()
            jobs_svc.apply_classification(s, j, settings, _inc)
        s.commit()
    r = client.post("/api/scheduler/recommend-settings", json={}).json()
    assert r["rates"]["throttle_rate"] is not None and r["rates"]["throttle_rate"] >= 0.3
    assert r["recommended"]["scheduler_liked_archive_limit_per_run"] == 1
    assert any("hrottle" in reason for reason in r["reasons"])


def test_jobs_scheduler_run_id_filter(client):
    with session_scope() as s:
        _seed(s)
    r = client.post("/api/scheduler/run-once", json={"liked_archive": True}).json()
    rows = client.get(f"/api/jobs?scheduler_run_id={r['run_id']}").json()
    assert len(rows) == r["liked_archive_jobs_created"] >= 1
    assert client.get("/api/jobs?scheduler_run_id=nope").json() == []
