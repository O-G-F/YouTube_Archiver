"""Phase 6E tests: no-raw-json import mode, DB stats, benchmark-large,
import-session retention/cleanup, and the --safe-large CLI preset.

Privacy/size invariants under test:
- ``store_raw_json=False`` drops the raw activity blob but ALWAYS keeps the
  normalized fields (video id / title / channel / timestamp / query).
- ``db-stats`` surfaces row counts + raw_json growth without reading content.
- ``benchmark-large`` is a dry-run full scan: no writes, no raw_json in output.
- ``cleanup`` deletes ONLY session rows — never jobs, never imported data, and
  never a running session; with no bounds it deletes nothing (safety).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import _safe_large_defaults
from app.cli import app as cli_app
from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Job, LikedVideo, SearchHistoryEvent, TakeoutImportSession, WatchHistoryEvent
from app.services import db_stats as dbs
from app.services import takeout as tk
import app.worker.tasks as worker


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _ma_zip(root: Path, name: str, *, n_liked=12, n_watch=8, n_search=0) -> str:
    """Build a My Activity ZIP. Liked + watch share one JSON (classified watch);
    search entries go in a separate JSON peeked as search_history."""
    acts = []
    for i in range(n_liked):
        acts.append({"title": f"高く評価した動画: L{i}",
                     "titleUrl": f"https://www.youtube.com/watch?v=p6el{i:07d}",
                     "time": f"2025-01-{(i % 28) + 1:02d}T00:00:00Z",
                     "subtitles": [{"name": f"Chan{i}"}]})
    for i in range(n_watch):
        acts.append({"title": f"視聴済み: W{i}",
                     "titleUrl": f"https://www.youtube.com/watch?v=p6ew{i:07d}",
                     "time": f"2025-02-{(i % 28) + 1:02d}T00:00:00Z",
                     "subtitles": [{"name": f"WChan{i}"}]})
    root.mkdir(parents=True, exist_ok=True)
    zp = root / name
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("Takeout/マイ アクティビティ/YouTube/MyActivity.json",
                   json.dumps(acts, ensure_ascii=False))
        if n_search:
            searches = [{"title": f"Searched for q{i}",
                         "titleUrl": f"https://www.youtube.com/results?search_query=q{i}",
                         "time": f"2025-03-{(i % 28) + 1:02d}T00:00:00Z"} for i in range(n_search)]
            z.writestr("Takeout/マイ アクティビティ/YouTube/MySearches.json",
                       json.dumps(searches, ensure_ascii=False))
    return name


# --------------------------------------------------------------------------- #
# no-raw-json import mode
# --------------------------------------------------------------------------- #
def test_liked_no_raw_json_keeps_normalized_fields(settings, session):
    name = _ma_zip(settings.takeout_import_root, "nr_liked.zip", n_liked=10, n_watch=0)
    r = tk.run_import_liked_videos(session, settings, name, store_raw_json=False)
    session.commit()
    assert r["store_raw_json"] is False
    assert r["raw_json_stored_count"] == 0 and r["raw_json_skipped_count"] == r["imported_count"] > 0
    rows = session.query(LikedVideo).all()
    assert rows and all(lv.raw_json is None for lv in rows)         # blob dropped
    assert all(lv.youtube_video_id and lv.title for lv in rows)     # normalized kept
    assert any(lv.channel_title for lv in rows)


def test_liked_raw_json_on_by_default(settings, session):
    name = _ma_zip(settings.takeout_import_root, "r_liked.zip", n_liked=6, n_watch=0)
    r = tk.run_import_liked_videos(session, settings, name)  # default True
    session.commit()
    assert r["store_raw_json"] is True and r["raw_json_stored_count"] == r["imported_count"] > 0
    assert any(lv.raw_json is not None for lv in session.query(LikedVideo).all())


def test_watch_no_raw_json_keeps_normalized_fields(settings, session):
    name = _ma_zip(settings.takeout_import_root, "nr_watch.zip", n_liked=0, n_watch=9)
    r = tk.run_import(session, settings, name, store_raw_json=False)
    session.commit()
    assert r["store_raw_json"] is False and r["raw_json_stored_count"] == 0
    rows = session.query(WatchHistoryEvent).all()
    assert rows and all(ev.raw_json is None for ev in rows)
    assert all(ev.youtube_video_id and ev.title for ev in rows)     # normalized kept


def test_search_no_raw_json_keeps_normalized_fields(settings, session):
    name = _ma_zip(settings.takeout_import_root, "nr_search.zip", n_liked=0, n_watch=0, n_search=7)
    r = tk.run_import_search(session, settings, name, store_raw_json=False)
    session.commit()
    assert r["store_raw_json"] is False and r["raw_json_stored_count"] == 0
    rows = session.query(SearchHistoryEvent).all()
    assert rows and all(ev.raw_json is None for ev in rows)
    assert all(ev.query for ev in rows)                             # normalized query kept


def test_import_api_store_raw_json_false(client, settings):
    name = _ma_zip(settings.takeout_import_root, "api_nr.zip", n_liked=8, n_watch=0)
    r = client.post("/api/takeout/import-liked-videos",
                    json={"path": name, "store_raw_json": False}).json()
    assert r["store_raw_json"] is False and r["raw_json_stored_count"] == 0
    with session_scope() as s:
        assert s.query(LikedVideo).count() == r["imported_count"] > 0
        assert all(lv.raw_json is None for lv in s.query(LikedVideo).all())


def test_import_job_no_raw_json(settings):
    # The background-job path must honor store_raw_json AND record the counts in
    # session.meta (so the UI/db-stats reflect the no-raw-json run).
    name = _ma_zip(settings.takeout_import_root, "job_nr.zip", n_liked=30, n_watch=0)
    with session_scope() as s:
        job, row = tk.create_import_job(
            s, get_settings(), import_kind="liked_videos", path=name, store_raw_json=False
        )
        s.commit()
        jid = job.id
    worker.run_job(jid)
    with session_scope() as s:
        liked = s.query(LikedVideo).all()
        assert len(liked) == 30 and all(lv.raw_json is None for lv in liked)
        row = s.query(TakeoutImportSession).filter(TakeoutImportSession.job_id == jid).one()
        assert row.meta.get("store_raw_json") is False
        assert row.meta.get("raw_json_stored_count") == 0
        assert row.meta.get("raw_json_skipped_count") == 30


# --------------------------------------------------------------------------- #
# DB stats
# --------------------------------------------------------------------------- #
def test_db_stats_service_counts_raw_json(settings, session):
    name = _ma_zip(settings.takeout_import_root, "stats.zip", n_liked=8, n_watch=0)
    tk.run_import_liked_videos(session, settings, name)  # raw_json on
    session.commit()
    st = dbs.db_stats(session)
    assert st["dialect"] == "sqlite"
    assert st["liked_videos"] == 8 and st["videos"] >= 8
    assert st["raw_json_stored"]["liked_videos"] == 8
    assert st["raw_json_stored_total"] == 8
    assert st["total_size_mb"] is not None and st["total_size_mb"] > 0
    assert "liked_videos" in st["table_counts"]


def test_db_stats_api(client, settings):
    name = _ma_zip(settings.takeout_import_root, "stats_api.zip", n_liked=5, n_watch=0)
    with session_scope() as s:
        tk.run_import_liked_videos(s, get_settings(), name, store_raw_json=False)
        s.commit()
    r = client.get("/api/storage/db-stats").json()
    assert r["dialect"] == "sqlite" and r["liked_videos"] == 5
    assert r["raw_json_stored_total"] == 0  # imported with no-raw-json


def test_db_stats_cli(settings):
    name = _ma_zip(settings.takeout_import_root, "stats_cli.zip", n_liked=4, n_watch=0)
    with session_scope() as s:
        tk.run_import_liked_videos(s, get_settings(), name)
        s.commit()
    res = CliRunner().invoke(cli_app, ["storage", "db-stats"])
    assert res.exit_code == 0, res.output
    assert "db stats" in res.output and "raw_json stored" in res.output


# --------------------------------------------------------------------------- #
# benchmark-large
# --------------------------------------------------------------------------- #
def test_benchmark_large_service_no_write(settings, session):
    name = _ma_zip(settings.takeout_import_root, "bl.zip", n_liked=10, n_watch=8)
    bl = tk.benchmark_large(session, settings, name)
    assert set(bl["results"]) == {"liked_videos", "watch_history"}
    assert bl["dry_run"] is True and bl["recommended_batch_size"] >= 500
    for b in bl["results"].values():
        assert b["dry_run"] is True
        assert b["entries_per_second"] is not None and b["peak_memory_mb"] is not None
        assert b["estimated_full_import_time_seconds"] is not None
        assert b["recommended_batch_size"] >= 500
    # full-scan dry-run wrote nothing
    assert session.query(LikedVideo).count() == 0
    assert session.query(WatchHistoryEvent).count() == 0
    # no personal content / raw_json leaked into the benchmark payload
    assert '"raw_json"' not in json.dumps(bl)


def test_benchmark_large_include_search(settings, session):
    name = _ma_zip(settings.takeout_import_root, "bls.zip", n_liked=4, n_watch=4, n_search=5)
    bl = tk.benchmark_large(session, settings, name, include_search=True)
    assert "search_history" in bl["results"]


def test_benchmark_large_api(client, settings):
    name = _ma_zip(settings.takeout_import_root, "bl_api.zip", n_liked=6, n_watch=6)
    r = client.post("/api/takeout/benchmark-large", json={"path": name}).json()
    assert r["dry_run"] is True and r["parser_backend"] in ("ijson", "json")
    assert "liked_videos" in r["results"] and "watch_history" in r["results"]
    with session_scope() as s:
        assert s.query(LikedVideo).count() == 0  # API benchmark wrote nothing


# --------------------------------------------------------------------------- #
# session retention / cleanup
# --------------------------------------------------------------------------- #
def _make_sessions(n: int, settings, *, n_liked=4) -> None:
    for i in range(n):
        name = _ma_zip(settings.takeout_import_root, f"c{i}.zip", n_liked=n_liked, n_watch=0)
        with session_scope() as s:
            tk.run_import_liked_videos(s, get_settings(), name)
            s.commit()


def test_cleanup_no_bounds_deletes_nothing(settings):
    _make_sessions(3, settings)
    with session_scope() as s:
        res = tk.cleanup_import_sessions(s, dry_run=False)
        s.commit()
    assert res["matched"] == 0 and res["deleted"] == 0
    with session_scope() as s:
        assert s.query(TakeoutImportSession).count() == 3


def test_cleanup_dry_run_keeps_all(settings):
    _make_sessions(4, settings)
    with session_scope() as s:
        res = tk.cleanup_import_sessions(s, keep_last=1, dry_run=True)
    assert res["matched"] == 3 and res["deleted"] == 0 and res["dry_run"] is True
    with session_scope() as s:
        assert s.query(TakeoutImportSession).count() == 4  # nothing actually removed


def test_cleanup_apply_preserves_jobs_and_imported_data(settings):
    # two job-backed imports (each creates a Job + a session + liked rows)
    job_ids = []
    for i in range(2):
        name = _ma_zip(settings.takeout_import_root, f"cj{i}.zip", n_liked=6, n_watch=0)
        with session_scope() as s:
            job, _row = tk.create_import_job(s, get_settings(), import_kind="liked_videos", path=name)
            s.commit()
            job_ids.append(job.id)
        worker.run_job(job_ids[-1])
    # plus a plain (non-job) import session
    _make_sessions(1, settings, n_liked=6)

    with session_scope() as s:
        jobs_before = s.query(Job).count()
        liked_before = s.query(LikedVideo).count()
        sessions_before = s.query(TakeoutImportSession).count()
    # every import (job-backed or plain) records a synchronous takeout_import Job
    assert sessions_before == 3 and jobs_before >= 2 and liked_before > 0

    with session_scope() as s:
        res = tk.cleanup_import_sessions(s, keep_last=1, dry_run=False)
        s.commit()
    assert res["deleted"] == 2 and res["kept"] == 1
    assert res["jobs_preserved"] >= 1  # deleted sessions referenced jobs

    with session_scope() as s:
        assert s.query(TakeoutImportSession).count() == 1          # only sessions pruned
        assert s.query(Job).count() == jobs_before                 # jobs untouched
        assert s.query(LikedVideo).count() == liked_before         # imported data untouched


def test_cleanup_never_deletes_running_session(settings):
    # oldest row is a still-running job session; then two finished imports
    name = _ma_zip(settings.takeout_import_root, "crun.zip", n_liked=4, n_watch=0)
    with session_scope() as s:
        _job, row = tk.create_import_job(s, get_settings(), import_kind="liked_videos", path=name)
        s.commit()
        running_sid = row.session_id
        assert row.status == "running"
    _make_sessions(2, settings)

    with session_scope() as s:
        res = tk.cleanup_import_sessions(s, keep_last=1, dry_run=False)
        s.commit()
    # only the middle finished session is deletable (newest kept, running excluded)
    assert res["deleted"] == 1
    with session_scope() as s:
        assert tk.get_import_session(s, running_sid) is not None   # running survived


def test_cleanup_api(client, settings):
    _make_sessions(3, settings)
    r = client.post("/api/takeout/import-sessions/cleanup",
                    json={"keep_last": 1, "dry_run": True}).json()
    assert r["matched"] == 2 and r["deleted"] == 0
    r2 = client.post("/api/takeout/import-sessions/cleanup",
                     json={"keep_last": 1, "dry_run": False}).json()
    assert r2["deleted"] == 2
    with session_scope() as s:
        assert s.query(TakeoutImportSession).count() == 1


# --------------------------------------------------------------------------- #
# --safe-large preset
# --------------------------------------------------------------------------- #
def test_safe_large_defaults_unit():
    # not safe-large: pass through job/dry_run; store_raw_json = not no_raw_json
    assert _safe_large_defaults(False, 0, False, False, False, False) == (False, False, True, False)
    assert _safe_large_defaults(False, 0, False, True, True, True) == (True, True, False, False)
    # safe-large, no --limit -> benchmark-only, job, dry-run, no-raw-json
    assert _safe_large_defaults(True, 0, False, False, False, False) == (True, True, False, True)
    # safe-large with --limit -> not benchmark-only, still dry-run unless --apply
    assert _safe_large_defaults(True, 500, False, False, False, False) == (True, True, False, False)
    assert _safe_large_defaults(True, 500, True, False, False, False) == (True, False, False, False)


def test_cli_safe_large_benchmark_only(settings):
    name = _ma_zip(settings.takeout_import_root, "sl.zip", n_liked=8, n_watch=0)
    res = CliRunner().invoke(cli_app, ["takeout", "import-liked-videos", name, "--safe-large"])
    assert res.exit_code == 0, res.output
    assert "safe-large benchmark" in res.output
    with session_scope() as s:
        assert s.query(LikedVideo).count() == 0  # benchmark-only: nothing imported
