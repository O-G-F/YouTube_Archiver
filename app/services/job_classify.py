"""Derive human-friendly hints about a job's outcome (Phase 5B/6A job UI).

Pure functions over (status, error text, meta) — no disk I/O — so they are cheap
enough to run for every row in a job listing, and reusable by the worker to
persist a ``classification`` into ``job.meta`` for download jobs.

Recognised failure categories (download-stabilization visibility):
  - rate_limited       : HTTP 429 / Too Many Requests
  - incomplete_data    : "Incomplete data received" (YouTube throttling)
  - fragments_failed   : fragment / video-data download failures
  - subtitles_failed   : subtitle download failures (often non-fatal)
  - impersonation      : optional impersonation/runtime dependency missing (low severity)
"""

from __future__ import annotations

from app.models import Job

# category -> (substring markers searched in the lowercased error text)
_MARKERS: dict[str, tuple[str, ...]] = {
    "rate_limited": ("http error 429", "too many requests", "429:"),
    "incomplete_data": ("incomplete data received",),
    "fragments_failed": (
        "unable to download video data",
        "fragment",
        "giving up after",
        "unable to download format",
    ),
    "subtitles_failed": (
        "unable to download subtitles",
        "unable to download video subtitles",
        "error downloading subtitle",
        "failed to download subtitle",
    ),
    "impersonation": ("impersonate", "curl_cffi", "could not find a suitable"),
    # YouTube Data API (OAuth) error categories (Phase 6B).
    "auth_required": ("auth_required", "oauth token not found", "not configured", "unauthorized"),
    "quota_exceeded": ("quota_exceeded", "quotaexceeded", "quota exceeded", "dailylimitexceeded"),
    "forbidden": ("forbidden", "accessnotconfigured", "insufficientpermissions"),
    "token_expired": ("token_expired", "invalid_grant", "token expired", "token has been expired"),
}

# human-readable note per category (low-severity ones flagged as such)
_NOTES: dict[str, str] = {
    "rate_limited": "YouTube rate limit (HTTP 429) — usually transient, retry later",
    "incomplete_data": "Incomplete data received — YouTube throttling; retry later "
    "(cookies / PO-token / impersonation may help — see README)",
    "fragments_failed": "Some media fragments failed to download — retry later",
    "subtitles_failed": "Subtitle download failed (usually non-fatal; body/metadata may be fine)",
    "impersonation": "optional impersonation/runtime dependency missing (low severity)",
    "auth_required": "YouTube Data API not configured / not authorized — set up OAuth (see README)",
    "quota_exceeded": "YouTube Data API quota exceeded — try again later",
    "forbidden": "YouTube Data API request forbidden (permissions / API not enabled)",
    "token_expired": "OAuth token expired — re-run the authorize flow",
}

_LOW_SEVERITY = ("impersonation", "subtitles_failed")


def classify_text(status: str, error_text: str | None, meta: dict | None) -> dict:
    """Classify an outcome from its status + stderr/error text + meta flags."""
    meta = meta or {}
    low = (error_text or "").lower()

    reasons: list[str] = []
    for cat, markers in _MARKERS.items():
        if any(m in low for m in markers):
            reasons.append(cat)
    # meta may carry an authoritative rate_limited flag set by the worker.
    if meta.get("rate_limited") and "rate_limited" not in reasons:
        reasons.insert(0, "rate_limited")

    rate_limited = "rate_limited" in reasons
    partial = status == "partial_success"
    retryable = bool(meta.get("retryable")) or status in (
        "failed",
        "canceled",
        "partial_success",
    )

    warnings = [_NOTES[c] for c in reasons if c in _NOTES]

    if rate_limited:
        summary = "Rate limited (HTTP 429)"
    elif "incomplete_data" in reasons:
        summary = "Incomplete data (YouTube throttling)"
    elif "fragments_failed" in reasons:
        summary = "Fragment download failed"
    elif partial:
        summary = "Partial success — usable output despite a non-zero exit"
    elif status == "failed":
        summary = "Failed"
    elif status == "success":
        summary = "Success"
    else:
        summary = None

    return {
        "rate_limited": rate_limited,
        "partial": partial,
        "retryable": retryable,
        "reasons": reasons,
        "warnings": warnings,
        "summary": summary,
    }


def classify_job(job: Job) -> dict:
    """Classification for a Job row (uses its persisted error_message + meta)."""
    return classify_text(job.status, job.error_message, job.meta)


def is_low_severity(reason: str) -> bool:
    return reason in _LOW_SEVERITY
