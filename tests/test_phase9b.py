"""Phase 9B: production deployment readiness.

production-check (PASS/WARN/FAIL) + archive-root migration guard, verified WITHOUT
any real download and without Redis/Postgres (infra checks are monkeypatched to a
healthy state; data checks run against the sqlite test DB).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.config import get_settings
from app.models import LikedVideo, MediaFile, Video
from app.services import preflight as pf
from app.services import production_check as pc
from app.services import queue_health, reconcile, storage

GIB = 1024 ** 3
REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# seed + monkeypatch helpers
# --------------------------------------------------------------------------- #
def _video(s, vid, *, video_files=0, present_paths=False, root: Path | None = None):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
              title="t", channel_title="C", first_seen_at=datetime(2025, 1, 1))
    s.add(v)
    s.flush()
    for i in range(video_files):
        rel = f"chan/{vid}/{i}.mp4"
        if present_paths and root is not None:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        s.add(MediaFile(video_id=v.id, media_type="video", path=rel,
                        profile="video_compressed_1080p_light"))
    s.flush()
    return v


def _healthy_settings(monkeypatch):
    """A settings object that passes the config-only checks (server DB, high
    min-free, restricted CORS, INFO logging). The session stays sqlite."""
    s = get_settings()
    monkeypatch.setattr(s, "database_url", "postgresql+psycopg2://prod")
    monkeypatch.setattr(s, "archive_min_free_gb", 500.0)
    monkeypatch.setattr(s, "cors_allow_origins", "http://localhost:8000")
    monkeypatch.setattr(s, "log_level", "INFO")
    return s


def _fake_disk(free_gb=1000.0, total_gb=1600.0, readable=True):
    def _fn(settings, path=None):
        if not readable:
            return {"readable": False, "total_bytes": None, "used_bytes": None,
                    "free_bytes": None, "total_gb": None, "used_gb": None,
                    "free_gb": None, "used_percent": None}
        return {"readable": True, "total_bytes": int(total_gb * GIB),
                "used_bytes": int((total_gb - free_gb) * GIB), "free_bytes": int(free_gb * GIB),
                "total_gb": total_gb, "used_gb": round(total_gb - free_gb, 2),
                "free_gb": round(free_gb, 2), "used_percent": round((total_gb - free_gb) / total_gb * 100, 1)}
    return _fn


def _patch_infra(monkeypatch, *, free_gb=1000.0, aof=True, worker=1, orphans=0, disk_readable=True):
    monkeypatch.setattr(pf, "system_preflight", lambda *a, **k: {
        "ok": True, "workers": [], "build_info": {"build_id": "x"},
        "checks": [{"name": n, "status": "ok", "detail": "ok"} for n in
                   ("db_connect", "redis_connect", "schema_head", "worker_build_match",
                    "worker_build_id", "cookies_file", "secret_value_exposed")]})
    monkeypatch.setattr(pc, "_redis_aof_enabled", lambda s: aof)
    monkeypatch.setattr(storage, "disk_usage", _fake_disk(free_gb=free_gb, readable=disk_readable))
    monkeypatch.setattr(queue_health, "queue_status", lambda s: {
        "queued": 0, "running": 0, "total_active": 0, "worker_count": worker,
        "by_type": {}, "by_source_action": {}})
    monkeypatch.setattr(reconcile, "reconcile_orphans", lambda *a, **k: {
        "rq_unreadable": False, "orphan_found": orphans, "scanned": orphans})


def _find(r, name):
    return next(c for c in r["checks"] if c["name"] == name)


# --------------------------------------------------------------------------- #
# production-check
# --------------------------------------------------------------------------- #
def test_production_check_all_pass(settings, session, monkeypatch):
    _patch_infra(monkeypatch)
    s = _healthy_settings(monkeypatch)
    _video(session, "vidok000001", video_files=1)
    session.commit()
    r = pc.production_check(session, s)
    assert r["overall"] == "pass"
    assert r["counts"]["fail"] == 0 and r["counts"]["warn"] == 0
    assert _find(r, "default_body_profile")["status"] == "pass"


def test_production_check_disk_low_fails(settings, session, monkeypatch):
    _patch_infra(monkeypatch, free_gb=100.0)  # below 500 GiB min-free
    s = _healthy_settings(monkeypatch)
    r = pc.production_check(session, s)
    assert _find(r, "archive_disk_free")["status"] == "fail"
    assert r["overall"] == "fail"


def test_production_check_orphan_fails(settings, session, monkeypatch):
    _patch_infra(monkeypatch, orphans=1)
    s = _healthy_settings(monkeypatch)
    r = pc.production_check(session, s)
    assert _find(r, "orphan_jobs")["status"] == "fail"


def test_production_check_duplicate_fails(settings, session, monkeypatch):
    _patch_infra(monkeypatch)
    s = _healthy_settings(monkeypatch)
    _video(session, "viddup00001", video_files=2)  # two 'video' media -> duplicate
    session.commit()
    r = pc.production_check(session, s)
    assert _find(r, "duplicate_video_media")["status"] == "fail"


def test_production_check_raw_json_fails(settings, session, monkeypatch):
    _patch_infra(monkeypatch)
    s = _healthy_settings(monkeypatch)
    session.add(LikedVideo(source="takeout_my_activity", youtube_video_id="vidraw00001",
                           url="https://youtu.be/vidraw00001", raw_json={"x": 1}))
    session.commit()
    r = pc.production_check(session, s)
    assert _find(r, "raw_json_stored")["status"] == "fail"


def test_production_check_comments_profile_warns(settings, session, monkeypatch):
    _patch_infra(monkeypatch)
    s = _healthy_settings(monkeypatch)
    monkeypatch.setattr(s, "body_archive_default_profile", "video_compressed_1080p")  # comments-enabled
    r = pc.production_check(session, s)
    assert _find(r, "default_body_profile")["status"] == "warn"


def test_production_check_redis_persistence_missing_fails(settings, session, monkeypatch):
    _patch_infra(monkeypatch, aof=False)
    s = _healthy_settings(monkeypatch)
    r = pc.production_check(session, s)
    assert _find(r, "redis_aof_persistence")["status"] == "fail"


def test_production_check_no_secret_or_path_leak(settings, session, monkeypatch):
    _patch_infra(monkeypatch)
    s = _healthy_settings(monkeypatch)
    monkeypatch.setattr(s, "cookies_file", "/secrets/cookies.txt")
    r = pc.production_check(session, s)
    blob = json.dumps(r)
    for bad in ("/secrets/", "/Users/", "cookies.txt", str(settings.archive_root)):
        assert bad not in blob


# --------------------------------------------------------------------------- #
# archive-root migration guard
# --------------------------------------------------------------------------- #
def test_archive_media_check_file_exists(settings, session):
    _video(session, "vidfile0001", video_files=1, present_paths=True, root=settings.archive_root)
    session.commit()
    r = pc.archive_media_check(session, settings)
    assert r["db_video_media_files"] == 1
    assert r["existing"] == 1 and r["missing"] == 0
    assert r["ok"] is True


def test_archive_media_check_missing_file(settings, session):
    _video(session, "vidmiss0001", video_files=1, present_paths=False)  # path recorded, no file
    session.commit()
    r = pc.archive_media_check(session, settings)
    assert r["missing"] == 1 and r["existing"] == 0
    assert r["ok"] is False
    assert "vidmiss0001" in r["missing_youtube_ids"]


def test_archive_media_check_duplicate_flagged(settings, session):
    _video(session, "viddup20001", video_files=2, present_paths=True, root=settings.archive_root)
    session.commit()
    r = pc.archive_media_check(session, settings)
    assert r["duplicate_video_media_files"] == 1
    assert r["ok"] is False


def test_archive_media_check_no_path_leak(settings, session):
    _video(session, "vidmiss0002", video_files=1, present_paths=False)
    session.commit()
    r = pc.archive_media_check(session, settings)
    blob = json.dumps(r)
    assert str(settings.archive_root) not in blob
    assert "chan/" not in blob  # relative media paths are not exposed either


# --------------------------------------------------------------------------- #
# environment templates
# --------------------------------------------------------------------------- #
def test_env_production_example_required_and_no_dangerous_defaults():
    text = (REPO / ".env.production.example").read_text("utf-8")
    for key in ("DATABASE_URL", "REDIS_URL", "ARCHIVE_MIN_FREE_GB",
                "BODY_ARCHIVE_DEFAULT_PROFILE", "ARCHIVE_HOST_PATH"):
        assert key in text, f"{key} not documented in .env.production.example"
    # dangerous defaults must NOT be the active values
    lowered = [ln.strip() for ln in text.splitlines()
               if ln.strip() and not ln.strip().startswith("#")]
    active = "\n".join(lowered)
    assert "ARCHIVE_MIN_FREE_GB=0" not in active
    assert "LOG_LEVEL=DEBUG" not in active
    assert "CORS_ALLOW_ORIGINS=*" not in active
    # production body profile must be comments-light, not the comments-heavy one
    assert "BODY_ARCHIVE_DEFAULT_PROFILE=video_compressed_1080p_light" in active
    assert "BODY_ARCHIVE_DEFAULT_PROFILE=video_compressed_1080p\n" not in active + "\n"
