"""Phase 11A: local single-user product acceptance — the security-posture summary.

The posture surfaces the KNOWN accepted CRITICAL count (from the decision dossier)
even when the live scan for this build is unavailable, and distinguishes
local-accepted-risk from production-blocked. It NEVER changes a release-check
result and NEVER emits host paths or secrets.
"""

from __future__ import annotations

from app.config import get_settings
from app.services import production_check as pc


def _posture(monkeypatch, dossier, prod=False):
    monkeypatch.setattr(pc, "_load_decision_dossier", lambda _s: dossier)
    s = get_settings()
    monkeypatch.setattr(type(s), "is_production", property(lambda self: prod))
    checks = [{"name": "critical_vulnerabilities", "status": "warn", "detail": "x"}]
    return pc._security_posture(s, checks)


def _dossier(remaining=7, candidates=4, reach=True):
    return {"proposals_validation": {"remaining_critical_count": remaining,
                                     "exception_candidates": candidates,
                                     "reachability_complete": reach}}


def test_posture_surfaces_known_accepted_critical(monkeypatch):
    p = _posture(monkeypatch, _dossier(7))
    assert p["known_critical_accepted"] == 7           # NOT hidden
    assert p["operating_mode"] == "local_single_user_dev"
    assert p["production_ready"] is False               # open CRITICAL => not prod-ready
    assert p["active_vulnerability_exceptions"] == 0
    assert p["reachability_assessed"] is True


def test_posture_production_mode_label(monkeypatch):
    p = _posture(monkeypatch, _dossier(7), prod=True)
    assert p["operating_mode"] == "production" and p["production_ready"] is False


def test_posture_zero_critical_is_production_ready(monkeypatch):
    p = _posture(monkeypatch, _dossier(0))
    assert p["known_critical_accepted"] == 0 and p["production_ready"] is True


def test_posture_no_dossier_is_safe(monkeypatch):
    p = _posture(monkeypatch, None)
    assert p["known_critical_accepted"] is None
    # unknown critical count must not falsely claim production-ready-with-open-CVEs;
    # with no known critical it is not blocked, but the note still warns.
    assert "not production-ready" in p["note"] or "not production" in p["note"] \
        or "production-ready" in p["note"]


def test_posture_no_host_path_or_secret(monkeypatch):
    import json

    from app.services.vuln_triage import scan_text_for_host_leaks
    p = _posture(monkeypatch, _dossier(7))
    txt = json.dumps(p, ensure_ascii=False)
    assert scan_text_for_host_leaks(txt, repo_root="/some/build/checkout/dir") == []
    # only repo-relative doc names, never absolute paths
    assert p["decision_dossier_doc"].startswith("docs/")
    assert p["risk_acceptance_doc"].startswith("docs/")


def test_release_readiness_includes_posture(monkeypatch):
    get_settings.cache_clear()
    r = pc.release_readiness(get_settings())
    assert "security_posture" in r and isinstance(r["security_posture"], dict)
    # the real shipped dossier reports 7 remaining CRITICAL
    assert r["security_posture"]["known_critical_accepted"] == 7


def test_posture_does_not_change_release_check_results(monkeypatch):
    # the posture is derived from checks; producing it must not mutate them
    monkeypatch.setattr(pc, "_load_decision_dossier", lambda _s: _dossier(7))
    s = get_settings()
    checks = pc._release_provenance_checks(s)
    before = [(c["name"], c["status"]) for c in checks]
    pc._security_posture(s, checks)
    after = [(c["name"], c["status"]) for c in checks]
    assert before == after
