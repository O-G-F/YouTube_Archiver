"""Phase 7F tests: progress graph data, run-history filters, recommendation
export, and scheduler-run retention/cleanup.

Cleanup deletes ONLY scheduler_runs rows — never jobs; job.meta.scheduler_run_id
survives a deleted run.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Job, SchedulerRun
from app.services import scheduler as sch


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _seed_runs(s, n=5, *, base=None):
    base = base or datetime(2025, 1, 1)
    for i in range(n):
        rt = "liked_archive" if i % 2 else "liked_metadata"
        st = "partial_success" if i % 3 == 0 else "success"
        s.add(SchedulerRun(
            run_id=f"run{i:02d}", run_type=rt, status=st,
            started_at=base + timedelta(days=i), finished_at=base + timedelta(days=i),
            jobs_created=1,
            meta={"progress_after": {
                "total_liked": 10, "metadata_fetched": i, "metadata_missing": 10 - i,
                "body_saved": i, "body_missing": 10 - i, "retryable_liked_jobs": 0,
                "failed_liked_jobs": 0, "partial_liked_jobs": 0, "active_archive_jobs": 0,
            }},
        ))
    s.flush()


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
def test_list_runs_filters(settings, session):
    _seed_runs(session, 5)
    session.commit()
    assert len(sch.list_runs(session)) == 5
    assert len(sch.list_runs(session, run_type="liked_archive")) == 2
    assert len(sch.list_runs(session, status="partial_success")) == 2  # i=0,3
    assert len(sch.list_runs(session, date_from=datetime(2025, 1, 4))) == 2  # i=3,4


def test_progress_history_filters_and_downsample(settings, session):
    # 3 runs on the SAME day + 2 on other days -> daily downsample collapses same-day
    base = datetime(2025, 5, 1, 8, 0, 0)
    session.add_all([
        SchedulerRun(run_id=f"d{i}", run_type="liked_metadata", status="success",
                     started_at=base + timedelta(hours=i), finished_at=base + timedelta(hours=i),
                     meta={"progress_after": {"total_liked": 10, "metadata_fetched": i, "body_saved": 0,
                                              "metadata_missing": 10, "body_missing": 10, "retryable_liked_jobs": 0,
                                              "failed_liked_jobs": 0, "partial_liked_jobs": 0, "active_archive_jobs": 0}})
        for i in range(3)
    ])
    session.add(SchedulerRun(run_id="day2", run_type="liked_archive", status="success",
                             started_at=datetime(2025, 5, 2, 9), finished_at=datetime(2025, 5, 2, 9),
                             meta={"progress_after": {"total_liked": 10, "metadata_fetched": 5, "body_saved": 1,
                                                      "metadata_missing": 5, "body_missing": 9, "retryable_liked_jobs": 0,
                                                      "failed_liked_jobs": 0, "partial_liked_jobs": 0, "active_archive_jobs": 0}}))
    session.commit()
    assert len(sch.progress_history(session)) == 4
    assert len(sch.progress_history(session, run_type="liked_archive")) == 1
    # daily downsample: day1 (3 runs) -> 1 point, day2 -> 1 point = 2
    assert len(sch.progress_history(session, downsample="daily")) == 2


# --------------------------------------------------------------------------- #
# recommendation export
# --------------------------------------------------------------------------- #
def test_recommend_export_env(settings, session):
    rec = sch.recommend_settings(session, settings)
    env = sch.recommend_export(rec, "env")
    assert "SCHEDULER_LIKED_ARCHIVE_LIMIT_PER_RUN=" in env
    assert "LIKED_ARCHIVE_JOB_DELAY_SECONDS=" in env
    # no secrets / tokens
    for bad in ("po_token", "cookies", "visitor_data", "client_secret"):
        assert bad not in env.lower()


def test_recommend_export_json(settings, session):
    import json

    rec = sch.recommend_settings(session, settings)
    data = json.loads(sch.recommend_export(rec, "json"))
    assert "recommended" in data and "current" in data


def test_recommend_export_api(client):
    r = client.post("/api/scheduler/recommend-settings/export", json={"format": "env"}).json()
    assert r["format"] == "env"
    assert "SCHEDULER_LIKED_ARCHIVE_LIMIT_PER_RUN=" in r["content"]
    assert "NOT changed automatically" in r["note"]
    rj = client.post("/api/scheduler/recommend-settings/export", json={"format": "json"}).json()
    assert rj["content"].strip().startswith("{")


# --------------------------------------------------------------------------- #
# cleanup (never deletes jobs)
# --------------------------------------------------------------------------- #
def test_cleanup_no_bounds_deletes_nothing(settings, session):
    _seed_runs(session, 5)
    session.commit()
    res = sch.cleanup_runs(session, dry_run=False)
    assert res["matched"] == 0 and res["deleted"] == 0
    assert session.query(SchedulerRun).count() == 5


def test_cleanup_dry_run(settings, session):
    _seed_runs(session, 5)
    session.commit()
    res = sch.cleanup_runs(session, keep_last=2, dry_run=True)
    assert res["total_runs"] == 5 and res["matched"] == 3 and res["deleted"] == 0
    assert session.query(SchedulerRun).count() == 5  # nothing deleted in dry-run


def test_cleanup_actual_keeps_jobs(settings, session):
    _seed_runs(session, 5)
    # a job linked to the OLDEST run (run00) which will be deleted
    session.add(Job(type="download", status="failed", url="https://x",
                    profile_name="video_compressed_1080p",
                    meta={"scheduler_run_id": "run00", "source_action": "liked_archive"}))
    session.commit()
    res = sch.cleanup_runs(session, keep_last=2, dry_run=False)
    session.commit()
    assert res["deleted"] == 3
    assert session.query(SchedulerRun).count() == 2
    # the JOB survives and keeps its scheduler_run_id
    job = session.query(Job).filter(Job.type == "download").one()
    assert job.meta["scheduler_run_id"] == "run00"
    assert sch.get_run(session, "run00") is None  # run00 row gone (job survived)


def test_cleanup_older_than_days(settings, session):
    now = datetime(2025, 1, 10)
    _seed_runs(session, 5, base=datetime(2025, 1, 1))  # days 1..5
    session.commit()
    # older than 6 days from now(=Jan10) -> before Jan4 -> run00(Jan1),run01(Jan2),run02(Jan3)
    res = sch.cleanup_runs(session, older_than_days=6, dry_run=True, now=now)
    assert res["matched"] == 3


# --------------------------------------------------------------------------- #
# API: cleanup + deleted-run job behaviour
# --------------------------------------------------------------------------- #
def test_cleanup_api_and_deleted_run_jobs(client):
    with session_scope() as s:
        _seed_runs(s, 4)
        s.add(Job(type="download", status="success", url="https://y",
                  profile_name="metadata_only",
                  meta={"scheduler_run_id": "run00", "source_action": "liked_archive"}))
        s.commit()
    # dry-run via API
    dr = client.post("/api/scheduler/runs/cleanup", json={"keep_last": 1, "dry_run": True}).json()
    assert dr["matched"] == 3 and dr["deleted"] == 0
    # apply
    ap = client.post("/api/scheduler/runs/cleanup", json={"keep_last": 1, "dry_run": False}).json()
    assert ap["deleted"] == 3
    # the deleted run's job is still reachable by scheduler_run_id
    jobs = client.get("/api/jobs?scheduler_run_id=run00").json()
    assert len(jobs) == 1
    # but the run detail is now 404 (UI shows "run history deleted")
    assert client.get("/api/scheduler/runs/run00").status_code == 404


def test_runs_api_filters(client):
    with session_scope() as s:
        _seed_runs(s, 5)
        s.commit()
    assert len(client.get("/api/scheduler/runs?run_type=liked_archive").json()) == 2
    assert len(client.get("/api/scheduler/runs?status=success").json()) == 3


def test_progress_history_api_downsample(client):
    with session_scope() as s:
        _seed_runs(s, 4)
        s.commit()
    pts = client.get("/api/liked-videos/progress/history?downsample=daily").json()["points"]
    # 4 runs on 4 different days -> 4 daily points
    assert len(pts) == 4
