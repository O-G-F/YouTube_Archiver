"""YouTube fetch-stabilization diagnostics (Phase 7B).

Two layers:
  - ``static_checks``: environment status (yt-dlp / deno / remote-components /
    curl_cffi / cookies / PO-token), NO network, NO secrets/paths exposed.
  - ``run_diagnostics``: actually runs yt-dlp (metadata / subtitles / optional
    small video) into a THROWAWAY temp dir so it measures real success/429/
    incomplete-data WITHOUT ever persisting a media body.

A diagnostic is a *measurement tool*, not a guarantee that 429 / Incomplete data
received will disappear.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from app.config import Settings
from app.logging_setup import get_logger
from app.services import storage
from app.services.command_builder import external_ctx
from app.services.job_classify import classify_text
from app.services.profiles import BuildContext, build_ytdlp_args, get_profile_spec
from app.services.ytdlp import run_ytdlp, ytdlp_version

logger = get_logger(__name__)

_VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".m4v", ".mov"}
_AUDIO_EXTS = {".m4a", ".opus", ".mp3", ".flac", ".ogg", ".oga", ".wav", ".aac"}

# steps that must NEVER produce a media body (used for assertions/UI labels)
NO_BODY_STEPS = ("metadata_only", "subtitles")


# --------------------------------------------------------------------------- #
# Capability probes
# --------------------------------------------------------------------------- #
def _curl_cffi_status() -> dict:
    try:
        import curl_cffi  # noqa: F401

        version = getattr(curl_cffi, "__version__", "unknown")
        return {"installed": True, "version": version}
    except Exception:  # noqa: BLE001
        return {"installed": False, "version": None}


def _impersonate_targets() -> int:
    """Best-effort count of yt-dlp impersonation targets (needs curl_cffi)."""
    try:
        from yt_dlp import YoutubeDL

        with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            targets = ydl._get_available_impersonate_targets()  # type: ignore[attr-defined]
        return len(targets or [])
    except Exception:  # noqa: BLE001
        return 0


def _deno_status(settings: Settings) -> dict:
    path = (settings.deno_path or "").strip()
    available = False
    if path and Path(path).is_file():
        available = True
    elif shutil.which("deno"):
        available = True
    return {"available": available}


# --------------------------------------------------------------------------- #
# Static checks (no network, no secrets)
# --------------------------------------------------------------------------- #
def static_checks(settings: Settings) -> dict:
    curl = _curl_cffi_status()
    targets = _impersonate_targets() if curl["installed"] else 0
    deno = _deno_status(settings)
    cookies = settings.cookies_file_status()
    rc = settings.effective_remote_components

    checks: list[dict] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    add("yt-dlp", "ok" if ytdlp_version() else "failed", f"version {ytdlp_version() or 'unknown'}")
    add("deno", "ok" if deno["available"] else "warning",
        "available" if deno["available"] else "not found (JS challenge solver needs deno)")
    add("remote_components", "ok" if rc else "warning", str(rc) if rc else "disabled")
    add("curl_cffi (impersonation)", "ok" if curl["installed"] else "warning",
        f"installed (targets: {targets})" if curl["installed"] else "not installed (optional)")
    add("cookies", "ok" if cookies["configured"] else "warning",
        _cookies_detail(cookies, settings))
    add("PO token", "ok" if settings.po_token_configured else "warning",
        "configured" if settings.po_token_configured else "not set (optional)")
    add("visitor data", "ok" if settings.visitor_data_configured else "warning",
        "configured" if settings.visitor_data_configured else "not set (optional)")

    ok = all(c["status"] != "failed" for c in checks)
    return {
        "ok": ok,
        "ytdlp_version": ytdlp_version(),
        "deno_available": deno["available"],
        "remote_components": rc,
        "curl_cffi_installed": curl["installed"],
        "curl_cffi_version": curl["version"],
        "impersonate_targets": targets,
        "impersonation_available": curl["installed"] and targets > 0,
        "cookies": cookies,  # configured/file_exists/readable/last_modified (NO path)
        "browser_cookies_configured": settings.browser_cookies_configured,
        "po_token_configured": settings.po_token_configured,
        "visitor_data_configured": settings.visitor_data_configured,
        "checks": checks,
        "recommendations": _static_recommendations(settings, curl, deno, targets),
    }


def _cookies_detail(cookies: dict, settings: Settings) -> str:
    # path is set but the file is missing/unreadable -> actionable warning
    if cookies["file_configured"] and not cookies["file_exists"]:
        return "cookies file path set but NOT found on disk (check the mount/path)"
    if cookies["file_configured"] and cookies["file_exists"] and not cookies["readable"]:
        return "cookies file found but NOT readable (fix permissions)"
    if cookies["file_exists"] and cookies["readable"]:
        return "cookies file present & readable"
    if settings.browser_cookies_configured:
        return "browser cookies configured"
    return "not configured (no cookies.txt / browser cookies)"


def _static_recommendations(settings, curl, deno, targets) -> list[str]:
    recs: list[str] = []
    if not deno["available"]:
        recs.append("Install deno (JS challenge solver) — required with --remote-components.")
    if not curl["installed"]:
        recs.append("Install curl_cffi to enable yt-dlp impersonation (fewer 403/429).")
    elif targets == 0:
        recs.append("curl_cffi is installed but yt-dlp reports no impersonate targets.")
    st = settings.cookies_file_status()
    if st["file_configured"] and not st["file_exists"]:
        recs.append("COOKIES_FILE is set but the file is missing — check the mount/path (e.g. /config/cookies.txt).")
    elif st["file_exists"] and not st["readable"]:
        recs.append("Cookies file is not readable — fix permissions.")
    elif not settings.cookies_configured:
        recs.append("Configure COOKIES_FILE (e.g. /config/cookies.txt) or COOKIES_FROM_BROWSER to reduce 429.")
    if not settings.po_token_configured:
        recs.append("Optionally set YOUTUBE_PO_TOKEN (+ YOUTUBE_VISITOR_DATA) to help with throttling.")
    if not recs:
        recs.append("Configuration looks healthy. 429 / Incomplete data may still occur intermittently.")
    return recs


# --------------------------------------------------------------------------- #
# Live diagnostic steps (run yt-dlp into a throwaway dir; never persist a body)
# --------------------------------------------------------------------------- #
def _has_body(out_dir: Path) -> bool:
    for p in out_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in (_VIDEO_EXTS | _AUDIO_EXTS):
            return True
    return False


def _run_step(
    settings: Settings,
    name: str,
    profile_name: str,
    url: str,
    *,
    timeout: int | None,
    log_base: Path | None,
) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="ytdiag_"))
    try:
        from app.db import session_scope

        with session_scope() as s:
            spec = get_profile_spec(s, profile_name)
        out_tpl = str(tmp / "%(id)s.%(ext)s")
        ctx = BuildContext(
            output_template=out_tpl,
            download_archive=None,
            no_playlist=True,
            default_sub_langs=settings.effective_subtitles_sub_langs,
            archive_sub_langs=settings.effective_subtitles_sub_langs,
            retry_sleep=settings.ytdlp_retry_backoff_seconds,
            **external_ctx(settings),
        )
        argv = build_ytdlp_args(spec, ctx)
        # Keep the step's logs under the job's log dir when running as a job;
        # otherwise inside the temp dir (cleaned up).
        log_dir = (log_base / name) if log_base is not None else (tmp / "logs")
        t0 = time.monotonic()
        run = run_ytdlp(argv, log_dir, url=url, settings=settings, timeout=timeout)
        duration = round(time.monotonic() - t0, 2)
        err_tail = _read_tail(run.stderr_path)
        produced_files = any(p.is_file() for p in tmp.rglob("*") if "logs" not in p.parts)
        media_body = _has_body(tmp)
        if run.ok:
            status = "success"
        elif produced_files:
            status = "partial"
        else:
            status = "failed"
        classification = classify_text(
            "success" if run.ok else ("partial_success" if produced_files else "failed"),
            err_tail,
            {},
        )
        log_rel = storage.log_relative(settings, log_dir) if log_base is not None else None
        return {
            "name": name,
            "profile": profile_name,
            "status": status,
            "duration_seconds": duration,
            "media_body_created": media_body,
            "classification": classification,
            "log_path": log_rel,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read_tail(path: Path, max_chars: int = 4000) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def run_diagnostics(
    settings: Settings,
    url: str,
    *,
    profile: str | None = None,
    include_video_download: bool = False,
    timeout: int | None = None,
    log_base: Path | None = None,
) -> dict:
    """Run metadata + subtitles (+ optional small video) tests against ``url``.

    The video test downloads into a temp dir that is deleted, so the DB never
    gains a media body. Returns per-step results + overall status +
    recommendations.
    """
    meta_timeout = timeout or 90
    video_profile = profile or "video_compressed_1080p"
    steps: list[dict] = [
        _run_step(settings, "metadata_only", "metadata_only", url, timeout=meta_timeout, log_base=log_base),
        _run_step(settings, "subtitles", "subtitles_refresh_only", url, timeout=meta_timeout, log_base=log_base),
    ]
    if include_video_download:
        steps.append(
            _run_step(settings, "video_download", video_profile, url, timeout=timeout or 180, log_base=log_base)
        )

    statuses = {s["status"] for s in steps}
    if statuses == {"success"}:
        overall = "success"
    elif "success" in statuses or "partial" in statuses:
        overall = "partial"
    else:
        overall = "failed"

    all_reasons = sorted({r for s in steps for r in s["classification"]["reasons"]})
    return {
        "url": url,
        "overall": overall,
        "steps": steps,
        "reasons": all_reasons,
        "recommendations": _dynamic_recommendations(settings, all_reasons, steps),
    }


def _dynamic_recommendations(settings, reasons: list[str], steps: list[dict]) -> list[str]:
    recs: list[str] = []
    if "rate_limited" in reasons:
        recs.append("HTTP 429 seen: wait and retry from the retryable list; configure cookies / PO-token; lower concurrency.")
    if "incomplete_data" in reasons:
        recs.append("Incomplete data received (throttling): cookies / PO-token / impersonation may help; retry later.")
    if "impersonation" in reasons:
        recs.append("Impersonation warning: install/enable curl_cffi for fewer 403/429.")
    if "fragments_failed" in reasons:
        recs.append("Fragment failures: retry later; a small video may still partially download.")
    if any(s["name"] == "subtitles" and s["status"] != "success" for s in steps):
        recs.append("Subtitles flaky: use `subtitles refresh` to re-fetch only subtitles (no body re-download).")
    if any(s["name"] == "video_download" and s["status"] != "success" for s in steps):
        recs.append(
            "Video download did not fully succeed: download in small batches with delay/backoff "
            "(DOWNLOAD_JOB_DELAY_SECONDS + DOWNLOAD_RETRY_*), then retry later from the retryable list."
        )
    if not reasons and all(s["status"] == "success" for s in steps):
        recs.append("Fetching is healthy right now. Note: 429 / Incomplete data can still recur intermittently.")
    elif not recs:
        recs.append("Some steps did not fully succeed — see per-step classification.")
    return recs
