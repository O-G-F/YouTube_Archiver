"""Derive human-friendly hints about a job's outcome (Phase 5B job UI).

Pure function over the Job row (status / meta / error_message) — no disk I/O —
so it is cheap enough to run for every row in a job listing.
"""

from __future__ import annotations

from app.models import Job

_RATE_LIMIT_MARKERS = ("HTTP Error 429", "Too Many Requests", "http error 429")
_IMPERSONATE_MARKERS = ("impersonate", "impersonation", "curl_cffi", "could not find a suitable")


def classify_job(job: Job) -> dict:
    """Return UI hints: rate_limited / partial / retryable / warnings / summary."""
    meta = job.meta or {}
    err = job.error_message or ""
    low = err.lower()

    rate_limited = bool(meta.get("rate_limited")) or any(m.lower() in low for m in _RATE_LIMIT_MARKERS)
    partial = job.status == "partial_success"
    retryable = bool(meta.get("retryable")) or job.status in ("failed", "canceled", "partial_success")

    warnings: list[str] = []
    if any(m in low for m in _IMPERSONATE_MARKERS):
        warnings.append("optional impersonation/runtime dependency missing (low severity)")
    if rate_limited:
        warnings.append(
            "YouTube rate limit (HTTP 429) — usually a transient subtitle/throttle limit, retry later"
        )

    if rate_limited:
        summary = "Rate limited (HTTP 429)"
    elif partial:
        summary = "Partial success — usable output despite a non-zero exit"
    elif job.status == "failed":
        summary = "Failed"
    elif job.status == "success":
        summary = "Success"
    else:
        summary = None

    return {
        "rate_limited": rate_limited,
        "partial": partial,
        "retryable": retryable,
        "warnings": warnings,
        "summary": summary,
    }
