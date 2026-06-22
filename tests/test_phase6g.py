"""Phase 6G tests: staged production import + resume/rerun safety + operation
report.

import-staged runs cumulative stages (dry-run plan by default; --apply executes
with verify + db-stats between stages; the full stage is gated by allow_full).
Re-running is dedup-safe (no destructive changes). The report never exposes
raw_json bodies / secrets / host paths.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import LikedVideo, TakeoutImportSession, WatchHistoryEvent
from app.services import takeout as tk


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _ma_zip(root: Path, name: str, *, n_liked=120, n_watch=120) -> str:
    acts = [{"title": f"高く評価した動画: L{i}",
             "titleUrl": f"https://www.youtube.com/watch?v=p6gl{i:07d}",
             "time": f"2025-01-{(i % 28) + 1:02d}T00:00:00Z",
             "subtitles": [{"name": f"C{i}"}]} for i in range(n_liked)]
    acts += [{"title": f"視聴済み: W{i}",
              "titleUrl": f"https://www.youtube.com/watch?v=p6gw{i:07d}",
              "time": f"2025-02-{(i % 28) + 1:02d}T00:00:00Z"} for i in range(n_watch)]
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / name, "w") as z:
        z.writestr("Takeout/マイ アクティビティ/YouTube/MyActivity.json",
                   json.dumps(acts, ensure_ascii=False))
    return name


# --------------------------------------------------------------------------- #
# import-staged dry-run
# --------------------------------------------------------------------------- #
def test_import_staged_dry_run_no_writes(settings):
    name = _ma_zip(settings.takeout_import_root, "sd.zip", n_liked=120, n_watch=0)
    res = tk.import_staged(settings, name, kind="liked_videos", apply=False, skip_preflight=True)
    assert res["ok"] is True and res["dry_run"] is True
    assert res["plan"]["liked_videos"] == [100, 1000, 5000, "full"]
    assert any(s["stage"] == "benchmark" for s in res["stages"])
    with session_scope() as s:
        assert s.query(LikedVideo).count() == 0  # dry-run wrote nothing


def test_import_staged_dry_run_default_is_dry_run(settings):
    name = _ma_zip(settings.takeout_import_root, "sd2.zip", n_liked=50, n_watch=0)
    # no apply flag -> dry-run
    res = tk.import_staged(settings, name, kind="liked_videos", skip_preflight=True)
    assert res["dry_run"] is True and res["store_raw_json"] is False  # no-raw-json default


# --------------------------------------------------------------------------- #
# import-staged apply (small fixture, synchronous stages)
# --------------------------------------------------------------------------- #
def test_import_staged_apply_liked(settings):
    name = _ma_zip(settings.takeout_import_root, "sa.zip", n_liked=150, n_watch=0)
    res = tk.import_staged(
        settings, name, kind="liked_videos", apply=True, as_job=False,
        skip_preflight=True, max_stage=2,  # stages: 100, then 1000 (cumulative)
    )
    assert res["ok"] is True and res["dry_run"] is False
    stages = [s for s in res["stages"] if s.get("session_id")]
    assert len(stages) == 2
    assert stages[0]["limit"] == 100 and stages[0]["imported"] == 100
    assert stages[1]["limit"] == 1000 and stages[1]["imported"] == 50  # 100 dupes skipped
    # per-stage verify + db-size recorded
    assert all(s["verify_ok"] for s in stages)
    assert all(s["db_size_mb_before"] is not None and s["db_size_mb_after"] is not None for s in stages)
    with session_scope() as s:
        rows = s.query(LikedVideo).all()
        assert len(rows) == 150 and all(lv.raw_json is None for lv in rows)  # no-raw-json default


def test_import_staged_apply_watch(settings):
    name = _ma_zip(settings.takeout_import_root, "saw.zip", n_liked=0, n_watch=200)
    res = tk.import_staged(
        settings, name, kind="watch_history", apply=True, as_job=False,
        skip_preflight=True, max_stage=1,  # first stage = 1000 (caps at 200 available)
    )
    assert res["ok"] is True
    st = [s for s in res["stages"] if s.get("session_id")][0]
    assert st["imported"] == 200 and st["raw_json_skipped"] == 200
    with session_scope() as s:
        assert s.query(WatchHistoryEvent).count() == 200


def test_import_staged_apply_all(settings):
    name = _ma_zip(settings.takeout_import_root, "saa.zip", n_liked=80, n_watch=90)
    res = tk.import_staged(
        settings, name, kind="all", apply=True, as_job=False, skip_preflight=True, max_stage=1,
    )
    assert res["ok"] is True
    kinds = {s["kind"] for s in res["stages"] if s.get("session_id")}
    assert kinds == {"liked_videos", "watch_history"}
    with session_scope() as s:
        assert s.query(LikedVideo).count() == 80
        # In My Activity, liked ('高く評価') entries are also watch-type activities,
        # so watch import sees all 80+90=170 activities (Phase 6C/6D behavior).
        assert s.query(WatchHistoryEvent).count() == 170


def test_import_staged_full_gated_by_allow_full(settings):
    name = _ma_zip(settings.takeout_import_root, "sfull.zip", n_liked=30, n_watch=0)
    res = tk.import_staged(
        settings, name, kind="liked_videos", apply=True, as_job=False, skip_preflight=True,
    )
    # the final (full) stage must be SKIPPED without allow_full
    assert any(s["status"] == "skipped_needs_allow_full" for s in res["stages"])
    assert "allow-full" in (res["recommended_next"] or "")


# --------------------------------------------------------------------------- #
# resume / rerun safety
# --------------------------------------------------------------------------- #
def test_import_staged_rerun_is_dedup_safe(settings):
    name = _ma_zip(settings.takeout_import_root, "rr.zip", n_liked=120, n_watch=0)
    first = tk.import_staged(settings, name, kind="liked_videos", apply=True, as_job=False,
                             skip_preflight=True, max_stage=1)
    assert first["ok"]
    with session_scope() as s:
        n_after_first = s.query(LikedVideo).count()
    # rerun: prior sessions surfaced, no destructive change, duplicates skipped
    second = tk.import_staged(settings, name, kind="liked_videos", apply=True, as_job=False,
                              skip_preflight=True, max_stage=1)
    assert len(second["prior_sessions"]) >= 1  # detected the prior session
    with session_scope() as s:
        assert s.query(LikedVideo).count() == n_after_first  # unchanged (dedup)


def test_cli_import_staged_dry_run(settings):
    name = _ma_zip(settings.takeout_import_root, "cli.zip", n_liked=60, n_watch=0)
    res = CliRunner().invoke(
        cli_app, ["takeout", "import-staged", name, "--kind", "liked_videos", "--skip-preflight"]
    )
    assert res.exit_code == 0, res.output
    assert "dry_run=True" in res.output and "store_raw_json=False" in res.output
    with session_scope() as s:
        assert s.query(LikedVideo).count() == 0


# --------------------------------------------------------------------------- #
# operation report
# --------------------------------------------------------------------------- #
def test_import_report_single(settings):
    name = _ma_zip(settings.takeout_import_root, "rep.zip", n_liked=40, n_watch=0)
    with session_scope() as s:
        tk.run_import_liked_videos(s, get_settings(), name, store_raw_json=False)
        s.commit()
        sid = tk.list_import_sessions(s)[0].session_id
        r = tk.import_report(s, get_settings(), session_id=sid)
    assert r["ok"] is True and r["session_id"] == sid
    assert r["recommended_next_action"] and r["leak_check_ok"] is True
    assert r["raw_json_real_blobs"]["liked_videos"] == 0


def test_import_report_recent_list(settings):
    name = _ma_zip(settings.takeout_import_root, "repr.zip", n_liked=20, n_watch=0)
    for _ in range(3):
        with session_scope() as s:
            tk.run_import_liked_videos(s, get_settings(), name, dry_run=True)
            s.commit()
    with session_scope() as s:
        rl = tk.import_report(s, get_settings(), kind="liked_videos", recent=3)
    assert rl["count"] == 3 and all("recommended_next_action" in x for x in rl["reports"])


def test_import_report_api_latest_and_by_id(client, settings):
    name = _ma_zip(settings.takeout_import_root, "repapi.zip", n_liked=15, n_watch=0)
    with session_scope() as s:
        tk.run_import_liked_videos(s, get_settings(), name, store_raw_json=False)
        s.commit()
        sid = tk.list_import_sessions(s)[0].session_id
    r = client.get("/api/takeout/import-report/latest").json()
    assert r["ok"] is True and r["session_id"] == sid
    r2 = client.get(f"/api/takeout/import-report/{sid}").json()
    assert r2["session_id"] == sid and "recommended_next_action" in r2
    assert client.get("/api/takeout/import-report/nope").status_code == 404


def test_import_report_no_leak(client, settings):
    name = _ma_zip(settings.takeout_import_root, "repleak.zip", n_liked=15, n_watch=0)
    with session_scope() as s:
        tk.run_import_liked_videos(s, get_settings(), name, store_raw_json=False)
        s.commit()
    blob = client.get("/api/takeout/import-report/latest").text
    # the report exposes no raw blob key, host path, or secret material
    for needle in ('"raw_json":', "/Users/", "/takeout_imports/", "titleUrl", "po_token", "cookie"):
        assert needle not in blob


def test_cli_import_report_latest(settings):
    name = _ma_zip(settings.takeout_import_root, "clirep.zip", n_liked=12, n_watch=0)
    with session_scope() as s:
        tk.run_import_liked_videos(s, get_settings(), name, store_raw_json=False)
        s.commit()
    res = CliRunner().invoke(cli_app, ["takeout", "import-report", "--latest"])
    assert res.exit_code == 0, res.output
    assert "import-report" in res.output and "next:" in res.output
    assert "leak_check: clean" in res.output
