"""Phase 10A: release-candidate provenance + software supply-chain controls.

Version identity, versioned+integrity-hashed release manifest, dependency-lock
pinning, release-check provenance gates, and no path/secret/identity leaks.
No real downloads; never touches volumes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.services import build_info as bi
from app.services import release_manifest as rm

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()


def _clear_caches():
    bi.frontend_build_id.cache_clear()


# --------------------------------------------------------------------------- #
# 1. version identity (API/CLI, dirty policy, no leak)
# --------------------------------------------------------------------------- #
def test_version_info_shape_and_no_leak(settings, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "v1.2.3")
    monkeypatch.setenv("APP_GIT_COMMIT", "abcdef1234567890")
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "1")
    monkeypatch.setenv("APP_FRONTEND_BUILD_ID", "ui:deadbeef")
    _clear_caches()
    get_settings.cache_clear()
    v = bi.version_info()
    assert v["app_version"] == "v1.2.3" and v["git_tree_clean"] is True
    assert v["build_id"].startswith("src:") or v["build_id"]
    assert v["frontend_build_id"] == "ui:deadbeef"
    blob = json.dumps(v)
    for bad in ("/Users/", "/home/", str(REPO), "password", "secret", "token"):
        assert bad not in blob
    _clear_caches()


def test_version_api_and_cli(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod

    monkeypatch.setenv("APP_VERSION", "v9.9.9")
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "1")
    _clear_caches()
    get_settings.cache_clear()
    client = TestClient(main_mod.app)
    r = client.get("/api/system/version")
    assert r.status_code == 200 and r.json()["app_version"] == "v9.9.9"
    assert "git_tree_clean" in r.json()

    res = runner.invoke(cli_app, ["system", "version"])
    assert res.exit_code == 0 and "v9.9.9" in res.output
    assert "/Users/" not in res.output and str(REPO) not in res.output


def test_dirty_tree_policy_prod_fail_dev_warn(settings, session, monkeypatch):
    from app.services import production_check as pc

    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "0")  # dirty
    monkeypatch.setenv("APP_VERSION", "v1.0.0")
    _clear_caches()
    get_settings.cache_clear()
    # development -> WARN
    checks = {c["name"]: c["status"] for c in pc._release_provenance_checks(get_settings())}
    assert checks["git_tree_clean"] == "warn"

    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    checks = {c["name"]: c["status"] for c in pc._release_provenance_checks(get_settings())}
    assert checks["git_tree_clean"] == "fail"


def test_app_version_dev_placeholder_policy(settings, monkeypatch):
    from app.services import production_check as pc

    monkeypatch.setenv("APP_VERSION", "0.0.0-dev")
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "1")
    monkeypatch.setenv("APP_ENV", "production")
    _clear_caches()
    get_settings.cache_clear()
    checks = {c["name"]: c["status"] for c in pc._release_provenance_checks(get_settings())}
    assert checks["application_version"] == "fail"


# --------------------------------------------------------------------------- #
# 2. release manifest: canonical hash, tamper, lock/schema/image mismatch
# --------------------------------------------------------------------------- #
def _make_manifest(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("APP_VERSION", kw.pop("app_version", "v1.0.0"))
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", kw.pop("tree_clean", "1"))
    monkeypatch.setenv("APP_GIT_COMMIT", "abcdef1234567890")
    _clear_caches()
    get_settings.cache_clear()
    m = rm.create_release_manifest(get_settings(), backend_test_count=640, frontend_test_count=68,
                                   images=kw.pop("images", None), sbom=kw.pop("sbom", None),
                                   vulnerability_scan=kw.pop("scan", None), **kw)
    out = tmp_path / "release-manifest.json"
    rm.write_release_manifest(m, out_path=out)
    return m, out


def test_manifest_canonical_hash_roundtrip(settings, tmp_path, monkeypatch):
    m, out = _make_manifest(tmp_path, monkeypatch)
    assert m["manifest_version"] == rm.MANIFEST_VERSION and m["integrity"]["scheme"] == "sha256"
    assert m["python_lock_sha256"] and m["frontend_lock_sha256"] and m["migration_dir_sha256"]
    r = rm.verify_release_manifest(out)
    assert r["ok"] and r["reason"] is None and r["release_id"] == m["release_id"]


def test_manifest_tamper_detected(settings, tmp_path, monkeypatch):
    _, out = _make_manifest(tmp_path, monkeypatch)
    data = json.loads(out.read_text())
    data["app_version"] = "v6.6.6"  # body tampered, integrity not recomputed
    out.write_text(json.dumps(data))
    r = rm.verify_release_manifest(out)
    assert not r["ok"] and r["reason"] == "manifest_integrity_mismatch"


def test_manifest_hmac_signing_and_wrong_key(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "1")
    _clear_caches()
    get_settings.cache_clear()
    m = rm.create_release_manifest(get_settings(), hmac_key="rel-key-1")
    assert m["integrity"]["scheme"] == "hmac_sha256"
    out = tmp_path / "m.json"
    rm.write_release_manifest(m, out_path=out)
    assert rm.verify_release_manifest(out, hmac_key="rel-key-1")["ok"]
    assert rm.verify_release_manifest(out, hmac_key="rel-key-2")["reason"] == "manifest_integrity_mismatch"
    assert rm.verify_release_manifest(out, hmac_key=None)["reason"] == "integrity_key_missing"
    assert "rel-key-1" not in out.read_text()


def test_manifest_lock_mismatch_vs_tree(settings, tmp_path, monkeypatch):
    _, out = _make_manifest(tmp_path, monkeypatch)
    # verify against a throwaway tree whose requirements.lock differs -> lock mismatch
    fake_root = tmp_path / "tree"
    (fake_root).mkdir()
    (fake_root / "requirements.lock").write_text("totally-different==0.0.0\n")
    r = rm.verify_release_manifest(out, root=fake_root)
    assert not r["ok"] and r["reason"] == "python_lock_mismatch"


def test_manifest_schema_mismatch(settings, tmp_path, monkeypatch):
    m, out = _make_manifest(tmp_path, monkeypatch)
    data = json.loads(out.read_text())
    data["schema_head"] = "ffffffffffff"
    data["integrity"] = rm._integrity_for(data, None)  # keep integrity valid
    out.write_text(json.dumps(data))
    r = rm.verify_release_manifest(out)
    assert not r["ok"] and r["reason"] == "schema_head_mismatch"


def test_manifest_service_build_mismatch(settings, tmp_path, monkeypatch):
    images = {
        "web": {"name": "web:latest", "image_id": "sha256:a", "build_id": "src:AAAA"},
        "worker": {"name": "worker:latest", "image_id": "sha256:b", "build_id": "src:BBBB"},
    }
    _, out = _make_manifest(tmp_path, monkeypatch, images=images)
    r = rm.verify_release_manifest(out)
    assert not r["ok"] and r["reason"] == "service_build_mismatch"


def test_manifest_incomplete_rejected(settings, tmp_path, monkeypatch):
    _clear_caches(); get_settings.cache_clear()
    m = rm.create_release_manifest(get_settings(), completed=False)
    out = tmp_path / "m.json"
    rm.write_release_manifest(m, out_path=out)
    assert rm.verify_release_manifest(out)["reason"] == "incomplete_release"


def test_manifest_warnings_no_sbom_no_scan_dirty(settings, tmp_path, monkeypatch):
    _, out = _make_manifest(tmp_path, monkeypatch, tree_clean="0")
    r = rm.verify_release_manifest(out)
    assert r["ok"]  # warnings, not failures
    assert "no_sbom" in r["warnings"] and "no_vulnerability_scan" in r["warnings"]
    assert "dirty_build" in r["warnings"]


def test_manifest_no_leak(settings, tmp_path, monkeypatch):
    images = {"web": {"name": "youtube_archiver-web:latest", "image_id": "sha256:abc",
                      "image_digest": None, "build_id": "src:X"}}
    m, out = _make_manifest(tmp_path, monkeypatch, images=images,
                            sbom={"tool": "docker-sbom", "sha256": "a" * 64, "artifact": "sbom.spdx.json"})
    blob = out.read_text()
    for bad in ("/Users/", "/home/", str(REPO), str(tmp_path), "password", "cookie", "@"):
        assert bad not in blob, bad
    s = rm.read_summary_dict(m)
    assert str(tmp_path) not in json.dumps(s)


def test_cli_create_manifest_reads_descriptor_files(settings, tmp_path, monkeypatch):
    """Regression: `release create-manifest` must actually parse the --*-json
    descriptor files (images/sbom/scan/rehearsal), not silently drop them."""
    monkeypatch.setenv("APP_VERSION", "v1.0.0")
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "1")
    _clear_caches()
    get_settings.cache_clear()
    images = tmp_path / "images.json"
    images.write_text(json.dumps({
        "web": {"name": "youtube_archiver-web:latest", "image_id": "sha256:a",
                "image_digest": "youtube_archiver-web@sha256:a", "build_id": "src:SAME"},
        "worker": {"name": "youtube_archiver-worker:latest", "image_id": "sha256:b",
                   "image_digest": None, "build_id": "src:SAME"}}))
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps({"tool": "docker-scout", "format": "spdx-json",
                                "artifact": "sbom.spdx.json", "sha256": "c" * 64}))
    scan = tmp_path / "scan.json"
    scan.write_text(json.dumps({"tool": "trivy", "status": "unavailable"}))
    out = tmp_path / "release-manifest.json"
    r = runner.invoke(cli_app, ["release", "create-manifest", "--out", str(out),
                                "--images-json", str(images), "--sbom-json", str(sbom),
                                "--scan-json", str(scan), "--backend-tests", "643"])
    assert r.exit_code == 0, r.output
    assert "could not read" not in r.output
    assert "services=2" in r.output and "sbom=yes" in r.output
    data = json.loads(out.read_text())
    assert len(data["images"]) == 2 and data["sbom"]["sha256"] == "c" * 64
    assert data["backend_test_count"] == 643


# --------------------------------------------------------------------------- #
# 3. release-check integration: SBOM / scanner policy / severity
# --------------------------------------------------------------------------- #
def _summary_env(tmp_path, monkeypatch, summary: dict):
    p = tmp_path / "release_summary.json"
    p.write_text(json.dumps(summary))
    monkeypatch.setenv("RELEASE_MANIFEST_SUMMARY_FILE", str(p))
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "1")
    monkeypatch.setenv("APP_VERSION", "v1.0.0")
    _clear_caches()
    get_settings.cache_clear()
    return p


def _rel_checks(settings):
    from app.services import production_check as pc

    return {c["name"]: c["status"] for c in pc._release_provenance_checks(settings)}


def test_release_check_missing_sbom_policy(settings, tmp_path, monkeypatch):
    _summary_env(tmp_path, monkeypatch, {
        "manifest_version": 1, "release_id": "rel-x", "completed": True, "integrity_scheme": "sha256",
        "service_build_ids": [], "sbom_present": False, "vulnerability_status": "pass"})
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    assert _rel_checks(get_settings())["sbom_present"] == "warn"
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    assert _rel_checks(get_settings())["sbom_present"] == "fail"


def test_release_check_scanner_unavailable_policy(settings, tmp_path, monkeypatch):
    _summary_env(tmp_path, monkeypatch, {
        "manifest_version": 1, "release_id": "rel-x", "completed": True, "integrity_scheme": "sha256",
        "service_build_ids": [], "sbom_present": True, "sbom_sha256": "a" * 64,
        "vulnerability_status": "unavailable"})
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("RELEASE_SCANNER_UNAVAILABLE_POLICY", "warn")
    get_settings.cache_clear()
    assert _rel_checks(get_settings())["vulnerability_scan"] == "warn"
    monkeypatch.setenv("RELEASE_SCANNER_UNAVAILABLE_POLICY", "fail")
    get_settings.cache_clear()
    assert _rel_checks(get_settings())["vulnerability_scan"] == "fail"


def test_release_check_critical_vuln_fails(settings, tmp_path, monkeypatch):
    _summary_env(tmp_path, monkeypatch, {
        "manifest_version": 1, "release_id": "rel-x", "completed": True, "integrity_scheme": "sha256",
        "service_build_ids": ["src:X"], "sbom_present": True, "sbom_sha256": "a" * 64,
        "vulnerability_status": "fail", "vulnerability_severities": {"CRITICAL": 2, "HIGH": 1}})
    get_settings.cache_clear()
    assert _rel_checks(get_settings())["vulnerability_scan"] == "fail"


def test_release_readiness_api_and_no_leak(settings, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod

    _summary_env(tmp_path, monkeypatch, {
        "manifest_version": 1, "release_id": "rel-abc", "completed": True, "integrity_scheme": "sha256",
        "app_version": "v1.0.0", "build_id": "src:X", "service_build_ids": ["src:X"],
        "sbom_present": True, "sbom_sha256": "a" * 64, "vulnerability_status": "pass",
        "vulnerability_severities": {}})
    client = TestClient(main_mod.app)
    r = client.get("/api/system/release-readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["manifest"]["release_id"] == "rel-abc" and "version" in body
    blob = json.dumps(body)
    assert str(tmp_path) not in blob and "/Users/" not in blob


def test_full_release_check_includes_provenance(settings, session, tmp_path, monkeypatch):
    from app.services import production_check as pc

    _summary_env(tmp_path, monkeypatch, {
        "manifest_version": 1, "release_id": "rel-x", "completed": True, "integrity_scheme": "sha256",
        "app_version": "v1.0.0", "build_id": bi.build_id(), "service_build_ids": [bi.build_id()],
        "image_digests_captured": 0, "sbom_present": True, "sbom_sha256": "a" * 64,
        "vulnerability_status": "pass", "vulnerability_severities": {}})
    r = pc.release_check(session, get_settings())
    names = {c["name"] for c in r["checks"]}
    assert {"git_tree_clean", "application_version", "schema_head_captured",
            "release_manifest", "service_image_build_match", "image_digests_captured",
            "sbom_present", "vulnerability_scan"} <= names
    assert str(REPO) not in json.dumps(r) and "/Users/" not in json.dumps(r)


def test_release_provenance_early_checks_without_summary(settings, monkeypatch):
    """Without a summary configured, the always-present identity checks appear
    and the manifest check WARNs (not FAIL) — no summary-dependent checks."""
    from app.services import production_check as pc

    monkeypatch.delenv("RELEASE_MANIFEST_SUMMARY_FILE", raising=False)
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "1")
    monkeypatch.setenv("APP_VERSION", "v1.0.0")
    _clear_caches()
    get_settings.cache_clear()
    names = {c["name"]: c["status"] for c in pc._release_provenance_checks(get_settings())}
    assert names["git_tree_clean"] == "pass" and names["application_version"] == "pass"
    assert names["release_manifest"] == "warn"
    assert "sbom_present" not in names  # summary-dependent, absent here


# --------------------------------------------------------------------------- #
# 4. dependency lock consistency + script static guards
# --------------------------------------------------------------------------- #
def test_pyproject_deps_present_in_requirements_lock():
    """Every direct [project] dependency must appear in the requirements lock so
    the two don't drift (Phase 10A reproducibility)."""
    import re

    pyproject = (REPO / "pyproject.toml").read_text("utf-8")
    block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    names = []
    for line in block.splitlines():
        m = re.match(r'\s*"([A-Za-z0-9_.\-]+)', line)
        if m:
            names.append(m.group(1).lower().replace("_", "-"))
    req = (REPO / "requirements.txt").read_text("utf-8").lower().replace("_", "-")
    for n in names:
        assert n in req, f"pyproject dep '{n}' missing from requirements.txt lock"


def test_release_scripts_static_guards():
    build = (REPO / "scripts" / "build-release.sh").read_text("utf-8")
    verify = (REPO / "scripts" / "verify-release.sh").read_text("utf-8")
    for text in (build, verify):
        assert "set -euo pipefail" in text
    assert 'DRY_RUN' in build
    # non-destructive: no service recreate / volume deletion / down -v in code lines
    code = [ln for ln in build.splitlines() if not ln.lstrip().startswith("#")]
    for ln in code:
        assert "down -v" not in ln
        assert "--volumes" not in ln
        assert "volume rm" not in ln
        assert "compose up" not in ln, f"build-release must not recreate services: {ln}"
        assert "rm -rf" not in ln
        assert "git add" not in ln, f"build-release must not stage artifacts: {ln}"
    # must not auto-mutate lockfiles
    assert "npm install" not in build and "npm update" not in build
    assert "pip-compile" not in build and "pip install -U" not in build


def test_release_artifacts_gitignored():
    import subprocess

    out = subprocess.run(["git", "-C", str(REPO), "check-ignore", "release/"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "release/" in out.stdout


def test_dockerfile_provenance_and_npm_ci():
    df = (REPO / "Dockerfile").read_text("utf-8")
    assert "ARG BASE_PYTHON_IMAGE" in df and "ARG BASE_NODE_IMAGE" in df
    assert "org.opencontainers.image.revision" in df
    assert "RUN npm ci" in df and "npm ci || npm install" not in df
    assert "ARG APP_VERSION" in df and "APP_GIT_TREE_CLEAN" in df
