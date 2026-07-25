"""Phase 10B.3: canonical CVE inventory, reachability/exception PROPOSALS, and the
decision dossier. Proposals are advisory and NEVER active; release-check keeps
FAILing the unapproved CRITICALs. No host paths/secrets in any output.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.services import vuln_inventory as vi
from app.services import vuln_proposals as vp

REPO = Path(__file__).resolve().parents[1]


def _v(cve, sev, pkg, inst, fixed=None, purl=None):
    d = {"VulnerabilityID": cve, "Severity": sev, "PkgName": pkg, "InstalledVersion": inst,
         "DataSource": {"Name": "Debian Security Tracker"}}
    if fixed:
        d["FixedVersion"] = fixed
    if purl:
        d["PkgIdentifier"] = {"PURL": purl}
    return d


def _scan(*vulns, target="app (debian 13.5)", cls="os-pkgs", typ="debian"):
    return {"Results": [{"Target": target, "Class": cls, "Type": typ, "Vulnerabilities": list(vulns)}]}


def _series():
    # rc2: libxml2 CVE-X present at u2 + mesa CVE-M ; rc3: libxml2 CVE-X at u3 (version
    # bumped) + mesa removed ; rc4: libxml2 CVE-X still at u3 (never removed)
    rc2 = _scan(_v("CVE-X", "CRITICAL", "libxml2", "2.9.14-u2", purl="pkg:deb/debian/libxml2@2.9.14-u2?distro=debian-13.5"),
                _v("CVE-M", "CRITICAL", "libgbm1", "25.0.7-2", fixed="25.0.7-2+deb13u1",
                   purl="pkg:deb/debian/libgbm1@25.0.7-2?distro=debian-13.5"))
    rc3 = _scan(_v("CVE-X", "CRITICAL", "libxml2", "2.9.14-u3", purl="pkg:deb/debian/libxml2@2.9.14-u3?distro=debian-13.5"))
    rc4 = _scan(_v("CVE-X", "CRITICAL", "libxml2", "2.9.14-u3", purl="pkg:deb/debian/libxml2@2.9.14-u3?distro=debian-13.5"))
    def sha(d): return hashlib.sha256(json.dumps(d).encode()).hexdigest()
    return [("rc2", rc2, sha(rc2)), ("rc3", rc3, sha(rc3)), ("rc4", rc4, sha(rc4))]


# --------------------------------------------------------------------------- #
# 1. canonical inventory: version-independent identity + lifecycle
# --------------------------------------------------------------------------- #
def test_inventory_version_independent_identity_and_lifecycle():
    inv = vi.build_inventory(_series())
    recs = {(r["vulnerability_id"], r["package"]): r for r in inv["records"]}
    x = recs[("CVE-X", "libxml2")]
    # same CVE across a version bump -> ONE record, still remaining (not removed+added)
    assert x["current_status"] == "remaining" and x["removed_in_release"] is None
    assert x["first_seen_release"] == "rc2" and x["per_release"]["rc4"]["installed_version"] == "2.9.14-u3"
    m = recs[("CVE-M", "libgbm1")]
    assert m["current_status"] == "removed" and m["removed_in_release"] == "rc3"
    assert inv["counts"]["remaining"] == 1 and inv["counts"]["removed"] == 1


def test_inventory_integrity_and_tamper():
    inv = vi.build_inventory(_series())
    assert vi.verify_inventory_integrity(inv)
    inv["records"][0]["severity"] = "LOW"   # tamper
    assert not vi.verify_inventory_integrity(inv)


# --------------------------------------------------------------------------- #
# 2. libxml2 reporting reconciliation
# --------------------------------------------------------------------------- #
def test_reconcile_detects_version_reclassification():
    inv = vi.build_inventory(_series())
    # a remediation report that lists CVE-X as BOTH removed and added (version bump)
    report = {"critical": {"removed": ["CVE-X|libxml2", "CVE-M|libgbm1"], "added": ["CVE-X|libxml2"]}}
    issues = vi.reconcile_remediation(inv, report)
    keyed = {i["vulnerability_id"]: i for i in issues}
    assert keyed["CVE-X"]["reason_code"] == vi.REASON_VERSION_RECLASSIFIED
    # CVE-M (mesa) genuinely removed -> not flagged
    assert "CVE-M" not in keyed


def test_reconcile_flags_claimed_removed_still_present():
    inv = vi.build_inventory(_series())
    report = {"critical": {"removed": ["CVE-X|libxml2"], "added": []}}  # claims removed but it's present
    issues = vi.reconcile_remediation(inv, report)
    assert issues and issues[0]["reason_code"] == vi.REASON_ID_MISMATCH


# --------------------------------------------------------------------------- #
# 3. proposals: schema, reachability enum, evidence, non-active
# --------------------------------------------------------------------------- #
def _inv_and_hash():
    inv = vi.build_inventory(_series())
    rec = next(r for r in inv["records"] if r["current_status"] == "remaining")
    h = "sha256:" + hashlib.sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return inv, rec, h


def _proposal(rec, h, **over):
    p = {
        "vulnerability_id": rec["vulnerability_id"], "package": rec["package"],
        "installed_version": rec["per_release"]["rc4"]["installed_version"],
        "reachability": "not_reachable_with_evidence",
        "evidence_summary": "not loaded in app process; ffmpeg-only transitive dep",
        "risk_summary": "DoS only", "recommended_decision": "exception_candidate",
        "evidence_hash": h, "tracking_reference": "T-1",
        "compensating_controls": ["control a", "control b"],
        "approved_by": None, "approved_at": None, "approval_reference": None,
    }
    p.update(over)
    return p


def test_proposals_valid_and_non_active():
    inv, rec, h = _inv_and_hash()
    doc = {"meta": {"active": False}, "proposals": [_proposal(rec, h)], "loaded": True}
    v = vp.validate_proposals(doc, inv)
    assert v["dossier_valid"] and v["all_unapproved"] and v["errors"] == []
    assert v["reachability_complete"] and v["exception_candidates"] == 1


def test_proposal_with_filled_approval_is_error():
    inv, rec, h = _inv_and_hash()
    doc = {"meta": {"active": False}, "proposals": [_proposal(rec, h, approved_by="alice")], "loaded": True}
    v = vp.validate_proposals(doc, inv)
    assert not v["dossier_valid"] and any("approval field" in e for e in v["errors"])


def test_not_reachable_requires_multiple_evidences():
    inv, rec, h = _inv_and_hash()
    doc = {"meta": {"active": False},
           "proposals": [_proposal(rec, h, compensating_controls=["only one"])], "loaded": True}
    v = vp.validate_proposals(doc, inv)
    assert any(">=2 evidences" in e for e in v["errors"])


def test_bad_reachability_enum_rejected():
    inv, rec, h = _inv_and_hash()
    doc = {"meta": {"active": False}, "proposals": [_proposal(rec, h, reachability="totally_safe")], "loaded": True}
    v = vp.validate_proposals(doc, inv)
    assert any("bad reachability" in e for e in v["errors"])


def test_exception_candidate_blocked_when_fix_available():
    # craft an inventory record WITH a fix, then propose exception_candidate -> blocked
    scans = _series()
    # add a fixed CVE present in all releases
    for _, d, _sha in scans:
        d["Results"][0]["Vulnerabilities"].append(
            _v("CVE-FIX", "CRITICAL", "libfoo", "1.0", fixed="1.1",
               purl="pkg:deb/debian/libfoo@1.0?distro=debian-13.5"))
    inv = vi.build_inventory([(l, d, hashlib.sha256(json.dumps(d).encode()).hexdigest()) for l, d, _ in scans])
    rec = next(r for r in inv["records"] if r["vulnerability_id"] == "CVE-FIX")
    h = "sha256:" + hashlib.sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    doc = {"meta": {"active": False}, "proposals": [_proposal(rec, h, reachability="potentially_reachable")],
           "loaded": True}
    v = vp.validate_proposals(doc, inv)
    assert any("fixed_version_available" in e for e in v["errors"])


def test_evidence_hash_mismatch_rejected():
    inv, rec, h = _inv_and_hash()
    doc = {"meta": {"active": False}, "proposals": [_proposal(rec, "sha256:" + "0" * 64)], "loaded": True}
    v = vp.validate_proposals(doc, inv)
    assert any("evidence_hash mismatch" in e for e in v["errors"])


# --------------------------------------------------------------------------- #
# 4. dossier integrity + gates + provenance/wheel separation
# --------------------------------------------------------------------------- #
def _dossier(**over):
    inv, rec, h = _inv_and_hash()
    doc = {"meta": {"active": False}, "proposals": [_proposal(rec, h)], "loaded": True}
    return vp.build_dossier(
        inv, doc,
        reconciliation_issues=[{"vulnerability_id": "CVE-X", "package": "libxml2",
                                "reason_code": vi.REASON_VERSION_RECLASSIFIED, "detail": "x"}],
        scanner_provenance=over.get("prov", {"status": "unverified", "operator_approval": False}),
        wheel_reproducibility=over.get("wheel", {"status": "not_bit_reproducible",
                                                 "differing_factor": "elf_gnu_build_id",
                                                 "functional_deps_identical": True}),
        generated_for_release="v0.10.0-rc4")


def test_dossier_integrity_and_tamper():
    d = _dossier()
    assert vp.verify_dossier_integrity(d)
    d["proposals_validation"]["valid_count"] = 99
    assert not vp.verify_dossier_integrity(d)


def test_dossier_gates_pass_but_scanner_and_wheel_warn():
    gates = {g["name"]: g["status"] for g in vp.dossier_checks(_dossier(), prod=True)}
    assert gates["vulnerability_inventory_consistent"] == "pass"
    assert gates["vulnerability_reachability_complete"] == "pass"
    assert gates["vulnerability_decision_dossier_valid"] == "pass"
    # unverified scanner => no operator approval present (not fabricated)
    assert gates["scanner_operator_approval_present"] == "warn"
    # non-bit-reproducible wheel is documented, not fatal
    assert gates["wheel_reproducibility_status"] == "warn"


def test_dossier_invalid_on_integrity_mismatch_fails_gate():
    d = _dossier()
    d["integrity"]["value"] = "0" * 64  # tamper
    gates = {g["name"]: g["status"] for g in vp.dossier_checks(d, prod=True)}
    assert gates["vulnerability_decision_dossier_valid"] == "fail"


def test_scanner_operator_approval_present_when_attested():
    d = _dossier(prov={"status": "local_image_id_verified", "operator_approval": True})
    gates = {g["name"]: g["status"] for g in vp.dossier_checks(d, prod=True)}
    assert gates["scanner_operator_approval_present"] == "pass"


# --------------------------------------------------------------------------- #
# 5. the SHIPPED artifacts: real proposals/dossier are valid, non-active, leak-free
# --------------------------------------------------------------------------- #
def test_shipped_proposals_are_valid_and_unapproved():
    doc = vp.load_proposals(REPO / "vulnerability-exception-proposals.yml")
    assert doc["loaded"] and (doc["meta"].get("active") is False)
    for p in doc["proposals"]:
        assert not p.get("approved_by") and not p.get("approved_at") and not p.get("approval_reference")


def test_shipped_dossier_valid_and_separate_from_active_exceptions():
    d = json.loads((REPO / "docs" / "vulnerability-decision-dossier.json").read_text("utf-8"))
    assert vp.verify_dossier_integrity(d)
    assert d["proposals_validation"]["dossier_valid"] and d["proposals_validation"]["all_unapproved"]
    sep = vp.proposals_are_inactive(REPO / "vulnerability-exception-proposals.yml",
                                    REPO / "vulnerability-exceptions.yml")
    assert sep["ok"] and sep["proposals_contribute_active_exceptions"] == 0


def test_no_leak_in_shipped_artifacts():
    from app.services.vuln_triage import scan_text_for_host_leaks
    for f in ("vulnerability-exception-proposals.yml",
              "docs/vulnerability-decision-dossier.json",
              "docs/vulnerability-decision-dossier.md"):
        txt = (REPO / f).read_text("utf-8")
        assert scan_text_for_host_leaks(txt, repo_root=str(REPO)) == [], f"leak in {f}"


# --------------------------------------------------------------------------- #
# 6. release-check integration: dossier gates present; CRITICAL stays FAIL
# --------------------------------------------------------------------------- #
def test_release_check_keeps_critical_fail_with_unapproved_and_proposals_not_active():
    from app.config import get_settings
    from app.services import production_check as pc
    get_settings.cache_clear()
    # 7 unapproved CRITICAL, empty active exceptions -> critical_vulnerabilities FAIL
    summary = {"vulnerability_status": "fail",
               "vulnerability_severities": {"CRITICAL": 7, "HIGH": 69},
               "critical_unapproved": 7, "vulnerability_report_integrity": True,
               "scanner_provenance_status": "unverified",
               "vulnerability_exceptions_active": 0}
    triage = {g["name"]: g["status"] for g in pc._vuln_triage_checks(get_settings(), summary, prod=True)}
    assert triage["critical_vulnerabilities"] == "fail"
    assert triage["vulnerability_exceptions_valid"] == "pass"  # empty is valid
    dossier_gates = {g["name"] for g in pc._decision_dossier_checks(get_settings(), prod=True)}
    assert "vulnerability_decision_dossier_valid" in dossier_gates
    assert "wheel_reproducibility_status" in dossier_gates
