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

from pathlib import Path

from app.config import Settings


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
