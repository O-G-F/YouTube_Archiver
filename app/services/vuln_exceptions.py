"""Phase 10B: vulnerability exception policy.

An exception is an operator's TIME-BOUND, documented acceptance of a specific
un-fixable / unreachable vulnerability. It is never automatic: CRITICALs are
never auto-ignored, an exception with no ``expires_at`` or no ``reason`` is
invalid, and an expired exception makes release-check FAIL. An exception only
applies while the exact (vulnerability_id, package, installed_version) still
matches — a package/version bump forces re-evaluation.

The file is ``vulnerability-exceptions.yml`` (repo-tracked template, empty until
an operator adds entries). It must contain no secrets, personal data, or
private URLs.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

REQUIRED_FIELDS = (
    "vulnerability_id", "package", "installed_version", "reason",
    "reachability_assessment", "compensating_control",
    "approved_by", "approved_at", "expires_at", "tracking_reference",
)
_LEAK_SUBSTRINGS = ("/users/", "/home/", "password", "secret", "token", "cookie",
                    "@gmail", "@ ", "://")


def _parse_dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", ""))
    except ValueError:
        return None


def load_exceptions(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    try:
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return []
    exc = data.get("exceptions") if isinstance(data, dict) else None
    return [e for e in (exc or []) if isinstance(e, dict)]


def validate_exceptions(exceptions: list[dict], *, now: datetime | None = None) -> dict:
    """Return {valid, active, expired, invalid, errors}. ``active`` = valid AND
    not yet expired; those are the only ones that may suppress a finding."""
    now = now or datetime.utcnow()
    active, expired, invalid, errors = [], [], [], []
    for e in exceptions:
        missing = [f for f in REQUIRED_FIELDS if not str(e.get(f) or "").strip()]
        if missing:
            invalid.append(e)
            errors.append(f"{e.get('vulnerability_id', '?')}: missing {','.join(missing)}")
            continue
        exp = _parse_dt(e.get("expires_at"))
        if exp is None:
            invalid.append(e)
            errors.append(f"{e['vulnerability_id']}: expires_at not a date")
            continue
        # leak / private-data guard on free-text fields
        blob = " ".join(str(e.get(f) or "") for f in
                        ("reason", "reachability_assessment", "compensating_control",
                         "tracking_reference", "approved_by")).lower()
        if any(s in blob for s in _LEAK_SUBSTRINGS):
            invalid.append(e)
            errors.append(f"{e['vulnerability_id']}: exception text contains a forbidden token/path")
            continue
        (expired if exp < now else active).append(e)
    return {
        "valid": not invalid and not expired,
        "active": active,
        "expired": expired,
        "invalid": invalid,
        "errors": errors,
    }


def approved_keys(active: list[dict]) -> set[tuple]:
    """(cve, package, installed_version) that active exceptions cover."""
    return {(e["vulnerability_id"], e["package"], e["installed_version"]) for e in active}


def evaluate(exceptions_path: str | Path, critical_keys: set[tuple], *,
             now: datetime | None = None) -> dict:
    """How many CRITICALs are covered by an ACTIVE, VALID exception, and how many
    remain unapproved. Package/version-scoped: a stale exception (not matching a
    current finding) does not count."""
    exceptions = load_exceptions(exceptions_path)
    v = validate_exceptions(exceptions, now=now)
    approved = approved_keys(v["active"])
    covered = critical_keys & approved
    unapproved = critical_keys - approved
    # active exceptions that no longer match any current finding -> stale
    stale = approved - critical_keys
    return {
        "total_exceptions": len(exceptions),
        "active": len(v["active"]),
        "expired": len(v["expired"]),
        "invalid": len(v["invalid"]),
        "errors": v["errors"],
        "critical_total": len(critical_keys),
        "critical_covered": len(covered),
        "critical_unapproved": len(unapproved),
        "stale_exceptions": len(stale),
        "policy_ok": len(v["invalid"]) == 0 and len(v["expired"]) == 0,
    }
