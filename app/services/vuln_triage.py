"""Phase 10B: vulnerability triage + controlled-remediation reporting.

Parses a Trivy JSON report into a leak-free, classified summary and produces a
machine-readable remediation diff (before/after). Classification uses the
package PURL / identifier so a library that Trivy attributes to the OS target
but is actually vendored inside a Python wheel (e.g. an ``pkg:rpm/almalinux/…``
inside psycopg2-binary) is correctly bucketed as a Python-wheel-bundled finding
rather than an OS package.

No host paths, secrets, private URLs, or raw environment ever appear in the
output — package *paths* are dropped; only CVE ids, package names, versions,
advisory-source names, and counts are kept.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")

# target-type buckets
OS = "os"
PYTHON = "python"
NPM = "npm"
BINARY = "binary"
APPLICATION = "application"
OTHER = "other"


def _bucket(result_class: str | None, result_type: str | None, purl: str | None) -> str:
    """Classify a finding. PURL wins over the Trivy target class so a wheel-
    vendored RPM/almalinux lib (bundled in a Python package) is not counted as
    an OS package."""
    p = (purl or "").lower()
    rt = (result_type or "").lower()
    rc = (result_class or "").lower()
    if p.startswith("pkg:pypi/"):
        return PYTHON
    if p.startswith(("pkg:npm/",)):
        return NPM
    if p.startswith(("pkg:rpm/", "pkg:apk/")) and rc == "os-pkgs" and rt in ("debian", "ubuntu"):
        # Trivy attributed an RPM/APK PURL to a debian OS target => it detected a
        # vendored library shipped inside another artifact, not the OS package.
        return BINARY
    if rc == "os-pkgs":
        return OS
    if rt in ("python-pkg", "pip"):
        return PYTHON
    if rt in ("node-pkg", "npm", "yarn"):
        return NPM
    if rc == "lang-pkgs":
        return PYTHON if "pip" in rt or "python" in rt else OTHER
    if rc in ("secret", "config", "license"):
        return APPLICATION
    return OTHER


# ---- scanner provenance (Phase 10B.2) ----------------------------------------
# Keep THREE scanner identifiers strictly distinct and NEVER conflate them:
#   image_id    : `docker inspect .Id` — the scanner image's content id, a full
#                 `sha256:<64hex>` config digest. Local; always available.
#   repo_digest : a REGISTRY RepoDigest `name@sha256:<64hex>` (`.RepoDigests[0]`)
#                 — exists ONLY when the image was pulled from / pushed to a
#                 registry. Absent for a locally loaded / air-gapped scanner.
#   operator_verified : the operator attested the artifact out-of-band.
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPO_DIGEST_RE = re.compile(r"^[A-Za-z0-9][\w.\-/:]*@sha256:[0-9a-f]{64}$")

SCANNER_PROV_DIGEST_PINNED = "digest_pinned"
SCANNER_PROV_LOCAL_ID_VERIFIED = "local_image_id_verified"
SCANNER_PROV_UNVERIFIED = "unverified"


def classify_scanner_provenance(image_id: str = "", repo_digest: str = "",
                                operator_verified: bool = False) -> dict:
    """Honestly classify a scanner artifact's provenance WITHOUT ever
    synthesizing a RepoDigest from the image id.

    Returns {status, image_id, repo_digest, operator_verified, errors[]}:
      digest_pinned           - a real, well-formed, NON-synthetic repo_digest.
      local_image_id_verified - no usable repo_digest, but a full-sha256 image_id
                                AND operator_verified (out-of-band attestation).
      unverified              - anything else (recorded but not verified).

    A repo_digest whose sha256 body equals (or short-prefixes) the image id is
    SYNTHETIC and is rejected (errors: repo_digest_synthetic_from_image_id).
    A non-full-sha256 image_id (e.g. a 12-hex short id) is rejected
    (errors: image_id_not_full_sha256) and cannot back local_image_id_verified.
    """
    image_id = (image_id or "").strip()
    repo_digest = (repo_digest or "").strip()
    errors: list[str] = []

    norm_id = None
    if image_id:
        if _SHA256_RE.match(image_id):
            norm_id = image_id
        else:
            errors.append("image_id_not_full_sha256")

    norm_digest = None
    if repo_digest:
        if not _REPO_DIGEST_RE.match(repo_digest):
            errors.append("repo_digest_malformed")
        else:
            dig = repo_digest.split("@", 1)[1]            # 'sha256:<hex>'
            dig_hex = dig.split(":", 1)[1].lower()
            id_hex = (norm_id or image_id).replace("sha256:", "").lower()
            synthetic = bool(id_hex) and (
                dig == norm_id
                or dig_hex == id_hex
                or (len(id_hex) >= 12 and dig_hex.startswith(id_hex))
            )
            if synthetic:
                errors.append("repo_digest_synthetic_from_image_id")
            else:
                norm_digest = repo_digest

    if norm_digest:
        status = SCANNER_PROV_DIGEST_PINNED
    elif norm_id and operator_verified:
        status = SCANNER_PROV_LOCAL_ID_VERIFIED
    else:
        status = SCANNER_PROV_UNVERIFIED

    return {
        "status": status,
        "image_id": norm_id,          # full sha256 or None (short/invalid dropped)
        "repo_digest": norm_digest,   # real RepoDigest or None (synthetic dropped)
        "operator_verified": bool(operator_verified),
        "errors": errors,
    }


# ---- artifact leak scanning (Phase 10B.2) ------------------------------------
# Release artifacts (SBOM, descriptors) must carry NO host-build-machine paths or
# secrets. An image SBOM legitimately contains IN-IMAGE paths (/usr/lib/…, /app);
# those are fine. We flag only host markers (the build repo path, /Users/<name>)
# and secret-like content. Returns count-only findings (never echoes the secret).
# High-precision only: an image SBOM legitimately mentions words like "password"
# in package descriptions, so the secret matcher is env-style (UPPERCASE KEY=value
# with a non-trivial value), not generic prose.
_LEAK_PATTERNS = (
    ("home_users_path", re.compile(r"/Users/[A-Za-z0-9._-]+")),
    ("secret_env_assignment",
     re.compile(r"(?:PASSWORD|PASSWD|SECRET|SECRET_KEY|API_KEY|APIKEY|ACCESS_KEY|TOKEN)=\S{6,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("db_url_with_credentials", re.compile(r"postgres(?:ql)?://[^\s\"']+:[^\s\"'@/]+@")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def scan_text_for_host_leaks(text: str, repo_root: str = "") -> list[dict]:
    """Return a list of {marker, count} for host paths / secrets found in an
    artifact's text. Empty list == clean. Never includes the matched content."""
    findings: list[dict] = []
    rr = (repo_root or "").rstrip("/")
    if rr and rr in text:
        findings.append({"marker": "build_repo_path", "count": text.count(rr)})
    for name, rx in _LEAK_PATTERNS:
        hits = rx.findall(text)
        if hits:
            findings.append({"marker": name, "count": len(hits)})
    return findings


def parse_report(trivy: dict) -> dict:
    """Return a classified, deduped, leak-free triage of a Trivy report."""
    artifact = str(trivy.get("ArtifactName") or "")[:120]
    rows: list[dict] = []
    for r in (trivy.get("Results") or []):
        rc, rt = r.get("Class"), r.get("Type")
        for v in (r.get("Vulnerabilities") or []):
            purl = (v.get("PkgIdentifier") or {}).get("PURL")
            rows.append({
                "cve": v.get("VulnerabilityID"),
                "severity": (v.get("Severity") or "UNKNOWN").upper(),
                "bucket": _bucket(rc, rt, purl),
                "target_type": (rt or None),
                "package": v.get("PkgName"),
                "installed": v.get("InstalledVersion"),
                "fixed": v.get("FixedVersion") or None,
                "advisory": (v.get("DataSource") or {}).get("Name"),
                "purl_kind": (purl.split(":", 2)[1].split("/", 1)[0] if purl and ":" in purl else None),
            })

    def sev_counts(items):
        c = {s: 0 for s in SEVERITIES}
        for x in items:
            c[x["severity"]] = c.get(x["severity"], 0) + 1
        return c

    def bucket_counts(items):
        c: dict[str, int] = {}
        for x in items:
            c[x["bucket"]] = c.get(x["bucket"], 0) + 1
        return c

    by_sev: dict[str, list] = {s: [x for x in rows if x["severity"] == s] for s in SEVERITIES}
    out = {
        "artifact": artifact,
        "total_findings": len(rows),
        "severity_counts": sev_counts(rows),
        "unique_cve_count": len({x["cve"] for x in rows}),
    }
    for sev in ("CRITICAL", "HIGH"):
        items = by_sev[sev]
        uniq = sorted({x["cve"] for x in items})
        withfix = [x for x in items if x["fixed"]]
        out[sev.lower()] = {
            "rows": len(items),
            "unique_cves": len(uniq),
            "by_bucket": bucket_counts(items),
            "fixed_available_rows": len(withfix),
            "no_fix_rows": len(items) - len(withfix),
            "findings": [
                {k: x[k] for k in ("cve", "bucket", "package", "installed", "fixed", "advisory", "purl_kind")}
                for x in sorted(items, key=lambda x: (x["bucket"], x["package"] or "", x["cve"] or ""))
            ],
        }
    return out


def cve_key_set(trivy: dict, severity: str = "CRITICAL") -> set[tuple]:
    """(cve, package, installed) tuples at a severity — for before/after diffs."""
    keys = set()
    for r in (trivy.get("Results") or []):
        for v in (r.get("Vulnerabilities") or []):
            if (v.get("Severity") or "").upper() == severity.upper():
                keys.add((v.get("VulnerabilityID"), v.get("PkgName"), v.get("InstalledVersion")))
    return keys


# ---- remediation diff report ------------------------------------------------
def _integrity(body: dict, hmac_key: str | None) -> dict:
    import hmac as _hmac

    data = json.dumps({k: v for k, v in body.items() if k != "integrity"},
                      sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    if hmac_key:
        return {"scheme": "hmac_sha256",
                "hash": _hmac.new(hmac_key.encode("utf-8"), data, hashlib.sha256).hexdigest()}
    return {"scheme": "sha256", "hash": hashlib.sha256(data).hexdigest()}


def remediation_report(*, before: dict, after: dict, before_meta: dict, after_meta: dict,
                       exceptions: list | None = None, tests: dict | None = None,
                       hmac_key: str | None = None) -> dict:
    """Build a machine-readable before/after remediation diff. ``*_meta`` carry
    release_id / scanner_version / db_timestamp / base_digest / lock_hash
    (basenames/hashes only). No host paths / secrets."""
    report: dict = {"report_version": 1, "kind": "remediation_diff"}
    for sev in ("CRITICAL", "HIGH"):
        b = cve_key_set(before, sev)
        a = cve_key_set(after, sev)
        report[sev.lower()] = {
            "added": sorted(f"{c}|{p}" for c, p, _ in (a - b)),
            "removed": sorted(f"{c}|{p}" for c, p, _ in (b - a)),
            "unchanged_count": len(a & b),
            "before_count": len(b),
            "after_count": len(a),
        }
    report["before"] = {k: before_meta.get(k) for k in
                        ("release_id", "scanner_version", "db_timestamp", "base_python_digest",
                         "base_node_digest", "python_lock_sha256")}
    report["after"] = {k: after_meta.get(k) for k in
                       ("release_id", "scanner_version", "db_timestamp", "base_python_digest",
                        "base_node_digest", "python_lock_sha256")}
    report["severity_before"] = parse_report(before)["severity_counts"]
    report["severity_after"] = parse_report(after)["severity_counts"]
    report["exceptions"] = exceptions or []
    report["tests"] = tests or {}
    crit_after = report["critical"]["after_count"]
    n_exc = len(exceptions or [])
    report["overall_status"] = "clean" if crit_after == 0 else (
        "critical_remaining_with_exceptions" if crit_after <= n_exc else "critical_remaining")
    report["integrity"] = _integrity(report, hmac_key)
    return report


def verify_report_integrity(report: dict, hmac_key: str | None = None) -> bool:
    integ = report.get("integrity") or {}
    scheme = integ.get("scheme")
    expected = _integrity(report, hmac_key if scheme == "hmac_sha256" else None)
    return expected["scheme"] == scheme and expected["hash"] == integ.get("hash")


def load_trivy(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None
