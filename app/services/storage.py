"""Storage path helpers.

The DB stores paths RELATIVE to ``ARCHIVE_ROOT`` (requirement 5.7.2 / added
requirement 3). These helpers convert between relative and absolute, and build
the yt-dlp ``-o`` output templates so files land in a deterministic per-video
directory:

    youtube/videos/<channel_id>/<video_id>/<title>.<ext>     (video profiles)
    youtube/audio/<channel_id>/<video_id>/<title>.<ext>      (audio profiles)
    youtube/metadata_snapshots/<video_id>/<UTC>.<kind>.json  (refresh snapshots)
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.config import Settings

# Phase 9A: report disk figures in GiB (1024**3) to match `df -h` on the host.
_GIB = 1024 ** 3


def disk_usage(settings: Settings, path: Path | str | None = None) -> dict:
    """Free/used/total space on the archive volume (Phase 9A).

    Returns ``readable=False`` (and ``None`` figures) when the path can't be
    stat'd — callers MUST treat "unreadable" as "cannot prove low disk" and not
    hard-block on it. Never returns the host path itself (no path leak).
    """
    target = Path(path) if path is not None else settings.archive_root
    try:
        u = shutil.disk_usage(target)
    except OSError:
        return {
            "readable": False,
            "total_bytes": None, "used_bytes": None, "free_bytes": None,
            "total_gb": None, "used_gb": None, "free_gb": None,
            "used_percent": None,
        }
    return {
        "readable": True,
        "total_bytes": int(u.total), "used_bytes": int(u.used), "free_bytes": int(u.free),
        "total_gb": round(u.total / _GIB, 2),
        "used_gb": round(u.used / _GIB, 2),
        "free_gb": round(u.free / _GIB, 2),
        "used_percent": round(u.used / u.total * 100, 1) if u.total else None,
    }


def to_relative(settings: Settings, absolute: Path | str) -> str:
    """Path relative to ARCHIVE_ROOT, as a forward-slash string for the DB."""
    abs_path = Path(absolute).resolve()
    root = settings.archive_root.resolve()
    try:
        return abs_path.relative_to(root).as_posix()
    except ValueError:
        # Not under the archive root: store the absolute path as a fallback.
        return abs_path.as_posix()


def to_absolute(settings: Settings, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute():
        return rel
    return (settings.archive_root / rel).resolve()


def _media_subdir(media_mode: str) -> str:
    return "audio" if media_mode == "audio" else "videos"


def video_output_dir(settings: Settings, media_mode: str, channel_id: str | None,
                     video_id: str) -> Path:
    """Absolute per-video output directory."""
    cid = (channel_id or "unknown_channel").strip() or "unknown_channel"
    return (
        settings.youtube_root
        / _media_subdir(media_mode)
        / cid
        / video_id
    )


def video_output_template(settings: Settings, media_mode: str,
                          channel_id: str | None, video_id: str) -> str:
    """yt-dlp ``-o`` template for a single video's output directory."""
    out_dir = video_output_dir(settings, media_mode, channel_id, video_id)
    return str(out_dir / "%(title).180B [%(id)s].%(ext)s")


def download_archive_path(settings: Settings, name: str = "history.txt") -> Path:
    return settings.youtube_root / "archive" / name


def snapshot_dir(settings: Settings, video_id: str) -> Path:
    return settings.youtube_root / "metadata_snapshots" / video_id


def job_log_dir(settings: Settings, job_id: int) -> Path:
    """Absolute directory for a job's stdout/stderr/command logs (under LOG_ROOT)."""
    return settings.log_root / "jobs" / str(job_id)


def log_relative(settings: Settings, absolute: Path | str) -> str:
    """Path relative to LOG_ROOT for storage in ``jobs.log_path``."""
    abs_path = Path(absolute).resolve()
    root = settings.log_root.resolve()
    try:
        return abs_path.relative_to(root).as_posix()
    except ValueError:
        return abs_path.as_posix()
