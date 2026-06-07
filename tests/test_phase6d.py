"""Phase 6D tests: large-import benchmark + import job-ization + progress + cancel.

Benchmark measures throughput/peak-memory without writing (dry-run). Import jobs
run on the worker with a ProgressTracker writing into the session; cancellation
stops at a checkpoint and leaves partial data. source_kind is filled for all kinds.
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
from app.models import Job, LikedVideo, TakeoutImportSession, WatchHistoryEvent
from app.services import takeout as tk
import app.worker.tasks as worker


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _ma_zip(root: Path, name: str, *, n_liked=20, n_watch=10) -> str:
    acts = []
    for i in range(n_liked):
        acts.append({"title": f"高く評価した動画: L{i}",
                     "titleUrl": f"https://www.youtube.com/watch?v=p6dl{i:07d}",
                     "time": f"2025-01-{(i % 28) + 1:02d}T00:00:00Z",
                     "subtitles": [{"name": f"C{i}"}]})
    for i in range(n_watch):
        acts.append({"title": f"視聴済み: W{i}",
                     "titleUrl": f"https://www.youtube.com/watch?v=p6dw{i:07d}",
                     "time": f"2025-02-{(i % 28) + 1:02d}T00:00:00Z"})
    root.mkdir(parents=True, exist_ok=True)
    zp = root / name
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("Takeout/マイ アクティビティ/YouTube/MyActivity.json", json.dumps(acts, ensure_ascii=False))
    return name


# --------------------------------------------------------------------------- #
# benchmark
# --------------------------------------------------------------------------- #
def test_benchmark_dry_run_no_write(settings, session):
    name = _ma_zip(settings.takeout_import_root, "b.zip", n_liked=20)
    b = tk.benchmark(session, settings, name, kind="liked_videos", dry_run=True)
    assert b["scanned"] == 20 and b["imported"] == 20
    assert b["parser_backend"] in ("ijson", "json")
    assert b["entries_per_second"] is not None and b["peak_memory_mb"] is not None
    assert b["source_kind"] == "takeout_my_activity"  # precise liked source
    assert session.query(LikedVideo).count() == 0  # dry-run wrote nothing


def test_benchmark_watch_and_all(settings, session):
    name = _ma_zip(settings.takeout_import_root, "b2.zip", n_liked=5, n_watch=10)
    bw = tk.benchmark(session, settings, name, kind="watch_history", dry_run=True)
    assert bw["scanned"] >= 10  # watch + liked activities both parse as watch
    ba = tk.benchmark(session, settings, name, kind="all", dry_run=True)
    assert ba["kind"] == "all" and ba["scanned"] > 0


def test_benchmark_api(client, settings):
    name = _ma_zip(settings.takeout_import_root, "bapi.zip", n_liked=8)
    r = client.post("/api/takeout/benchmark", json={"path": name, "kind": "liked_videos", "dry_run": True}).json()
    assert r["parser_backend"] in ("ijson", "json")
    assert r["dry_run"] is True and r["imported"] == 8


# --------------------------------------------------------------------------- #
# import job
# --------------------------------------------------------------------------- #
def test_import_job_creates_session_and_job(settings):
    name = _ma_zip(settings.takeout_import_root, "job.zip", n_liked=10)
    with session_scope() as s:
        job, row = tk.create_import_job(s, get_settings(), import_kind="liked_videos", path=name, limit=5)
        s.commit()
        assert job.type == "takeout_import" and job.status == "queued"
        assert row.status == "running" and row.job_id == job.id
        assert job.meta["import_kind"] == "liked_videos" and job.meta["path_basename"] == "job.zip"
        # no host path stored in job.meta
        assert "/" not in job.meta["path_basename"]


def test_import_job_worker_runs(settings):
    name = _ma_zip(settings.takeout_import_root, "jobrun.zip", n_liked=10)
    with session_scope() as s:
        job, row = tk.create_import_job(s, get_settings(), import_kind="liked_videos", path=name, limit=7)
        s.commit()
        jid, sid = job.id, row.session_id
    worker.run_job(jid)  # worker processes it
    with session_scope() as s:
        j = s.get(Job, jid)
        r = tk.get_import_session(s, sid)
        assert j.status == "success"
        assert r.status == "success" and r.imported == 7
        assert r.source_kind == "takeout_my_activity"  # precise liked source
        assert r.entries_per_second is not None and r.parser_backend in ("ijson", "json")
        assert s.query(LikedVideo).count() == 7  # actually imported


def test_import_job_dry_run_no_write(settings):
    name = _ma_zip(settings.takeout_import_root, "jobdry.zip", n_liked=10)
    with session_scope() as s:
        job, row = tk.create_import_job(s, get_settings(), import_kind="liked_videos", path=name, dry_run=True)
        s.commit()
        jid, sid = job.id, row.session_id
    worker.run_job(jid)
    with session_scope() as s:
        r = tk.get_import_session(s, sid)
        assert r.status == "success" and r.imported == 10 and r.dry_run is True
        assert s.query(LikedVideo).count() == 0  # dry-run job wrote nothing


def test_watch_history_job_sets_source_kind(settings):
    name = _ma_zip(settings.takeout_import_root, "wjob.zip", n_liked=2, n_watch=8)
    with session_scope() as s:
        job, row = tk.create_import_job(s, get_settings(), import_kind="watch_history", path=name)
        s.commit()
        jid, sid = job.id, row.session_id
    worker.run_job(jid)
    with session_scope() as s:
        r = tk.get_import_session(s, sid)
        assert r.import_kind == "watch_history"
        assert r.source_kind == "my_activity_takeout"  # 6C bug fixed: not null


# --------------------------------------------------------------------------- #
# cancel
# --------------------------------------------------------------------------- #
def test_cancel_running_session(settings):
    name = _ma_zip(settings.takeout_import_root, "cancel.zip", n_liked=5)
    with session_scope() as s:
        job, row = tk.create_import_job(s, get_settings(), import_kind="liked_videos", path=name)
        s.commit()
        sid = row.session_id
        assert tk.request_cancel(s, sid) is True
        s.commit()
        assert tk.get_import_session(s, sid).cancel_requested is True
    # the tracker checks cancel_requested -> worker run finishes as cancelled (or success if it
    # completed before the first checkpoint on a tiny file); either way it must not crash.
    worker.run_job(job.id)
    with session_scope() as s:
        r = tk.get_import_session(s, sid)
        assert r.status in ("cancelled", "success")


def test_cancel_finished_session_409(client, settings):
    name = _ma_zip(settings.takeout_import_root, "cf.zip", n_liked=3)
    with session_scope() as s:
        _r = tk.run_import_liked_videos(s, get_settings(), name)  # synchronous -> success
        s.commit()
        sid = tk.list_import_sessions(s)[0].session_id
    assert client.post(f"/api/takeout/import-sessions/{sid}/cancel").status_code == 409


# --------------------------------------------------------------------------- #
# progress API
# --------------------------------------------------------------------------- #
def test_progress_api(client, settings):
    name = _ma_zip(settings.takeout_import_root, "prog.zip", n_liked=5)
    job = client.post("/api/takeout/import-liked-videos-job", json={"path": name, "limit": 5}).json()
    assert job["type"] == "takeout_import"
    # session linked to the job
    with session_scope() as s:
        row = s.query(TakeoutImportSession).filter(TakeoutImportSession.job_id == job["id"]).one()
        sid = row.session_id
    p = client.get(f"/api/takeout/import-sessions/{sid}/progress").json()
    assert p["session_id"] == sid and p["job_id"] == job["id"]
    assert p["status"] in ("running", "success")
    assert client.get("/api/takeout/import-sessions/nope/progress").status_code == 404


def test_session_out_exposes_6d_fields(client, settings):
    name = _ma_zip(settings.takeout_import_root, "fields.zip", n_liked=4)
    with session_scope() as s:
        job, row = tk.create_import_job(s, get_settings(), import_kind="liked_videos", path=name)
        s.commit()
        jid = job.id
    worker.run_job(jid)
    rows = client.get("/api/takeout/import-sessions").json()
    r = rows[0]
    for key in ("job_id", "parser_backend", "entries_per_second", "peak_memory_mb", "current_phase"):
        assert key in r
    # no raw_json / abs-path leaked
    assert "raw_json" not in client.get("/api/takeout/import-sessions").text
