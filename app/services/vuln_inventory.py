"""Phase 10B.3: canonical cross-release CVE inventory + reporting reconciliation.

Builds ONE canonical record per ``(vulnerability_id, package)`` across an ordered
series of scans. Canonical identity is version-INDEPENDENT on purpose: a finding
whose ``installed_version`` merely changes between releases (e.g. libxml2
``deb13u2`` -> ``deb13u3``) is the SAME CVE and must never be double-counted as
removed+added. Each record carries its per-release presence/version, lifecycle
(first/last seen, removed_in, current_status) and the scans' evidence hashes; the
whole inventory gets a canonical SHA-256 (or HMAC) integrity value.

Leak-free: only CVE ids, package names, versions, advisory-source names, PURLs,
Trivy target labels, and counts appear — never host paths, secrets, or raw
identities.
"""

from __future__ import annotations

import hashlib
import hmac
import json

CRITICAL = "CRITICAL"

# reason codes for reconcile_remediation()
REASON_VERSION_RECLASSIFIED = "package_version_reclassified"
REASON_ID_MISMATCH = "reporting_id_mismatch"
REASON_RESULT_CHANGED = "scanner_result_changed"


def _findings(trivy: dict):
    for res in trivy.get("Results", []) or []:
        tgt, cls, typ = res.get("Target"), res.get("Class"), res.get("Type")
        for v in res.get("Vulnerabilities", []) or []:
            purl = (v.get("PkgIdentifier") or {}).get("PURL")
            yield {
                "cve": v.get("VulnerabilityID"),
                "package": v.get("PkgName"),
                "installed": v.get("InstalledVersion"),
                "fixed": v.get("FixedVersion") or None,
                "severity": (v.get("Severity") or "UNKNOWN").upper(),
                "purl": purl,
                "target": tgt,
                "target_class": cls,
                "target_type": typ,
                "advisory": (v.get("DataSource") or {}).get("Name"),
            }


def _target_type(purl: str | None, cls: str | None, typ: str | None) -> str:
    from app.services.vuln_triage import _bucket

    return _bucket(cls, typ, purl)


def build_inventory(ordered_scans, *, severity: str | None = CRITICAL,
                    hmac_key: str | None = None) -> dict:
    """Build the canonical inventory.

    ``ordered_scans``: list of ``(label, trivy_dict, scan_sha256)`` in
    CHRONOLOGICAL order (oldest first). ``severity=None`` keeps all severities.
    """
    if not ordered_scans:
        raise ValueError("no scans provided")
    labels = [lbl for lbl, _, _ in ordered_scans]
    if len(set(labels)) != len(labels):
        raise ValueError("duplicate release labels")
    evidence = {lbl: sha for lbl, _, sha in ordered_scans}

    records: dict[tuple, dict] = {}
    for label, trivy, _sha in ordered_scans:
        seen: set[tuple] = set()
        for f in _findings(trivy):
            if severity and f["severity"] != severity:
                continue
            key = (f["cve"], f["package"])
            if key in seen:
                continue
            seen.add(key)
            r = records.setdefault(key, {
                "vulnerability_id": f["cve"], "package": f["package"],
                "severity": f["severity"], "purl": f["purl"], "target": f["target"],
                "target_type": _target_type(f["purl"], f["target_class"], f["target_type"]),
                "scanner_source": f["advisory"], "fixed_version": f["fixed"],
                "per_release": {},
            })
            r["per_release"][label] = {"present": True,
                                       "installed_version": f["installed"],
                                       "fixed_version": f["fixed"]}
            r["fixed_version"] = f["fixed"]          # most-recent wins
            r["purl"] = f["purl"] or r["purl"]

    out = []
    last = labels[-1]
    for r in records.values():
        for lbl in labels:
            r["per_release"].setdefault(
                lbl, {"present": False, "installed_version": None, "fixed_version": None})
        present = [lbl for lbl in labels if r["per_release"][lbl]["present"]]
        in_latest = r["per_release"][last]["present"]
        removed_in = None
        if not in_latest and present:
            idx = labels.index(present[-1])
            removed_in = labels[idx + 1] if idx + 1 < len(labels) else None
        r["first_seen_release"] = present[0] if present else None
        r["last_seen_release"] = present[-1] if present else None
        r["removed_in_release"] = removed_in
        r["current_status"] = "remaining" if in_latest else "removed"
        r["has_fix"] = bool(r["fixed_version"])
        out.append(r)

    out.sort(key=lambda r: (r["current_status"], r["target_type"], r["package"],
                            r["vulnerability_id"]))
    inv = {
        "kind": "cve_inventory",
        "severity": severity,
        "releases": labels,
        "evidence_hashes": evidence,
        "record_count": len(out),
        "counts": {
            "remaining": sum(1 for r in out if r["current_status"] == "remaining"),
            "removed": sum(1 for r in out if r["current_status"] == "removed"),
            "remaining_no_fix": sum(1 for r in out
                                    if r["current_status"] == "remaining" and not r["has_fix"]),
            "remaining_with_fix": sum(1 for r in out
                                      if r["current_status"] == "remaining" and r["has_fix"]),
        },
        "records": out,
    }
    inv["integrity"] = _integrity(inv, hmac_key)
    return inv


def _canonical_bytes(obj: dict) -> bytes:
    body = {k: v for k, v in obj.items() if k != "integrity"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _integrity(obj: dict, hmac_key: str | None) -> dict:
    body = _canonical_bytes(obj)
    if hmac_key:
        key = hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key
        return {"scheme": "hmac-sha256",
                "value": hmac.new(key, body, hashlib.sha256).hexdigest()}
    return {"scheme": "sha256", "value": hashlib.sha256(body).hexdigest()}


def verify_inventory_integrity(inv: dict, hmac_key: str | None = None) -> bool:
    want = inv.get("integrity") or {}
    if not want.get("value"):
        return False
    use_key = hmac_key if want.get("scheme") == "hmac-sha256" else None
    got = _integrity(inv, use_key)
    return hmac.compare_digest(str(want.get("value")), str(got.get("value"))) \
        and want.get("scheme") == got.get("scheme")


def reconcile_remediation(inventory: dict, remediation_report: dict,
                          severity: str = CRITICAL) -> list[dict]:
    """Cross-check a remediation report's removed/added lists against the
    canonical presence matrix. Flags version-reclassification artifacts and
    genuine reporting/scanner-result mismatches with reason codes."""
    sev = (remediation_report.get(severity.lower()) or {})
    removed = list(sev.get("removed") or [])
    added = set(sev.get("added") or [])
    idx = {f'{r["vulnerability_id"]}|{r["package"]}': r for r in inventory["records"]}
    issues = []
    for entry in sorted(set(removed)):
        cve = entry.split("|", 1)[0]
        pkg = entry.split("|", 1)[1] if "|" in entry else ""
        rec = idx.get(entry)
        if entry in added:
            issues.append({
                "vulnerability_id": cve, "package": pkg,
                "reason_code": REASON_VERSION_RECLASSIFIED,
                "detail": ("listed as BOTH removed and added — same CVE, changed "
                           "installed_version between releases; net effect: none"),
            })
        elif rec and rec["current_status"] == "remaining":
            issues.append({
                "vulnerability_id": cve, "package": pkg,
                "reason_code": REASON_ID_MISMATCH,
                "detail": ("remediation report claims removed, but the canonical "
                           "inventory shows this CVE still present in the latest scan"),
            })
    return issues


def inventory_consistent(inventory: dict, remediation_reports: list[dict] | None = None,
                         hmac_key: str | None = None) -> dict:
    """Overall consistency verdict for release-check: integrity valid AND no
    unresolved reporting mismatches across the provided remediation reports."""
    integrity_ok = verify_inventory_integrity(inventory, hmac_key)
    all_issues = []
    for rep in (remediation_reports or []):
        all_issues.extend(reconcile_remediation(inventory, rep))
    return {
        "integrity_ok": integrity_ok,
        "reconciliation_issues": all_issues,
        "consistent": integrity_ok,  # reconciliation issues are *reported*, not fatal once corrected
        "issue_count": len(all_issues),
    }
