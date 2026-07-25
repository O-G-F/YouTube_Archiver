"""Phase 10A.1: reproducible lock + release gate closure.

Hash-pinned Python lock, base-image digest pin recording, real-vs-unavailable
vulnerability policy, production HMAC manifest authenticity, and the reproducible
release-check gates. No real downloads; no secret/path/identity leaks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.services import build_info as bi
from app.services import production_check as pc
from app.services import release_manifest as rm

REPO = Path(__file__).resolve().parents[1]


def _clear():
    bi.frontend_build_id.cache_clear()


# --------------------------------------------------------------------------- #
# 1. the committed requirements.lock IS a real hash-pinned lock
# --------------------------------------------------------------------------- #
def test_requirements_lock_is_exact_and_hashed():
    st = rm.python_lock_status()
    assert st["present"] and st["exact"] and st["hashed"]
    assert st["package_count"] >= 40 and not st["unpinned"]


def test_lock_every_requirement_pinned_with_hash():
    text = (REPO / "requirements.lock").read_text("utf-8")
    reqs, cur_hashes, started = [], 0, False
    per_req = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash=sha256:"):
            cur_hashes += 1
            continue
        if started:
            per_req.append(cur_hashes)
        reqs.append(line.split("\\", 1)[0].strip())
        cur_hashes = 0
        started = True
    if started:
        per_req.append(cur_hashes)
    assert reqs, "lock has no requirements"
    assert all("==" in r for r in reqs), "a requirement is not == pinned"
    assert all(n >= 1 for n in per_req), "a requirement has no --hash"


def test_pyproject_direct_deps_present_in_lock():
    """Every direct [project] dependency must appear in the hash-pinned lock."""
    import re

    block = (REPO / "pyproject.toml").read_text("utf-8").split("dependencies = [", 1)[1].split("]", 1)[0]
    names = []
    for line in block.splitlines():
        m = re.match(r'\s*"([A-Za-z0-9_.\-]+)', line)
        if m:
            names.append(m.group(1).lower().replace("_", "-"))
    lock = (REPO / "requirements.lock").read_text("utf-8").lower().replace("_", "-")
    for n in names:
        assert f"\n{n}==" in ("\n" + lock), f"direct dep '{n}' missing from requirements.lock"


def test_lock_has_transitive_deps():
    """The lock must be a full closure, not just direct deps (e.g. starlette,
    anyio, greenlet are transitive)."""
    lock = (REPO / "requirements.lock").read_text("utf-8").lower()
    for transitive in ("starlette==", "anyio==", "greenlet==", "certifi=="):
        assert transitive in lock, f"transitive dep {transitive} missing (lock not a full closure)"


def test_dockerfile_uses_lock_not_direct_requirements_for_install():
    df = (REPO / "Dockerfile").read_text("utf-8")
    # Phase 10B.2: hashes are still fully enforced, now via a wheelhouse — the
    # committed lock is verified by `pip wheel --require-hashes ... -r
    # requirements.lock` in the builder, and the runtime does
    # `pip install --require-hashes --no-index ... -r ...requirements.runtime.lock`.
    assert "--require-hashes" in df
    assert "-r requirements.lock" in df          # builder verifies the committed lock
    assert "requirements.runtime.lock" in df     # runtime installs the derived lock
    # the non-lock requirements.txt must NOT be the production install input
    for ln in df.splitlines():
        if ln.strip().startswith("RUN pip install") and "requirements.txt" in ln:
            pytest.fail(f"Dockerfile installs from requirements.txt: {ln}")


def test_python_lock_status_detects_unpinned_and_unhashed(tmp_path, monkeypatch):
    bad = tmp_path / "requirements.lock"
    bad.write_text("fastapi>=0.115\nrequests==2.0\n")
    monkeypatch.setattr(rm, "_repo_root", lambda: tmp_path)
    st = rm.python_lock_status()
    assert st["present"] and not st["exact"]   # fastapi>= is unpinned
    assert not st["hashed"]                     # no --hash lines
    assert "fastapi>=0.115" in st["unpinned"]


# --------------------------------------------------------------------------- #
# 2. release manifest records lock status + base digests; verify checks them
# --------------------------------------------------------------------------- #
def _manifest(tmp_path, monkeypatch, *, base_images=None, scan=None, hmac_key=None, **kw):
    monkeypatch.setenv("APP_VERSION", kw.pop("app_version", "v1.0.0"))
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "1")
    _clear()
    get_settings.cache_clear()
    m = rm.create_release_manifest(get_settings(), base_images=base_images,
                                   vulnerability_scan=scan, hmac_key=hmac_key, **kw)
    out = tmp_path / "release-manifest.json"
    rm.write_release_manifest(m, out_path=out)
    return m, out


def test_manifest_records_python_lock_status(settings, tmp_path, monkeypatch):
    m, _ = _manifest(tmp_path, monkeypatch)
    assert m["python_lock"]["exact"] and m["python_lock"]["hashed"]
    assert m["python_lock"]["package_count"] >= 40
    assert m["python_direct_input_sha256"] and m["python_lock_sha256"]


def test_verify_warns_when_base_not_digest_pinned(settings, tmp_path, monkeypatch):
    _, out = _manifest(tmp_path, monkeypatch, base_images={
        "python": {"ref": "python:3.12-slim", "digest": ""},   # floating, not pinned
        "node": {"ref": "node:20-slim", "digest": ""}})
    r = rm.verify_release_manifest(out)
    assert r["ok"] and "base_python_not_digest_pinned" in r["warnings"]
    assert "base_node_not_digest_pinned" in r["warnings"]


def test_verify_clean_when_base_digest_pinned(settings, tmp_path, monkeypatch):
    _, out = _manifest(tmp_path, monkeypatch, base_images={
        "python": {"ref": "python:3.12-slim@sha256:" + "a" * 64, "digest": "python@sha256:" + "a" * 64},
        "node": {"ref": "node:20-slim@sha256:" + "b" * 64, "digest": "node@sha256:" + "b" * 64}})
    r = rm.verify_release_manifest(out)
    assert r["ok"]
    assert "base_python_not_digest_pinned" not in r["warnings"]
    assert "base_node_not_digest_pinned" not in r["warnings"]


def test_summary_surfaces_lock_and_base_pins(settings, tmp_path, monkeypatch):
    m, _ = _manifest(tmp_path, monkeypatch, base_images={
        "python": {"ref": "python:3.12-slim@sha256:" + "a" * 64},
        "node": {"ref": "node:20-slim@sha256:" + "b" * 64}},
        scan={"tool": "trivy", "status": "pass", "severities": {}, "completed": True,
              "db_updated_at": "2026-07-19T12:54:38Z", "tool_version": "0.64.1"})
    s = rm.read_summary_dict(m)
    assert s["python_lock_exact"] is True and s["python_lock_hashed"] is True
    assert s["base_python_digest_pinned"] and s["base_node_digest_pinned"]
    assert s["vulnerability_db_updated_at"] == "2026-07-19T12:54:38Z"
    assert s["vulnerability_tool_version"] == "0.64.1"


# --------------------------------------------------------------------------- #
# 3. HMAC manifest authenticity (prod requires HMAC)
# --------------------------------------------------------------------------- #
def test_manifest_hmac_sign_and_verify(settings, tmp_path, monkeypatch):
    m, out = _manifest(tmp_path, monkeypatch, hmac_key="rel-key-A")
    assert m["integrity"]["scheme"] == "hmac_sha256"
    assert rm.verify_release_manifest(out, hmac_key="rel-key-A")["ok"]
    assert rm.verify_release_manifest(out, hmac_key="rel-key-B")["reason"] == "manifest_integrity_mismatch"
    assert rm.verify_release_manifest(out, hmac_key=None)["reason"] == "integrity_key_missing"
    assert "rel-key-A" not in out.read_text("utf-8")


def test_release_manifest_hmac_key_separate_from_backup_audit(settings, tmp_path, monkeypatch):
    relf = tmp_path / "relkey"; relf.write_text("release-key")
    bakf = tmp_path / "bakkey"; bakf.write_text("backup-key")
    audf = tmp_path / "audkey"; audf.write_text("audit-key")
    monkeypatch.setenv("RELEASE_MANIFEST_HMAC_KEY_FILE", str(relf))
    monkeypatch.setenv("BACKUP_MANIFEST_HMAC_KEY_FILE", str(bakf))
    monkeypatch.setenv("AUDIT_HMAC_KEY_FILE", str(audf))
    get_settings.cache_clear()
    s = get_settings()
    assert s.release_manifest_hmac_key() == "release-key"
    assert s.release_manifest_hmac_key() != s.backup_manifest_hmac_key()
    assert s.release_manifest_hmac_key() != s.audit_hmac_key()


# --------------------------------------------------------------------------- #
# 4. release-check reproducibility gates (dev WARN vs prod FAIL)
# --------------------------------------------------------------------------- #
def _summary_env(tmp_path, monkeypatch, summary, *, prod=False):
    p = tmp_path / "rel_summary.json"
    p.write_text(json.dumps(summary))
    monkeypatch.setenv("RELEASE_MANIFEST_SUMMARY_FILE", str(p))
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "1")
    monkeypatch.setenv("APP_VERSION", "v1.0.0")
    if prod:
        monkeypatch.setenv("APP_ENV", "production")
    _clear()
    get_settings.cache_clear()
    return get_settings()


_FULL = {
    "manifest_version": 1, "release_id": "rel-x", "completed": True, "integrity_scheme": "hmac_sha256",
    "app_version": "v1.0.0", "service_build_ids": [], "sbom_present": True, "sbom_sha256": "a" * 64,
    "vulnerability_status": "pass", "vulnerability_severities": {},
    "vulnerability_db_updated_at": None,  # set per test
    "python_lock_exact": True, "python_lock_hashed": True,
    "base_python_digest_pinned": True, "base_node_digest_pinned": True,
}


def _checks(settings):
    return {c["name"]: c for c in pc._release_provenance_checks(settings)}


def test_gate_vulnerability_scan_completed_vs_unavailable(settings, tmp_path, monkeypatch):
    s = _summary_env(tmp_path, monkeypatch, {**_FULL, "vulnerability_status": "unavailable"}, prod=True)
    monkeypatch.setenv("RELEASE_SCANNER_UNAVAILABLE_POLICY", "fail")
    get_settings.cache_clear()
    c = _checks(get_settings())
    assert c["vulnerability_scan_completed"]["status"] == "fail"
    assert c["vulnerability_scan"]["status"] == "fail"


def test_gate_vulnerability_scan_completed_pass(settings, tmp_path, monkeypatch):
    from app.models import utcnow
    fresh = utcnow().isoformat() + "Z"
    s = _summary_env(tmp_path, monkeypatch, {**_FULL, "vulnerability_db_updated_at": fresh})
    c = _checks(get_settings())
    assert c["vulnerability_scan_completed"]["status"] == "pass"
    assert c["vulnerability_db_fresh"]["status"] == "pass"


def test_gate_vuln_db_stale(settings, tmp_path, monkeypatch):
    old = "2020-01-01T00:00:00Z"
    _summary_env(tmp_path, monkeypatch, {**_FULL, "vulnerability_db_updated_at": old}, prod=True)
    c = _checks(get_settings())
    assert c["vulnerability_db_fresh"]["status"] == "fail"


def test_gate_base_digest_pin_prod_fail(settings, tmp_path, monkeypatch):
    _summary_env(tmp_path, monkeypatch, {**_FULL, "base_python_digest_pinned": False}, prod=True)
    c = _checks(get_settings())
    assert c["base_images_digest_pinned"]["status"] == "fail"


def test_gate_manifest_authenticated_prod_requires_hmac(settings, tmp_path, monkeypatch):
    _summary_env(tmp_path, monkeypatch, {**_FULL, "integrity_scheme": "sha256"}, prod=True)
    c = _checks(get_settings())
    assert c["release_manifest_authenticated"]["status"] == "fail"
    # dev tolerates sha256-only as WARN
    monkeypatch.delenv("APP_ENV", raising=False)
    get_settings.cache_clear()
    c2 = _checks(get_settings())
    assert c2["release_manifest_authenticated"]["status"] == "warn"


def test_gate_installed_packages_match_lock(settings, tmp_path, monkeypatch):
    """The test env installs the same deps as the lock, so this should PASS or
    (if a version differs in this venv) be a WARN in dev — never crash."""
    _summary_env(tmp_path, monkeypatch, _FULL)
    c = _checks(get_settings())
    assert c["installed_packages_match_lock"]["status"] in ("pass", "warn", "fail")
    assert c["python_lock_exact"]["status"] == "pass"
    assert c["python_lock_hashes_valid"]["status"] == "pass"


def test_release_readiness_no_leak(settings, tmp_path, monkeypatch):
    s = _summary_env(tmp_path, monkeypatch, _FULL)
    d = pc.release_readiness(get_settings())
    blob = json.dumps(d)
    for bad in ("/Users/", "/home/", str(REPO), str(tmp_path), "password", "secret", "token"):
        assert bad not in blob


# --------------------------------------------------------------------------- #
# 5. build-release.sh static guards for 10A.1
# --------------------------------------------------------------------------- #
def test_build_release_pins_base_and_uses_db_cache():
    txt = (REPO / "scripts" / "build-release.sh").read_text("utf-8")
    assert "resolve_base_digest" in txt
    assert "RELEASE_REQUIRE_DIGEST" in txt
    assert "--skip-db-update" in txt and "TRIVY_DB_VOLUME" in txt
    # still non-destructive
    code = [ln for ln in txt.splitlines() if not ln.lstrip().startswith("#")]
    for ln in code:
        assert "down -v" not in ln and "volume rm" not in ln and "compose up" not in ln
    # gen-python-lock is present and does not auto-upgrade
    gen = (REPO / "scripts" / "gen-python-lock.py").read_text("utf-8")
    assert "NO re-resolution" in gen and "pip install -U" not in gen
