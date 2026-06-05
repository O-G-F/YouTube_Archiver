"""yt-dlp wrapper.

Design (requirement 4.2): downloads run via **subprocess** so the exact CLI is
recorded and re-runnable, and the old conf assets map over cleanly. Lightweight
**metadata extraction / URL probing** uses the yt-dlp Python API.

Every run records three artifacts in the job log directory:
  - ``command.txt``  – the exact command (password-family values masked)
  - ``yt-dlp.stdout.log``
  - ``yt-dlp.stderr.log``
Cookie/token *values* are never written to logs (requirement 12). The cookies
file *path* is kept so the command stays re-runnable.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

# Values following these flags are masked in recorded commands / logs.
_SENSITIVE_VALUE_FLAGS = {
    "--password",
    "--ap-password",
    "--video-password",
    "--client-secret",
}


def redact_args(args: list[str], *, mask_cookies: bool = False) -> list[str]:
    """Mask secret *values* for safe logging.

    By default the ``--cookies`` *path* is kept (command.txt must stay
    re-runnable). For user-facing dry-run output, pass ``mask_cookies=True`` to
    also mask the cookies path (requirement: never surface cookie/secret paths).
    """
    sensitive = set(_SENSITIVE_VALUE_FLAGS)
    if mask_cookies:
        sensitive |= {"--cookies"}
    out: list[str] = []
    mask_next = False
    for arg in args:
        if mask_next:
            out.append("******")
            mask_next = False
            continue
        # Inline secrets carried inside a combined arg (e.g. an extractor-arg
        # "youtube:po_token=XXXX"): mask the value, keep the key.
        if "po_token=" in arg:
            out.append(re.sub(r"(po_token=)[^,;\s]+", r"\1******", arg))
            continue
        out.append(arg)
        if arg in sensitive:
            mask_next = True
    return out


@dataclass
class CompletedRun:
    returncode: int
    command: list[str]
    command_display: str
    stdout_path: Path
    stderr_path: Path
    command_path: Path

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def build_command(
    settings: Settings,
    argv: list[str],
    *,
    url: str | None = None,
    batch_file: str | None = None,
) -> list[str]:
    """Assemble the full command list: binary + args + (url | -a batch_file)."""
    cmd = [settings.ytdlp_binary, *argv]
    if batch_file:
        cmd += ["-a", batch_file]
    if url:
        cmd.append(url)
    return cmd


def run_ytdlp(
    argv: list[str],
    log_dir: Path,
    *,
    url: str | None = None,
    batch_file: str | None = None,
    settings: Settings | None = None,
    timeout: int | None = None,
) -> CompletedRun:
    """Run yt-dlp as a subprocess, capturing stdout/stderr to separate files."""
    settings = settings or get_settings()
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_command(settings, argv, url=url, batch_file=batch_file)

    display = shlex.join(redact_args(cmd))
    command_path = log_dir / "command.txt"
    command_path.write_text(display + "\n", encoding="utf-8")

    stdout_path = log_dir / "yt-dlp.stdout.log"
    stderr_path = log_dir / "yt-dlp.stderr.log"

    logger.info("yt-dlp run: %s", display)
    with stdout_path.open("w", encoding="utf-8") as out_f, stderr_path.open(
        "w", encoding="utf-8"
    ) as err_f:
        try:
            proc = subprocess.run(
                cmd,
                stdout=out_f,
                stderr=err_f,
                text=True,
                timeout=timeout,
                check=False,
            )
            returncode = proc.returncode
        except FileNotFoundError as exc:
            err_f.write(f"\n[archiver] yt-dlp binary not found: {exc}\n")
            returncode = 127
        except subprocess.TimeoutExpired as exc:
            err_f.write(f"\n[archiver] yt-dlp timed out after {exc.timeout}s\n")
            returncode = 124

    return CompletedRun(
        returncode=returncode,
        command=cmd,
        command_display=display,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        command_path=command_path,
    )


def extract_info(
    url: str,
    *,
    flat: bool = False,
    settings: Settings | None = None,
) -> dict:
    """Extract metadata via the yt-dlp Python API (no download).

    ``flat=True`` returns a shallow playlist listing (used for playlist
    expansion) without resolving every entry.
    """
    import yt_dlp  # imported lazily so the module loads without the package

    settings = settings or get_settings()
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noprogress": True,
        "ignoreerrors": True,
        "extract_flat": "in_playlist" if flat else False,
    }
    if settings.cookies_file and Path(settings.cookies_file).is_file():
        opts["cookiefile"] = settings.cookies_file

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info is None:
            raise RuntimeError(f"yt-dlp could not extract info for {url!r}")
        return ydl.sanitize_info(info)


def ytdlp_version(settings: Settings | None = None) -> str | None:
    """Return the yt-dlp version string, or ``None`` if it cannot be run."""
    settings = settings or get_settings()
    try:
        proc = subprocess.run(
            [settings.ytdlp_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None
