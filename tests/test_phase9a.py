"""Phase 9A: production body-archive operation controls.

Disk capacity guard, per-video size estimator, production default profile,
batch planning, and consolidated operations status — verified WITHOUT any real
video download (uses seeded media_files + a monkeypatched disk_usage so the
outcome never depends on the host's free space).
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.models import Job, LikedVideo, MediaFile, Video
from app.services import liked_archive as la
from app.services import storage

GIB = 1024 ** 3
MIB = 1024 ** 2


# --------------------------------------------------------------------------- #
# seed helpers
# --------------------------------------------------------------------------- #
def _video(s, vid, *, body=False, filesize=None, info=False):
    v = Video(
        youtube_video_id=vid,
        url=f"https://www.youtube.com/watch?v={vid}",
        title="t",
        channel_title="Chan",
        first_seen_at=datetime(2025, 1, 1),
    )
    s.add(v)
    s.flush()
    if body:
        s.add(MediaFile(video_id=v.id, media_type="video", path=f"{vid}.mp4",
                        profile="video_compressed_1080p_light", filesize=filesize))
    if info:
        s.add(MediaFile(video_id=v.id, media_type="info_json", path=f"{vid}.info.json"))
    s.flush()
    return v


def _liked(s, vid, *, video=None, liked_at=None):
    lv = LikedVideo(
        source="takeout_my_activity",
        youtube_video_id=vid,
        title="t",
        channel_title="Chan",
        url=f"https://youtu.be/{vid}",
        liked_at=liked_at or datetime(2025, 1, 1),
        video_id=video.id if video else None,
    )
    s.add(lv)
    s.flush()
    return lv


def _patch_disk(monkeypatch, *, free_gb=None, readable=True, total_gb=1600.0):
    """Force storage.disk_usage() to a deterministic value (no host dependency)."""
    def fake(settings, path=None):
        if not readable:
            return {"readable": False, "total_bytes": None, "used_bytes": None,
                    "free_bytes": None, "total_gb": None, "used_gb": None,
                    "free_gb": None, "used_percent": None}
        free = int(free_gb * GIB)
        total = int(total_gb * GIB)
        return {"readable": True, "total_bytes": total, "used_bytes": total - free,
                "free_bytes": free, "total_gb": total_gb,
                "used_gb": round((total - free) / GIB, 2), "free_gb": round(free_gb, 2),
                "used_percent": round((total - free) / total * 100, 1)}
    monkeypatch.setattr(storage, "disk_usage", fake)


# --------------------------------------------------------------------------- #
# 1. production default profile (comments-light), legacy kept
# --------------------------------------------------------------------------- #
def test_default_body_profile_is_comments_light(settings):
    assert settings.effective_body_archive_profile == "video_compressed_1080p_light"
    assert settings.liked_archive_default_profile == "video_compressed_1080p"  # legacy kept
    # scheduler body pass also defaults to the production body profile
    assert settings.effective_scheduler_liked_archive_profile == "video_compressed_1080p_light"


def test_enqueue_archive_defaults_to_comments_light(settings, session, monkeypatch):
    _patch_disk(monkeypatch, free_gb=1000.0)
    v = _video(session, "vidlight0001", info=True)
    _liked(session, "vidlight0001", video=v)
    session.commit()
    r = la.enqueue_archive(session, settings, filters=la.LikedFilters(missing_body=True),
                           limit=5, submit=False)
    assert r.profile == "video_compressed_1080p_light"
    assert r.jobs_created == 1
    assert session.get(Job, r.job_ids[0]).profile_name == "video_compressed_1080p_light"


# --------------------------------------------------------------------------- #
# 3. size estimator: measured from media_files / conservative / fallback
# --------------------------------------------------------------------------- #
def test_size_estimate_fallback_when_insufficient(settings, session):
    for i in range(3):  # < archive_size_estimate_min_samples (10) -> fallback
        _video(session, f"vidsmall{i:04d}", body=True, filesize=100 * MIB)
    session.commit()
    est = la.video_size_estimate(session, settings)
    assert est["source"] == "fallback"
    assert est["sample_count"] == 3
    assert est["estimate_mb"] == pytest.approx(settings.archive_size_estimate_fallback_mb)


def test_size_estimate_measured_is_conservative(settings, session):
    for i in range(20):  # >= min samples -> measured
        _video(session, f"vidmeas{i:05d}", body=True, filesize=(100 + i * 10) * MIB)
    session.commit()
    est = la.video_size_estimate(session, settings)
    assert est["source"] == "measured"
    assert est["sample_count"] == 20
    assert est["p90_mb"] >= est["median_mb"]      # p90 not below median
    assert est["estimate_mb"] >= est["avg_mb"]    # conservative: err large


# --------------------------------------------------------------------------- #
# 2. disk capacity guard: allow / block / override / unreadable
# --------------------------------------------------------------------------- #
def test_capacity_allows_when_enough_free(settings, session, monkeypatch):
    _patch_disk(monkeypatch, free_gb=1000.0)
    cap = la.capacity_plan(session, settings, selected_count=100, min_free_gb=500.0)
    assert cap["disk_readable"] is True
    assert cap["blocked"] is False
    assert cap["disk_safe_limit"] > 0


def test_capacity_blocks_when_would_go_below_min_free(settings, session, monkeypatch):
    _patch_disk(monkeypatch, free_gb=520.0)  # only 20 GiB above the 500 GiB floor
    cap = la.capacity_plan(session, settings, selected_count=1000, min_free_gb=500.0)
    assert cap["blocked"] is True
    assert cap["would_go_below_min_free"] is True
    assert "min-free" in cap["block_reason"]


def test_capacity_override_allows_low_disk(settings, session, monkeypatch):
    _patch_disk(monkeypatch, free_gb=520.0)
    cap = la.capacity_plan(session, settings, selected_count=1000, min_free_gb=500.0,
                           allow_low_disk=True)
    assert cap["blocked"] is False
    assert "OVERRIDDEN" in (cap["block_reason"] or "")


def test_capacity_unreadable_never_blocks(settings, session, monkeypatch):
    _patch_disk(monkeypatch, readable=False)
    cap = la.capacity_plan(session, settings, selected_count=100_000, min_free_gb=500.0)
    assert cap["disk_readable"] is False
    assert cap["blocked"] is False
    assert cap["disk_safe_limit"] is None


# --------------------------------------------------------------------------- #
# 4. batch planning surfaced by plan-archive
# --------------------------------------------------------------------------- #
def test_plan_batch_and_disk_fields(settings, session, monkeypatch):
    _patch_disk(monkeypatch, free_gb=1000.0)
    for i in range(3):  # 3 eligible (missing body, has info_json, not permanent)
        v = _video(session, f"videlig{i:04d}", info=True)
        _liked(session, f"videlig{i:04d}", video=v)
    vb = _video(session, "vidhasbody99", body=True, filesize=200 * MIB)
    _liked(session, "vidhasbody99", video=vb)
    session.commit()
    plan = la.archive_plan(session, settings, filters=la.LikedFilters(), limit=2)
    assert plan.requested_limit == 2
    assert plan.cap_per_run == settings.liked_archive_max_enqueue_per_run
    assert plan.eligible_missing_body == 3
    assert plan.selected_count == 2                       # min(requested 2, cap, eligible 3)
    assert plan.recommended_profile == "video_compressed_1080p_light"
    assert plan.disk_readable is True
    assert plan.disk_free_gb == 1000.0
    assert plan.estimated_free_after_gb is not None
    assert plan.disk_safe_limit is not None and plan.disk_safe_limit > 0
    assert plan.blocked is False
    assert plan.limiting_factor in {"requested", "cap", "eligible", "disk"}


def test_plan_blocked_when_disk_nearly_full(settings, session, monkeypatch):
    _patch_disk(monkeypatch, free_gb=0.5)  # < one ~300 MiB fallback-sized batch of 3
    for i in range(3):
        v = _video(session, f"vidblk{i:05d}", info=True)
        _liked(session, f"vidblk{i:05d}", video=v)
    session.commit()
    plan = la.archive_plan(session, settings, filters=la.LikedFilters(), limit=3)
    assert plan.blocked is True
    assert plan.block_reason and "min-free" in plan.block_reason
    assert plan.limiting_factor == "disk"
    assert plan.recommended_limit <= 1


# --------------------------------------------------------------------------- #
# 2. enqueue guard refuses a real run unless overridden
# --------------------------------------------------------------------------- #
def test_enqueue_blocked_creates_no_jobs(settings, session, monkeypatch):
    _patch_disk(monkeypatch, free_gb=0.5)
    v = _video(session, "vidnope00001", info=True)
    _liked(session, "vidnope00001", video=v)
    session.commit()
    r = la.enqueue_archive(session, settings, filters=la.LikedFilters(missing_body=True),
                           limit=3, submit=False, min_free_gb=500.0)
    assert r.blocked is True
    assert r.jobs_created == 0
    assert r.block_reason


def test_enqueue_allow_low_disk_overrides(settings, session, monkeypatch):
    _patch_disk(monkeypatch, free_gb=0.5)
    v = _video(session, "vidyes000001", info=True)
    _liked(session, "vidyes000001", video=v)
    session.commit()
    r = la.enqueue_archive(session, settings, filters=la.LikedFilters(missing_body=True),
                           limit=3, submit=False, min_free_gb=500.0, allow_low_disk=True)
    assert r.blocked is False
    assert r.jobs_created == 1


# --------------------------------------------------------------------------- #
# 5. consolidated operations status returns disk/orphan/dup/raw_json/comments
# --------------------------------------------------------------------------- #
def test_operations_status_fields(settings, session, monkeypatch):
    _patch_disk(monkeypatch, free_gb=800.0)
    v1 = _video(session, "vidops00001", body=True, filesize=200 * MIB)
    _liked(session, "vidops00001", video=v1)
    v2 = _video(session, "vidops00002", info=True)  # missing body -> eligible
    _liked(session, "vidops00002", video=v2)
    session.commit()
    st = la.operations_status(session, settings)
    assert st["default_body_profile"] == "video_compressed_1080p_light"
    assert st["body_saved"] == 1
    assert st["remaining_eligible_body"] == 1
    assert st["duplicate_video_media_files"] == 0
    assert st["raw_json_stored_total"] == 0
    assert st["disk"]["readable"] is True and st["disk"]["free_gb"] == 800.0
    assert set(st["orphan"]) >= {"scanned", "orphan_found", "rq_unreadable"}
    assert st["size_estimate"]["source"] in {"measured", "fallback"}
    assert st["comments_table_bytes"] == 0  # sqlite: table sizes not reported / empty


# --------------------------------------------------------------------------- #
# 8. no secret / host-path / raw_json content leaks through status or plan
# --------------------------------------------------------------------------- #
def test_status_and_plan_do_not_leak_paths(settings, session, monkeypatch):
    _patch_disk(monkeypatch, free_gb=800.0)
    monkeypatch.setenv("COOKIES_FILE", "/secrets/cookies.txt")
    v = _video(session, "vidleak00001", info=True)
    _liked(session, "vidleak00001", video=v)
    session.commit()
    st = la.operations_status(session, settings)
    plan = la.archive_plan(session, settings, filters=la.LikedFilters(), limit=1)
    blob = json.dumps(st) + json.dumps(plan.__dict__, default=str)
    for bad in ("/secrets/", "/Users/", "cookies.txt", str(settings.archive_root)):
        assert bad not in blob
