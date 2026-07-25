"""Phase 10B.3: vulnerability exception *proposals* (advisory, never active).

Proposals live in their OWN file (`vulnerability-exception-proposals.yml`) and
are NEVER enforced: release-check reads ACTIVE exceptions only from
`vulnerability-exceptions.yml` (see ``vuln_exceptions``). This module loads and
validates proposals, cross-checks them against the canonical CVE inventory, and
produces the decision-dossier verdict used by release-check.

Hard invariants enforced here:
  * a proposal MUST leave approval fields empty (``approved_by`` / ``approved_at``
    / ``approval_reference``). A filled approval field is an ERROR — approval
    belongs in the active-exceptions file, not here.
  * ``evidence_hash`` must match the canonical inventory record for that CVE.
  * ``reachability`` / ``recommended_decision`` must be from the allowed enums.
  * a CVE recommended as ``exception_candidate`` must satisfy the exception-
    candidacy criteria (no fix, reachability assessed, compensating control, …).

Leak-free: CVE ids / package names / versions / enum values / counts only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REACHABILITY = ("reachable", "potentially_reachable",
                "not_reachable_with_evidence", "unknown")
DECISIONS = ("remediate", "wait_for_upstream", "exception_candidate",
             "reject_exception", "insufficient_evidence")
APPROVAL_FIELDS = ("approved_by", "approved_at", "approval_reference")
REQUIRED = ("vulnerability_id", "package", "installed_version", "reachability",
            "evidence_summary", "risk_summary", "recommended_decision",
            "evidence_hash", "tracking_reference")

# free-text fields that must never carry secrets / host paths
_TEXT_FIELDS = ("evidence_summary", "risk_summary", "primary_source",
                "proposed_expiry_basis", "remediation_available")


def load_proposals(path: str | Path) -> dict:
    import yaml

    p = Path(path)
    if not p.is_file():
        return {"meta": {}, "proposals": [], "loaded": False}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("meta", {})
    data.setdefault("proposals", [])
    data["loaded"] = True
    return data


def _record_hash(record: dict) -> str:
    body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _leak_scan(text: str) -> bool:
    from app.services.vuln_triage import scan_text_for_host_leaks

    return bool(scan_text_for_host_leaks(text or ""))


def validate_proposals(proposals_doc: dict, inventory: dict) -> dict:
    """Validate proposals against the canonical inventory. Returns a verdict with
    per-proposal errors and dossier-level rollups. Fail-closed on any error."""
    proposals = proposals_doc.get("proposals") or []
    remaining = [r for r in inventory.get("records", [])
                 if r.get("current_status") == "remaining"]
    remaining_by_key = {(r["vulnerability_id"], r["package"]): r for r in remaining}
    inv_hash = {(r["vulnerability_id"], r["package"]): _record_hash(r) for r in remaining}

    errors: list[str] = []
    valid, active_leak = [], []
    covered_keys = set()
    exception_candidates = []

    # meta must declare non-active
    if (proposals_doc.get("meta") or {}).get("active") is True:
        errors.append("meta.active must be false — proposals are never active")

    for i, pr in enumerate(proposals):
        tag = f"proposal[{i}] {pr.get('vulnerability_id')}|{pr.get('package')}"
        missing = [f for f in REQUIRED if not pr.get(f)]
        if missing:
            errors.append(f"{tag}: missing {missing}")
            continue
        # approval fields MUST be empty
        filled = [f for f in APPROVAL_FIELDS if pr.get(f)]
        if filled:
            errors.append(f"{tag}: approval field(s) {filled} filled — proposals must NOT be approved here")
            continue
        if pr["reachability"] not in REACHABILITY:
            errors.append(f"{tag}: bad reachability {pr['reachability']!r}")
            continue
        if pr["recommended_decision"] not in DECISIONS:
            errors.append(f"{tag}: bad recommended_decision {pr['recommended_decision']!r}")
            continue
        key = (pr["vulnerability_id"], pr["package"])
        rec = remaining_by_key.get(key)
        if rec is None:
            errors.append(f"{tag}: not a REMAINING CRITICAL in the canonical inventory")
            continue
        # package/version must match the inventory's latest installed version
        latest = inventory["releases"][-1]
        inv_ver = (rec["per_release"].get(latest) or {}).get("installed_version")
        if pr["installed_version"] != inv_ver:
            errors.append(f"{tag}: installed_version {pr['installed_version']!r} != inventory {inv_ver!r}")
            continue
        # evidence_hash must match
        if pr["evidence_hash"] != inv_hash.get(key):
            errors.append(f"{tag}: evidence_hash mismatch vs canonical record")
            continue
        # not_reachable_with_evidence requires >=2 compensating controls / evidences
        if pr["reachability"] == "not_reachable_with_evidence" \
                and len(pr.get("compensating_controls") or []) < 2:
            errors.append(f"{tag}: not_reachable_with_evidence requires >=2 evidences")
            continue
        # exception_candidate criteria
        if pr["recommended_decision"] == "exception_candidate":
            crit = _exception_candidacy(pr, rec)
            if crit["blockers"]:
                errors.append(f"{tag}: exception_candidate blocked by {crit['blockers']}")
                continue
            exception_candidates.append(key)
        # leak scan free-text
        if any(_leak_scan(str(pr.get(f, ""))) for f in _TEXT_FIELDS):
            errors.append(f"{tag}: free-text leak (host path/secret)")
            active_leak.append(key)
            continue
        covered_keys.add(key)
        valid.append(pr["vulnerability_id"])

    reachability_complete = all(k in covered_keys for k in remaining_by_key)
    return {
        "loaded": bool(proposals_doc.get("loaded")),
        "proposal_count": len(proposals),
        "valid_count": len(valid),
        "remaining_critical_count": len(remaining_by_key),
        "covered_remaining": len(covered_keys),
        "reachability_complete": reachability_complete,
        "exception_candidates": len(exception_candidates),
        "all_unapproved": True if proposals and not errors else (not proposals),
        "errors": errors,
        "dossier_valid": (not errors) and reachability_complete and bool(proposals),
    }


def _exception_candidacy(pr: dict, rec: dict) -> dict:
    """Criteria (Phase 10B.3 §8): an entry may be an exception_candidate ONLY if
    it has no fix, reachability is assessed, a compensating control exists, and it
    matches the canonical record. A CVE with a fix is NOT a candidate."""
    blockers = []
    if rec.get("has_fix"):
        blockers.append("fixed_version_available")
    if pr.get("reachability") not in REACHABILITY or pr.get("reachability") == "unknown":
        blockers.append("reachability_not_assessed")
    if not (pr.get("compensating_controls")):
        blockers.append("no_compensating_control")
    if not pr.get("tracking_reference"):
        blockers.append("no_tracking_reference")
    return {"blockers": blockers}


def proposals_are_inactive(proposals_path: str | Path, active_exceptions_path: str | Path) -> dict:
    """Confirm the two files are distinct and the proposals file contributes ZERO
    active exceptions (belt-and-suspenders for release-check)."""
    pp, ap = Path(proposals_path), Path(active_exceptions_path)
    same_file = pp.resolve() == ap.resolve() if pp.exists() and ap.exists() else False
    return {
        "distinct_files": not same_file,
        "proposals_contribute_active_exceptions": 0,  # by construction — never read as active
        "ok": not same_file,
    }


# ---- decision dossier (machine artifact release-check reads) -----------------
def build_dossier(inventory: dict, proposals_doc: dict, *,
                  reconciliation_issues: list[dict] | None = None,
                  scanner_provenance: dict | None = None,
                  wheel_reproducibility: dict | None = None,
                  generated_for_release: str = "", hmac_key: str | None = None) -> dict:
    """Assemble the machine-readable decision dossier with a canonical integrity
    hash. Embeds the canonical inventory, the reconciliation corrections, the
    proposals-validation verdict, the scanner-provenance evidence, and the
    wheel-reproducibility status."""
    validation = validate_proposals(proposals_doc, inventory)
    dossier = {
        "kind": "vulnerability_decision_dossier",
        "phase": "10B.3",
        "generated_for_release": generated_for_release or None,
        "inventory": inventory,
        "reconciliation": {
            "issues": reconciliation_issues or [],
            "reason_codes": sorted({i.get("reason_code") for i in (reconciliation_issues or [])}),
            "documented": all(i.get("reason_code") for i in (reconciliation_issues or [])),
        },
        "proposals_validation": validation,
        "scanner_provenance": scanner_provenance or {"status": "unverified",
                                                     "operator_approval": False},
        "wheel_reproducibility": wheel_reproducibility or {"status": "unknown"},
    }
    dossier["integrity"] = _dossier_integrity(dossier, hmac_key)
    return dossier


def _dossier_integrity(dossier: dict, hmac_key: str | None) -> dict:
    body = {k: v for k, v in dossier.items() if k != "integrity"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    import hmac as _hmac

    if hmac_key:
        key = hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key
        return {"scheme": "hmac-sha256", "value": _hmac.new(key, raw, hashlib.sha256).hexdigest()}
    return {"scheme": "sha256", "value": hashlib.sha256(raw).hexdigest()}


def verify_dossier_integrity(dossier: dict, hmac_key: str | None = None) -> bool:
    import hmac as _hmac

    want = dossier.get("integrity") or {}
    if not want.get("value"):
        return False
    use_key = hmac_key if want.get("scheme") == "hmac-sha256" else None
    got = _dossier_integrity(dossier, use_key)
    return _hmac.compare_digest(str(want.get("value")), str(got.get("value"))) \
        and want.get("scheme") == got.get("scheme")


def dossier_checks(dossier: dict | None, *, prod: bool, hmac_key: str | None = None) -> list[dict]:
    """The Phase 10B.3 release-check gates. Returns a list of
    {name, status, detail} dicts (status ∈ pass|warn|fail)."""
    PASS, WARN, FAIL = "pass", "warn", "fail"

    def c(name, status, detail):
        return {"name": name, "status": status, "detail": detail}

    out: list[dict] = []
    if not dossier:
        # no dossier present — cannot assert consistency; not a hard fail by itself
        for n in ("vulnerability_inventory_consistent", "vulnerability_reachability_complete",
                  "vulnerability_decision_dossier_valid"):
            out.append(c(n, WARN if not prod else FAIL, "no decision dossier present"))
        out.append(c("scanner_operator_approval_present", WARN, "no dossier — scanner approval unknown"))
        out.append(c("wheel_reproducibility_status", WARN, "no dossier — wheel reproducibility unknown"))
        return out

    integ_ok = verify_dossier_integrity(dossier, hmac_key)
    inv = dossier.get("inventory") or {}
    from app.services import vuln_inventory as vi
    inv_integ = vi.verify_inventory_integrity(inv) if inv else False
    recon = dossier.get("reconciliation") or {}
    documented = recon.get("documented", False)

    # 1. inventory consistency: integrity valid AND every reconciliation issue is
    #    documented with a reason code (a corrected report is consistent).
    if inv_integ and documented:
        out.append(c("vulnerability_inventory_consistent", PASS,
                     f"inventory integrity ok; {len(recon.get('issues') or [])} reconciliation "
                     f"issue(s), all documented {recon.get('reason_codes') or []}"))
    else:
        out.append(c("vulnerability_inventory_consistent", FAIL,
                     "inventory integrity invalid or undocumented reconciliation mismatch"))

    # 2. reachability completeness
    val = dossier.get("proposals_validation") or {}
    if val.get("reachability_complete") and val.get("remaining_critical_count", 0) > 0:
        out.append(c("vulnerability_reachability_complete", PASS,
                     f"all {val['remaining_critical_count']} remaining CRITICAL(s) have a reachability judgement"))
    else:
        out.append(c("vulnerability_reachability_complete", FAIL if prod else WARN,
                     "not every remaining CRITICAL has a reachability judgement"))

    # 3. dossier validity (integrity + proposals valid + all unapproved)
    if integ_ok and val.get("dossier_valid") and val.get("all_unapproved"):
        out.append(c("vulnerability_decision_dossier_valid", PASS,
                     f"dossier integrity ok; {val.get('valid_count')} proposals valid, all unapproved "
                     f"({val.get('exception_candidates')} exception_candidate(s))"))
    else:
        detail = "dossier integrity mismatch" if not integ_ok else \
            ("proposals invalid" if not val.get("dossier_valid") else "a proposal is approved in the proposals file")
        out.append(c("vulnerability_decision_dossier_valid", FAIL, detail))

    # 4. scanner operator approval present (informational; unverified => not present)
    prov = dossier.get("scanner_provenance") or {}
    if prov.get("operator_approval") is True or prov.get("status") in ("digest_pinned", "local_image_id_verified", "verified"):
        out.append(c("scanner_operator_approval_present", PASS,
                     f"scanner provenance {prov.get('status')}"))
    else:
        out.append(c("scanner_operator_approval_present", WARN,
                     f"scanner provenance {prov.get('status', 'unverified')} — no operator approval recorded "
                     "(not fabricated); see runbook"))

    # 5. wheel reproducibility status (non-determinism is documented, not fatal)
    wr = dossier.get("wheel_reproducibility") or {}
    st = wr.get("status", "unknown")
    if st in ("bit_reproducible", "reproducible"):
        out.append(c("wheel_reproducibility_status", PASS, "psycopg2 wheel bit-for-bit reproducible"))
    elif st == "not_bit_reproducible":
        out.append(c("wheel_reproducibility_status", WARN,
                     f"psycopg2 wheel NOT bit-reproducible (differing factor: {wr.get('differing_factor')}); "
                     "functional deps identical, hash captured per build"))
    else:
        out.append(c("wheel_reproducibility_status", WARN, f"wheel reproducibility {st}"))
    return out


def render_dossier_markdown(dossier: dict, proposals_doc: dict | None = None) -> str:
    """Render the human-readable per-CVE decision table + provenance/repro notes.
    Contains only CVE/package/enum/count data — no host paths or secrets."""
    inv = dossier.get("inventory") or {}
    proposals = {(p["vulnerability_id"], p["package"]): p
                 for p in ((proposals_doc or {}).get("proposals") or [])}
    remaining = [r for r in inv.get("records", []) if r.get("current_status") == "remaining"]
    latest = (inv.get("releases") or ["?"])[-1]
    L = []
    L.append("# Vulnerability Decision Dossier — Phase 10B.3\n")
    L.append(f"- Release under review: **{dossier.get('generated_for_release') or '?'}**")
    L.append(f"- Canonical inventory integrity: `{(inv.get('integrity') or {}).get('value','?')}`")
    L.append(f"- Dossier integrity: `{(dossier.get('integrity') or {}).get('value','?')}`")
    prov = dossier.get("scanner_provenance") or {}
    L.append(f"- Scanner provenance: **{prov.get('status','?')}** (operator_approval="
             f"{prov.get('operator_approval')}) — {prov.get('note','')}")
    wr = dossier.get("wheel_reproducibility") or {}
    L.append(f"- psycopg2 wheel reproducibility: **{wr.get('status','?')}** "
             f"(differing factor: {wr.get('differing_factor','n/a')}; functional deps identical: "
             f"{wr.get('functional_deps_identical')})")
    recon = dossier.get("reconciliation") or {}
    if recon.get("issues"):
        L.append("\n## Reporting corrections")
        for i in recon["issues"]:
            L.append(f"- `{i.get('vulnerability_id')}` ({i.get('package')}): "
                     f"**{i.get('reason_code')}** — {i.get('detail')}")
    L.append("\n## Remaining CRITICAL decision table\n")
    L.append("| CVE | package/version | class | reachability | fixed | recommended | max days | tracking | evidence_hash |")
    L.append("|-----|-----------------|-------|--------------|-------|-------------|----------|----------|---------------|")
    for r in remaining:
        key = (r["vulnerability_id"], r["package"])
        pr = proposals.get(key, {})
        ver = (r["per_release"].get(latest) or {}).get("installed_version", "?")
        L.append("| {cve} | {pkg} {ver} | {cls} | {reach} | {fix} | {dec} | {days} | {trk} | `{eh}` |".format(
            cve=r["vulnerability_id"], pkg=r["package"], ver=ver,
            cls=pr.get("class", "?"), reach=pr.get("reachability", "?"),
            fix=(r.get("fixed_version") or "none"),
            dec=pr.get("recommended_decision", "?"), days=pr.get("proposed_max_duration_days", "?"),
            trk=pr.get("tracking_reference", "?"), eh=(pr.get("evidence_hash", "?"))[:22]))
    L.append("\n## Operator options (per CVE)\n")
    L.append("For each CVE above, choose one:")
    L.append("- **approve** → copy the proposal into `vulnerability-exceptions.yml`, fill "
             "`approved_by`/`approved_at`/`approval_reference` and a concrete `expires_at`.")
    L.append("- **reject** → keep it as an open CRITICAL (release-check stays FAIL).")
    L.append("- **investigate** → request more evidence before deciding.")
    L.append("- **wait_for_upstream** → track the Debian/upstream fix; no exception.")
    L.append("\n_No proposal here is active. `release-check` continues to FAIL on the "
             "unapproved CRITICALs until an operator explicitly approves exceptions._")
    return "\n".join(L) + "\n"

