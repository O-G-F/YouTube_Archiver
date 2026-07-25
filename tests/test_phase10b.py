"""Phase 10B: vulnerability triage + controlled-remediation gating.

Trivy-report parse/classify (OS vs Python vs npm vs vendored-binary via PURL),
CVE dedup, fixed/no-fix, time-bound exception policy, remediation diff + integrity,
and the production release gates. No host paths / secrets in any output.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.services import vuln_exceptions as ve
from app.services import vuln_triage as vt

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()


def _trivy(*results):
    return {"ArtifactName": "app-web:latest", "Results": list(results)}


def _os_result(*vulns):
    return {"Target": "app-web:latest (debian 13.5)", "Class": "os-pkgs", "Type": "debian",
            "Vulnerabilities": list(vulns)}


def _py_result(*vulns):
    return {"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg",
            "Vulnerabilities": list(vulns)}


def _v(cve, sev, pkg, inst, fixed=None, purl=None, src="Debian Security Tracker"):
    d = {"VulnerabilityID": cve, "Severity": sev, "PkgName": pkg,
         "InstalledVersion": inst, "DataSource": {"Name": src}}
    if fixed:
        d["FixedVersion"] = fixed
    if purl:
        d["PkgIdentifier"] = {"PURL": purl}
    return d


# --------------------------------------------------------------------------- #
# 1. parse / classify / dedup / fixed
# --------------------------------------------------------------------------- #
def test_parse_classifies_os_python_npm_and_bundled_binary():
    report = _trivy(
        _os_result(
            _v("CVE-2026-40393", "CRITICAL", "libgbm1", "25.0.7-2", fixed="25.0.7-2+deb13u1"),
            _v("CVE-2026-6653", "CRITICAL", "libxml2", "2.9.14"),  # no fix
            # vendored RPM lib inside a python wheel, mis-attributed to the debian target
            _v("CVE-2022-1586", "CRITICAL", "pcre2", "10.32-3.el8_6", fixed="10.40-1",
               purl="pkg:rpm/almalinux/pcre2@10.32-3.el8_6"),
        ),
        _py_result(
            _v("CVE-2024-9999", "HIGH", "somepkg", "1.0.0", fixed="1.0.1",
               purl="pkg:pypi/somepkg@1.0.0"),
        ),
    )
    s = vt.parse_report(report)
    assert s["severity_counts"]["CRITICAL"] == 3
    cb = s["critical"]["by_bucket"]
    assert cb.get("os") == 2 and cb.get("binary") == 1  # pcre2 -> binary, not os
    assert s["critical"]["fixed_available_rows"] == 2 and s["critical"]["no_fix_rows"] == 1
    # HIGH python bucket
    assert s["high"]["by_bucket"].get("python") == 1
    # the pcre2 finding is bucketed binary with purl_kind rpm
    pf = [f for f in s["critical"]["findings"] if f["package"] == "pcre2"][0]
    assert pf["bucket"] == "binary" and pf["purl_kind"] == "rpm"


def test_duplicate_cve_across_packages_deduped_in_unique_count():
    report = _trivy(_os_result(
        _v("CVE-2026-40393", "CRITICAL", "libgbm1", "25.0.7-2", fixed="x"),
        _v("CVE-2026-40393", "CRITICAL", "libglx-mesa0", "25.0.7-2", fixed="x"),
        _v("CVE-2026-40393", "CRITICAL", "mesa-libgallium", "25.0.7-2", fixed="x"),
    ))
    s = vt.parse_report(report)
    assert s["critical"]["rows"] == 3 and s["critical"]["unique_cves"] == 1


def test_no_leak_in_parse_output():
    report = _trivy(_os_result(_v("CVE-1", "CRITICAL", "p", "1")))
    report["Results"][0]["Vulnerabilities"][0]["PkgPath"] = "/Users/someone/x"  # must be dropped
    blob = json.dumps(vt.parse_report(report))
    for bad in ("/Users/", "/home/", "PkgPath", "someone"):
        assert bad not in blob


# --------------------------------------------------------------------------- #
# 2. exception policy
# --------------------------------------------------------------------------- #
def _exc(**over):
    base = {
        "vulnerability_id": "CVE-2026-6653", "package": "libxml2", "installed_version": "2.9.14",
        "reason": "no Debian fix published", "reachability_assessment": "not reached by app",
        "compensating_control": "network isolation", "approved_by": "opshandle",
        "approved_at": "2026-07-19T00:00:00",
        "expires_at": (datetime.utcnow() + timedelta(days=30)).isoformat(),
        "tracking_reference": "TICKET-123",
    }
    base.update(over)
    return base


def test_exception_valid_active_covers_critical():
    keys = {("CVE-2026-6653", "libxml2", "2.9.14")}
    v = ve.validate_exceptions([_exc()])
    assert v["valid"] and len(v["active"]) == 1
    assert ve.approved_keys(v["active"]) == keys


def test_exception_missing_reason_is_invalid():
    v = ve.validate_exceptions([_exc(reason="")])
    assert not v["valid"] and len(v["invalid"]) == 1


def test_exception_no_expiry_is_invalid():
    v = ve.validate_exceptions([_exc(expires_at="")])
    assert not v["valid"] and len(v["invalid"]) == 1


def test_expired_exception_flagged():
    v = ve.validate_exceptions([_exc(expires_at="2020-01-01T00:00:00")])
    assert not v["valid"] and len(v["expired"]) == 1 and not v["active"]


def test_exception_version_mismatch_does_not_cover():
    # exception is for 2.9.14 but the current finding is 2.9.15 -> not covered
    crit_keys = {("CVE-2026-6653", "libxml2", "2.9.15")}
    ex = _exc()  # installed 2.9.14

    class _S:
        pass

    import tempfile
    p = Path(tempfile.mkstemp(suffix=".yml")[1])
    import yaml
    p.write_text(yaml.safe_dump({"exceptions": [ex]}))
    r = ve.evaluate(p, crit_keys)
    assert r["critical_unapproved"] == 1 and r["critical_covered"] == 0 and r["stale_exceptions"] == 1


def test_exception_text_leak_rejected():
    v = ve.validate_exceptions([_exc(reason="see https://internal.example/secret")])
    assert not v["valid"] and len(v["invalid"]) == 1


def test_repo_exceptions_file_is_empty_template():
    from app.services import vuln_exceptions as ve2

    excs = ve2.load_exceptions(REPO / "vulnerability-exceptions.yml")
    assert excs == []  # no exceptions added without operator approval


# --------------------------------------------------------------------------- #
# 3. remediation diff + integrity
# --------------------------------------------------------------------------- #
def test_remediation_report_diff_and_integrity():
    before = _trivy(_os_result(
        _v("CVE-A", "CRITICAL", "mesa", "1", fixed="2"),
        _v("CVE-B", "CRITICAL", "xml", "1"),
    ))
    after = _trivy(_os_result(_v("CVE-B", "CRITICAL", "xml", "1")))  # CVE-A remediated
    rep = vt.remediation_report(before=before, after=after,
                                before_meta={"release_id": "rc1"}, after_meta={"release_id": "rc2"})
    assert rep["critical"]["before_count"] == 2 and rep["critical"]["after_count"] == 1
    assert rep["critical"]["removed"] == ["CVE-A|mesa"] and rep["critical"]["added"] == []
    assert rep["overall_status"] == "critical_remaining"  # CVE-B still there, no exceptions
    assert vt.verify_report_integrity(rep) is True


def test_remediation_report_integrity_tamper_detected():
    rep = vt.remediation_report(before=_trivy(), after=_trivy(),
                                before_meta={}, after_meta={})
    assert vt.verify_report_integrity(rep) is True
    rep["critical"]["after_count"] = 99  # tamper
    assert vt.verify_report_integrity(rep) is False


def test_remediation_report_hmac():
    rep = vt.remediation_report(before=_trivy(), after=_trivy(),
                                before_meta={}, after_meta={}, hmac_key="k1")
    assert rep["integrity"]["scheme"] == "hmac_sha256"
    assert vt.verify_report_integrity(rep, "k1") is True
    assert vt.verify_report_integrity(rep, "k2") is False


# --------------------------------------------------------------------------- #
# 4. release-check gates
# --------------------------------------------------------------------------- #
def _summary_env(settings, tmp_path, monkeypatch, extra: dict):
    base = {"manifest_version": 1, "release_id": "rel-x", "completed": True,
            "integrity_scheme": "hmac_sha256", "app_version": "v1.0.0",
            "build_id": "src:X", "service_build_ids": ["src:X"], "image_digests_captured": 4,
            "sbom_present": True, "sbom_sha256": "a" * 64, "python_lock_exact": True,
            "python_lock_hashed": True, "base_python_digest_pinned": True,
            "base_node_digest_pinned": True, "vulnerability_status": "fail",
            "vulnerability_db_updated_at": datetime.utcnow().isoformat()}
    base.update(extra)
    p = tmp_path / "release_summary.json"
    p.write_text(json.dumps(base))
    monkeypatch.setenv("RELEASE_MANIFEST_SUMMARY_FILE", str(p))
    monkeypatch.setenv("APP_GIT_TREE_CLEAN", "1")
    monkeypatch.setenv("APP_VERSION", "v1.0.0")
    from app.services import build_info as bi

    bi.frontend_build_id.cache_clear()
    get_settings.cache_clear()
    return p


def _checks(settings):
    from app.services import production_check as pc

    return {c["name"]: c["status"] for c in pc._release_provenance_checks(settings)}


def test_release_check_unapproved_critical_fails(settings, tmp_path, monkeypatch):
    _summary_env(settings, tmp_path, monkeypatch, {
        "vulnerability_severities": {"CRITICAL": 14, "HIGH": 87}, "critical_unapproved": 14,
        "scanner_provenance_status": "recorded_unverified", "vulnerability_report_integrity": True})
    ch = _checks(get_settings())
    assert ch["critical_vulnerabilities"] == "fail"
    assert ch["high_vulnerabilities"] in ("warn",)  # policy=warn default


def test_release_check_critical_covered_by_exceptions_passes(settings, tmp_path, monkeypatch):
    _summary_env(settings, tmp_path, monkeypatch, {
        "vulnerability_severities": {"CRITICAL": 6, "HIGH": 5}, "critical_unapproved": 0,
        "critical_covered": 6, "scanner_provenance_status": "verified",
        "vulnerability_report_integrity": True, "vulnerability_exceptions_active": 6})
    ch = _checks(get_settings())
    assert ch["critical_vulnerabilities"] == "pass"
    assert ch["scanner_provenance_verified"] == "pass"


def test_release_check_expired_exception_fails(settings, tmp_path, monkeypatch):
    _summary_env(settings, tmp_path, monkeypatch, {
        "vulnerability_severities": {"CRITICAL": 1}, "critical_unapproved": 0,
        "vulnerability_exceptions_expired": 1, "vulnerability_report_integrity": True})
    assert _checks(get_settings())["vulnerability_exceptions_valid"] == "fail"


def test_release_check_report_integrity_mismatch_fails(settings, tmp_path, monkeypatch):
    _summary_env(settings, tmp_path, monkeypatch, {
        "vulnerability_severities": {"CRITICAL": 0}, "critical_unapproved": 0,
        "vulnerability_report_integrity": False})
    assert _checks(get_settings())["vulnerability_report_integrity"] == "fail"


def test_release_check_scanner_provenance_prod_fail(settings, tmp_path, monkeypatch):
    _summary_env(settings, tmp_path, monkeypatch, {
        "vulnerability_severities": {"CRITICAL": 0}, "critical_unapproved": 0,
        "scanner_provenance_status": "recorded_unverified", "vulnerability_report_integrity": True})
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    assert _checks(get_settings())["scanner_provenance_verified"] == "fail"


def test_release_check_no_leak(settings, tmp_path, monkeypatch):
    _summary_env(settings, tmp_path, monkeypatch, {
        "vulnerability_severities": {"CRITICAL": 0}, "critical_unapproved": 0})
    from app.services import production_check as pc

    blob = json.dumps(pc._release_provenance_checks(get_settings()))
    assert str(tmp_path) not in blob and "/Users/" not in blob


# --------------------------------------------------------------------------- #
# 5. CLI triage-scan + remediation-report
# --------------------------------------------------------------------------- #
def test_cli_triage_scan_gates_and_records(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("VULNERABILITY_EXCEPTIONS_FILE", str(tmp_path / "none.yml"))
    get_settings.cache_clear()
    report = _trivy(_os_result(_v("CVE-X", "CRITICAL", "p", "1"), _v("CVE-Y", "HIGH", "q", "1")))
    tf = tmp_path / "trivy.json"
    tf.write_text(json.dumps(report))
    out = tmp_path / "scan-desc.json"
    r = runner.invoke(cli_app, ["release", "triage-scan", "--trivy", str(tf), "--out", str(out),
                                "--scanner-version", "0.64.1", "--scanner-image-id", "abc123"])
    assert r.exit_code == 1  # 1 unapproved CRITICAL
    d = json.loads(out.read_text())
    assert d["critical_total"] == 1 and d["critical_unapproved"] == 1
    # Phase 10B.2: a short (non-full-sha256) image id with no real RepoDigest is
    # `unverified` (and the short id is rejected, not stored).
    assert d["scanner"]["provenance_status"] == "unverified"
    assert "image_id_not_full_sha256" in d["scanner"]["provenance_errors"]
    assert d["scanner"]["image_id"] is None
    assert d["severities"]["CRITICAL"] == 1 and d["report_integrity_valid"] is True
    assert str(tmp_path) not in r.output and "/Users/" not in r.output


def test_cli_remediation_report(settings, tmp_path):
    b = tmp_path / "b.json"
    a = tmp_path / "a.json"
    b.write_text(json.dumps(_trivy(_os_result(_v("CVE-A", "CRITICAL", "m", "1", fixed="2")))))
    a.write_text(json.dumps(_trivy()))
    out = tmp_path / "rem.json"
    r = runner.invoke(cli_app, ["release", "remediation-report", "--before", str(b),
                                "--after", str(a), "--out", str(out), "--before-id", "rc1",
                                "--after-id", "rc2"])
    assert r.exit_code == 0 and "removed=1" in r.output
    rep = json.loads(out.read_text())
    assert rep["critical"]["removed"] == ["CVE-A|m"] and rep["overall_status"] == "clean"


# --------------------------------------------------------------------------- #
# 6. script / static guards
# --------------------------------------------------------------------------- #
def test_build_release_no_auto_upgrade_or_force():
    t = (REPO / "scripts" / "build-release.sh").read_text("utf-8")
    code = [ln for ln in t.splitlines() if not ln.lstrip().startswith("#")]
    for ln in code:
        assert "npm audit fix --force" not in ln
        assert "apt-get upgrade" not in ln  # blanket upgrade forbidden
        assert "pip install -U" not in ln and "pip install --upgrade -r" not in ln
    # scanner failure must never be recorded as a pass
    assert '"status": "unavailable"' in t
    assert "--require-hashes" in (REPO / "Dockerfile").read_text("utf-8")  # hash lock kept


def test_dockerfile_no_floating_base_regression_and_targeted_upgrade():
    df = (REPO / "Dockerfile").read_text("utf-8")
    assert "ARG BASE_PYTHON_IMAGE" in df
    # if an apt upgrade exists it must be targeted (--only-upgrade), never blanket
    for ln in df.splitlines():
        if "apt-get" in ln and "upgrade" in ln and not ln.lstrip().startswith("#"):
            assert "--only-upgrade" in ln, f"blanket apt upgrade: {ln}"
