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

from datetime import datetime, timedelta

from app.config import Settings
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
    "comments_failed": ("unable to download comments", "error downloading comments"),
    "live_chat_failed": ("unable to download live chat", "live chat replay is not available"),
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
    "subtitles_failed": "Subtitle download failed (non-fatal; body/metadata may be fine — retry subtitles only)",
    "comments_failed": "Comment download failed — retry later",
    "live_chat_failed": "Live chat download failed / not available",
    "impersonation": "optional impersonation/runtime dependency missing (low severity)",
    "auth_required": "YouTube Data API not configured / not authorized — set up OAuth (see README)",
    "quota_exceeded": "YouTube Data API quota exceeded — try again later",
    "forbidden": "YouTube Data API request forbidden (permissions / API not enabled)",
    "token_expired": "OAuth token expired — re-run the authorize flow",
}

_LOW_SEVERITY = ("impersonation", "subtitles_failed")

# Categories that are worth automatically retrying (transient / fetchable later).
# auth_required / forbidden / impersonation are NOT here (need setup, or harmless).
RETRYABLE_REASONS = frozenset(
    {
        "rate_limited",
        "incomplete_data",
        "fragments_failed",
        "subtitles_failed",
        "comments_failed",
        "live_chat_failed",
        "quota_exceeded",
        "token_expired",
    }
)


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
    # Retryable = a recognised transient reason, OR a partial success (we can
    # re-fetch the missing piece), OR an explicit worker flag. A plain `failed`
    # with no recognised retryable reason is NOT retryable (e.g. video deleted).
    retryable = (
        bool(meta.get("retryable"))
        or partial
        or any(r in RETRYABLE_REASONS for r in reasons)
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


def is_retryable(classification: dict) -> bool:
    return bool(classification.get("retryable"))


def subtitles_only_failure(classification: dict) -> bool:
    """True if subtitles failed but nothing else did (good candidate for a
    subtitles-only refresh on a partial_success body/metadata job)."""
    reasons = set(classification.get("reasons") or [])
    hard = reasons - {"subtitles_failed", "impersonation"}
    return "subtitles_failed" in reasons and not hard


def compute_next_retry_at(
    reasons: list[str],
    retry_count: int,
    settings: Settings,
    now: datetime,
    *,
    seed: int = 0,
) -> datetime | None:
    """Backoff time for the next auto-retry, or None if not retryable / over cap.

    delay = backoff * multiplier**retry_count (+ deterministic jitter from seed).
    ``seed`` (e.g. the job id) spreads retries without using a RNG.
    """
    if retry_count >= max(0, settings.download_retry_max_attempts):
        return None
    if not any(r in RETRYABLE_REASONS for r in reasons):
        return None
    base = max(0, settings.download_retry_backoff_seconds)
    mult = max(1.0, settings.download_retry_backoff_multiplier) ** max(0, retry_count)
    delay = base * mult
    jitter = max(0, settings.download_retry_jitter_seconds)
    if jitter > 0:
        delay += seed % (jitter + 1)
    return now + timedelta(seconds=int(delay))
