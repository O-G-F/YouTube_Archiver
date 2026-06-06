"""Safe access to per-job log files.

Only the three known log files inside ``LOG_ROOT/jobs/<id>/`` may be read, and
every resolved path is verified to live under ``LOG_ROOT`` (path-traversal
guard), so a tampered ``jobs.log_path`` can never escape the log root.

All log text is passed through :func:`mask_secrets` before it leaves this
module, so cookie paths / passwords / tokens never reach the API or UI even if
they somehow appear in a log line (defense in depth; the command line is also
redacted at write time by ``services.ytdlp``).
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings
from app.models import Job

# Logical stream name -> on-disk filename.
LOG_FILES: dict[str, str] = {
    "command": "command.txt",
    "stdout": "yt-dlp.stdout.log",
    "stderr": "yt-dlp.stderr.log",
}

MASK = "***REDACTED***"

# yt-dlp / generic flags whose VALUE is a secret (cookie path, password, token).
_SECRET_FLAGS = (
    "--cookies",
    "--cookies-from-browser",
    "--password",
    "--ap-password",
    "--video-password",
    "--username",
    "--ap-username",
    "--client-secret",
    "--token",
    "--api-key",
)
# "<flag> <value>" or "<flag>=<value>" (value may be single/double quoted).
_FLAG_VALUE_RE = re.compile(
    r"(" + "|".join(re.escape(f) for f in _SECRET_FLAGS) + r")(\s+|=)('[^']*'|\"[^\"]*\"|\S+)"
)
# "Authorization: <scheme token>" (mask the whole credential to end of line) /
# "password=<v>" / "token: <v>" / "api_key=<v>" ...
_TOKEN_RES = (
    re.compile(r"(Authorization:\s*)(.+)", re.IGNORECASE),
    re.compile(
        r"((?:password|passwd|secret|token|api[_-]?key|access[_-]?token|refresh[_-]?token)"
        r"\s*[=:]\s*)(\S+)",
        re.IGNORECASE,
    ),
    # yt-dlp PO token / visitor data in extractor-args: mask the values.
    re.compile(r"(po_token=)([^,;\s]+)", re.IGNORECASE),
    re.compile(r"(visitor_data=)([^,;\s]+)", re.IGNORECASE),
)


def mask_secrets(text: str | None, settings: Settings | None = None) -> str | None:
    """Redact cookie paths / passwords / tokens from arbitrary log text.

    Targeted (known secret-bearing flags + token patterns + the configured
    cookies file path) so ordinary log content is never mangled.
    """
    if not text:
        return text
    text = _FLAG_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    for pat in _TOKEN_RES:
        text = pat.sub(lambda m: f"{m.group(1)}{MASK}", text)
    if settings is not None:
        cookies = (getattr(settings, "cookies_file", "") or "").strip()
        if cookies:
            text = text.replace(cookies, MASK)
    return text


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def job_log_dir(settings: Settings, job: Job) -> Path | None:
    """Resolve the job's log directory, or None if absent / outside LOG_ROOT."""
    if not job.log_path:
        return None
    base = (settings.log_root / job.log_path).resolve()
    if not _is_within(base, settings.log_root):
        return None
    return base


def log_file_path(settings: Settings, job: Job, stream: str) -> Path | None:
    """Resolve a specific log file, with a hard path-traversal guard."""
    if stream not in LOG_FILES:
        return None
    base = job_log_dir(settings, job)
    if base is None:
        return None
    path = (base / LOG_FILES[stream]).resolve()
    if not _is_within(path, settings.log_root) or not path.is_file():
        return None
    return path


def read_log(
    settings: Settings, job: Job, stream: str, *, tail: int | None = None
) -> str | None:
    """Read a job log stream (optionally only the last ``tail`` lines)."""
    path = log_file_path(settings, job, stream)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if tail and tail > 0:
        text = "\n".join(text.splitlines()[-tail:])
    return mask_secrets(text, settings)


def relative_log_paths(settings: Settings, job: Job) -> dict[str, str | None]:
    """Relative-to-LOG_ROOT paths for each stream (for job detail views)."""
    if not job.log_path:
        return {k: None for k in LOG_FILES}
    return {
        stream: f"{job.log_path}/{fname}" for stream, fname in LOG_FILES.items()
    }
