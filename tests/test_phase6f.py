"""Phase 6F tests: build identity / preflight (stale-worker guard) / large-import
runner / verify-import / scheduled session cleanup.

The headline guard: ``system_preflight`` must FAIL when a worker's build_id
differs from web's (stale worker), and ``import_large --apply`` (as a job) must
refuse to import in that case. ``cleanup-auto`` deletes ONLY session rows.
"""

from __future__ import annotations

import fnmatch
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Job, LikedVideo, TakeoutImportSession, WatchHistoryEvent
from app.services import build_info as bi
from app.services import preflight as pf
from app.services import takeout as tk
import app.worker.tasks as worker


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


class FakeRedis:
    """Minimal in-memory Redis stand-in for heartbeat/preflight tests."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def ping(self):
        return True

    def set(self, k, v, ex=None):
        self.store[k] = v

    def get(self, k):
        return self.store.get(k)

    def scan_iter(self, match=None):
        for k in list(self.store):
            if match is None or fnmatch.fnmatch(k, match):
                yield k


def _ma_zip(root: Path, name: str, *, n_liked=12, n_watch=8) -> str:
    acts = [{"title": f"高く評価した動画: L{i}",
             "titleUrl": f"https://www.youtube.com/watch?v=p6fl{i:07d}",
             "time": f"2025-01-{(i % 28) + 1:02d}T00:00:00Z",
             "subtitles": [{"name": f"C{i}"}]} for i in range(n_liked)]
    acts += [{"title": f"視聴済み: W{i}",
              "titleUrl": f"https://www.youtube.com/watch?v=p6fw{i:07d}",
              "time": f"2025-02-{(i % 28) + 1:02d}T00:00:00Z"} for i in range(n_watch)]
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / name, "w") as z:
        z.writestr("Takeout/マイ アクティビティ/YouTube/MyActivity.json",
                   json.dumps(acts, ensure_ascii=False))
    return name


# --------------------------------------------------------------------------- #
# build-info
# --------------------------------------------------------------------------- #
def test_build_info_has_stable_id_and_job_types():
    info = bi.build_info()
    assert info["build_id"] and bi.build_id() == bi.build_id()  # stable
    assert "takeout_import" in info["supported_job_types"]
    assert info["schema_head"]  # alembic head resolved from code


def test_build_info_api(client):
    r = client.get("/api/system/build-info").json()
    assert r["app_version"] == "0.1.0" and r["build_id"]
    assert "takeout_import" in r["supported_job_types"]


def test_build_info_cli():
    res = CliRunner().invoke(cli_app, ["system", "build-info"])
    assert res.exit_code == 0, res.output
    assert "build_id" in res.output and "schema_head" in res.output


def test_health_full_api(client):
    r = client.get("/api/system/health/full").json()
    assert "build_info" in r and "worker_build_match" in r and "workers" in r


# --------------------------------------------------------------------------- #
# worker heartbeat + preflight (stale-worker guard)
# --------------------------------------------------------------------------- #
def test_worker_heartbeat_roundtrip():
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)
    hbs = bi.read_worker_heartbeats(fake)
    assert len(hbs) == 1
    assert hbs[0]["build_id"] == bi.build_id()
    assert "takeout_import" in hbs[0]["supported_job_types"]
    assert hbs[0]["stale"] is False


def test_preflight_worker_match(settings, session, monkeypatch):
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)  # matching build_id
    monkeypatch.setattr("app.worker.queue.get_redis", lambda: fake)
    rep = pf.system_preflight(session, settings)
    names = {c["name"]: c["status"] for c in rep["checks"]}
    assert names["redis_connect"] == "ok"
    assert names["worker_build_id"] == "ok"
    assert names["worker_build_match"] == "ok"
    assert names["worker_takeout_capable"] == "ok"
    assert rep["ok"] is True  # warns (dev schema) allowed


def test_preflight_detects_stale_worker(settings, session, monkeypatch):
    fake = FakeRedis()
    fake.store["archiver:worker:heartbeat:old:1"] = json.dumps({
        "build_id": "src:STALEOLDBUILD", "app_version": "0.1.0",
        "supported_job_types": ["takeout_import"], "worker_id": "old:1", "ts": time.time(),
    })
    monkeypatch.setattr("app.worker.queue.get_redis", lambda: fake)
    rep = pf.system_preflight(session, settings)
    names = {c["name"]: c["status"] for c in rep["checks"]}
    assert names["worker_build_match"] == "fail"  # stale worker caught
    assert rep["ok"] is False


def test_preflight_no_worker_fails(settings, session, monkeypatch):
    monkeypatch.setattr("app.worker.queue.get_redis", lambda: FakeRedis())  # pings, no heartbeats
    rep = pf.system_preflight(session, settings)
    names = {c["name"]: c["status"] for c in rep["checks"]}
    assert names["worker_build_id"] == "fail"  # no worker heartbeat
    assert rep["ok"] is False


def test_preflight_cli_exit_code(settings):
    # dead redis (no worker) -> preflight fails -> non-zero exit
    res = CliRunner().invoke(cli_app, ["system", "preflight"])
    assert res.exit_code == 1
    assert "system preflight" in res.output


# --------------------------------------------------------------------------- #
# preflight-large
# --------------------------------------------------------------------------- #
def test_preflight_large_service(settings, session):
    name = _ma_zip(settings.takeout_import_root, "pl.zip", n_liked=10, n_watch=8)
    pl = tk.preflight_large(session, settings, name, kind="all")
    assert pl["ok"] is True and pl["parser_backend"] == "ijson"
    assert set(pl["results"]) == {"liked_videos", "watch_history"}
    assert pl["results"]["liked_videos"]["current_db_count"] == 0
    assert pl["recommended_command"] and pl["path_basename"] == "pl.zip"


def test_preflight_large_missing_zip_fails(settings, session):
    pl = tk.preflight_large(session, settings, "nope.zip", kind="liked_videos")
    assert pl["ok"] is False
    assert any(c["name"] == "zip_exists" and c["status"] == "fail" for c in pl["checks"])
    # no absolute path leaked in the detail
    assert all("/" not in c["detail"] or "not under" in c["detail"] for c in pl["checks"])


def test_preflight_large_api(client, settings):
    name = _ma_zip(settings.takeout_import_root, "plapi.zip", n_liked=6, n_watch=6)
    r = client.post("/api/takeout/preflight-large", json={"path": name}).json()
    assert r["ok"] is True and r["parser_backend"] == "ijson"
    assert "liked_videos" in r["results"] and "watch_history" in r["results"]


# --------------------------------------------------------------------------- #
# import-large
# --------------------------------------------------------------------------- #
def test_import_large_dry_run_default(settings, session):
    name = _ma_zip(settings.takeout_import_root, "il.zip", n_liked=10, n_watch=0)
    res = tk.import_large(session, settings, name, kind="liked_videos", skip_preflight=True)
    session.commit()
    assert res["ok"] is True and res["dry_run"] is True       # default = dry-run
    assert res["store_raw_json"] is False                     # default = no-raw-json
    assert res["as_job"] is True
    assert session.query(LikedVideo).count() == 0             # dry-run wrote nothing


def test_import_large_apply_writes_no_raw_json(settings, session):
    name = _ma_zip(settings.takeout_import_root, "ilapply.zip", n_liked=10, n_watch=0)
    res = tk.import_large(
        session, settings, name, kind="liked_videos", apply=True, as_job=False, skip_preflight=True,
    )
    session.commit()
    assert res["ok"] is True and res["dry_run"] is False
    rows = session.query(LikedVideo).all()
    assert len(rows) == 10 and all(lv.raw_json is None for lv in rows)  # no-raw-json default


def test_import_large_blocks_on_failed_preflight(settings):
    # apply + job, NO skip_preflight: system preflight fails (dead redis/no worker)
    # -> import must be BLOCKED and nothing written.
    name = _ma_zip(settings.takeout_import_root, "ilblock.zip", n_liked=8, n_watch=0)
    with session_scope() as s:
        res = tk.import_large(s, get_settings(), name, kind="liked_videos", apply=True, as_job=True)
        s.commit()
    assert res["ok"] is False and res["preflight_ok"] is False
    assert "preflight" in (res["message"] or "").lower()
    with session_scope() as s:
        assert s.query(LikedVideo).count() == 0  # nothing imported


def test_cli_import_large_dry_run_default(settings):
    name = _ma_zip(settings.takeout_import_root, "ilcli.zip", n_liked=8, n_watch=0)
    res = CliRunner().invoke(
        cli_app, ["takeout", "import-large", name, "--kind", "liked_videos", "--skip-preflight"]
    )
    assert res.exit_code == 0, res.output
    assert "dry_run=True" in res.output and "store_raw_json=False" in res.output
    with session_scope() as s:
        assert s.query(LikedVideo).count() == 0


# --------------------------------------------------------------------------- #
# verify-import
# --------------------------------------------------------------------------- #
def test_verify_import_service(settings, session):
    name = _ma_zip(settings.takeout_import_root, "vi.zip", n_liked=10, n_watch=0)
    tk.run_import_liked_videos(session, settings, name, store_raw_json=False)
    session.commit()
    sid = tk.list_import_sessions(session)[0].session_id
    v = tk.verify_import(session, settings, session_id=sid)
    assert v["ok"] is True and v["status"] == "success"
    assert v["store_raw_json"] is False
    assert v["raw_json_real_blobs"]["liked_videos"] == 0
    assert v["leak_check_ok"] is True and v["leak_findings"] == []


def test_verify_import_latest(settings, session):
    name = _ma_zip(settings.takeout_import_root, "vil.zip", n_liked=5, n_watch=0)
    tk.run_import_liked_videos(session, settings, name, store_raw_json=False)
    session.commit()
    v = tk.verify_import(session, settings, latest=True, kind="liked_videos")
    assert v["session_id"] and v["import_kind"] == "liked_videos"


def test_verify_import_api(client, settings):
    name = _ma_zip(settings.takeout_import_root, "viapi.zip", n_liked=4, n_watch=0)
    with session_scope() as s:
        tk.run_import_liked_videos(s, get_settings(), name, store_raw_json=False)
        s.commit()
        sid = tk.list_import_sessions(s)[0].session_id
    r = client.get(f"/api/takeout/import-sessions/{sid}/verify").json()
    assert r["ok"] is True and r["leak_check_ok"] is True
    assert client.get("/api/takeout/import-sessions/nope/verify").status_code == 404


# --------------------------------------------------------------------------- #
# auto cleanup (sessions only)
# --------------------------------------------------------------------------- #
def test_cleanup_auto_disabled_no_op(settings, session):
    name = _ma_zip(settings.takeout_import_root, "ca0.zip", n_liked=4, n_watch=0)
    for _ in range(3):
        tk.run_import_liked_videos(session, settings, name, dry_run=True)
    session.commit()
    res = tk.auto_cleanup_import_sessions(session, settings)  # disabled by default
    assert res["ran"] is False and "disabled" in res["reason"]
    assert session.query(TakeoutImportSession).count() == 3


def test_cleanup_auto_force_preserves_jobs_and_data(settings, monkeypatch):
    monkeypatch.setattr(settings, "takeout_import_session_keep_last", 1)
    name = _ma_zip(settings.takeout_import_root, "ca.zip", n_liked=6, n_watch=0)
    job_ids = []
    for i in range(2):
        with session_scope() as s:
            job, _ = tk.create_import_job(s, get_settings(), import_kind="liked_videos", path=name)
            s.commit()
            job_ids.append(job.id)
        worker.run_job(job_ids[-1])

    with session_scope() as s:
        jobs_before = s.query(Job).count()
        liked_before = s.query(LikedVideo).count()
        sess_before = s.query(TakeoutImportSession).count()
    assert sess_before == 2 and jobs_before >= 2 and liked_before > 0

    with session_scope() as s:
        res = tk.auto_cleanup_import_sessions(s, settings, force=True)
        s.commit()
    assert res["ran"] is True and res["result"]["deleted"] >= 1

    with session_scope() as s:
        assert s.query(TakeoutImportSession).count() < sess_before  # sessions pruned
        assert s.query(Job).count() == jobs_before                  # jobs untouched
        assert s.query(LikedVideo).count() == liked_before          # data untouched


def test_cleanup_status_reports_config_and_last_run(settings, monkeypatch):
    monkeypatch.setattr(settings, "takeout_import_session_keep_last", 1)
    monkeypatch.setattr(settings, "takeout_import_session_cleanup_enabled", True)
    name = _ma_zip(settings.takeout_import_root, "cs.zip", n_liked=4, n_watch=0)
    for _ in range(3):
        with session_scope() as s:
            tk.run_import_liked_videos(s, get_settings(), name, dry_run=True)
            s.commit()
    with session_scope() as s:
        tk.auto_cleanup_import_sessions(s, settings, force=True)
        s.commit()
    st = tk.cleanup_status(settings)
    assert st["enabled"] is True and st["keep_last"] == 1
    assert st["last_run_at"] is not None and st["last_result"]["deleted"] >= 1


def test_cli_cleanup_auto_dry_run(settings, monkeypatch):
    monkeypatch.setattr(settings, "takeout_import_session_keep_last", 1)
    name = _ma_zip(settings.takeout_import_root, "cadry.zip", n_liked=4, n_watch=0)
    for _ in range(2):
        with session_scope() as s:
            tk.run_import_liked_videos(s, get_settings(), name, dry_run=True)
            s.commit()
    res = CliRunner().invoke(cli_app, ["takeout", "sessions", "cleanup-auto", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "dry-run" in res.output and "NOT deleted" in res.output
    with session_scope() as s:
        assert s.query(TakeoutImportSession).count() == 2  # dry-run deleted nothing
