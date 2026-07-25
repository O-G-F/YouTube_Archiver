"""Phase 11B: separate the running runtime from the last scanned release.

A stale release manifest must never be presented as the current runtime's scan.
The comparison exposes build-id match/mismatch/no-manifest verdicts + ages, and
carries no host paths or secrets.
"""

from __future__ import annotations

from app.config import get_settings
from app.services import production_check as pc


def _v(build="src:RUNTIME"):
    return {"app_version": "0.1.0", "build_id": build, "git_commit": "3d4fca8fde0217e0",
            "schema_head": "e5f6a7b8c9d0", "git_tree_clean": None}


def test_runtime_matches_release():
    m = {"release_id": "rel-20260719114809-x", "app_version": "v1", "build_id": "src:SAME"}
    r = pc._runtime_release_status(_v("src:SAME"), m)
    assert r["verdict"] == "match" and r["manifest_matches_runtime"] is True
    assert "matches the scanned release" in r["message"]


def test_runtime_mismatch_when_dev_build_differs():
    m = {"release_id": "rel-20260719114809-x", "app_version": "v0.10.0-rc1", "build_id": "src:RELEASE"}
    r = pc._runtime_release_status(_v("src:RUNTIME"), m)
    assert r["verdict"] == "mismatch" and r["manifest_matches_runtime"] is False
    assert r["runtime_build_id"] == "src:RUNTIME" and r["manifest_build_id"] == "src:RELEASE"
    assert "differs from the last scanned release" in r["message"]
    # ages are derived from the release id / scan timestamp (best-effort)
    assert r["manifest_age_seconds"] is not None and r["manifest_age_seconds"] > 0


def test_no_scanned_release_when_no_manifest():
    r = pc._runtime_release_status(_v(), None)
    assert r["verdict"] == "no_scanned_release" and r["status_source"] == "none"
    assert r["manifest_build_id"] is None and r["manifest_matches_runtime"] is False


def test_release_readiness_includes_runtime_release_and_posture():
    get_settings.cache_clear()
    d = pc.release_readiness(get_settings())
    assert "runtime_release" in d and isinstance(d["runtime_release"], dict)
    assert d["runtime_release"]["verdict"] in ("match", "mismatch", "no_scanned_release")
    # 11A posture kept
    assert d["security_posture"]["known_critical_accepted"] == 7


def test_runtime_release_no_leak():
    from app.services.vuln_triage import scan_text_for_host_leaks
    import json
    m = {"release_id": "rel-20260719114809-x", "app_version": "v1", "build_id": "src:X",
         "vulnerability_db_updated_at": "2026-07-19T12:54:38Z"}
    r = pc._runtime_release_status(_v(), m)
    assert scan_text_for_host_leaks(json.dumps(r), repo_root="/some/build/dir") == []


def test_schema_serializes_runtime_release():
    from app.schemas import ReleaseReadinessOut
    get_settings.cache_clear()
    out = ReleaseReadinessOut(**pc.release_readiness(get_settings()))
    assert out.runtime_release is not None
    assert out.runtime_release.verdict in ("match", "mismatch", "no_scanned_release")


# ---- first-run setup checklist -------------------------------------------- #
def test_first_run_status_fresh(settings, session):
    d = pc.first_run_status(session, settings)
    assert d["is_fresh"] is True and d["video_count"] == 0 and d["job_count"] == 0
    keys = {i["key"] for i in d["items"]}
    assert {"storage", "auth", "cookies", "takeout", "metadata", "download_policy", "backup"} <= keys
    # every link is a safe in-app route — never a dangerous auto-run command
    assert all(i["link"].startswith("/") for i in d["items"])
    # optional items are marked optional (no false "must do" pressure)
    assert next(i for i in d["items"] if i["key"] == "cookies")["optional"] is True


def test_first_run_auth_disabled_warns(settings, session):
    d = pc.first_run_status(session, settings)  # test settings default AUTH_MODE=disabled
    assert d["auth_mode"] == "disabled" and d["exposure_warning"] is True
    auth = next(i for i in d["items"] if i["key"] == "auth")
    assert auth["done"] is False and auth.get("warn") is True
    assert "LAN" in d["exposure_note"] or "internet" in d["exposure_note"]


def test_first_run_no_leak(settings, session):
    import json

    from app.services.vuln_triage import scan_text_for_host_leaks
    d = pc.first_run_status(session, settings)
    assert scan_text_for_host_leaks(json.dumps(d), repo_root="/some/build/dir") == []


def test_first_run_schema_serializes(settings, session):
    from app.schemas import FirstRunStatusOut
    out = FirstRunStatusOut(**pc.first_run_status(session, settings))
    assert out.total_count == len(out.items) and out.total_count >= 5
