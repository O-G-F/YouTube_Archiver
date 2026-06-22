"""yt-dlp wrapper.

Design (requirement 4.2): downloads run via **subprocess** so the exact CLI is
recorded and re-runnable, and the old conf assets map over cleanly. Lightweight
**metadata extraction / URL probing** uses the yt-dlp Python API.

Every run records three artifacts in the job log directory:
  - ``command.txt``  – the exact command (password-family values masked)
  - ``yt-dlp.stdout.log``
  - ``yt-dlp.stderr.log``
Cookie/token *values* are never written to logs (requirement 12). The cookies
file *path* is also masked in ``command.txt`` (Phase 7I+). At run time yt-dlp is
given a writable COPY of the cookies file (it rewrites the jar on exit), so a
read-only cookies mount never triggers ``[Errno 30] Read-only file system``.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)


@contextmanager
def writable_cookie_copy(src: str | None):
    """Yield a WRITABLE copy of a (possibly read-only) cookie file path.

    yt-dlp rewrites the cookie jar back to the ``--cookies`` / ``cookiefile``
    path when the session ends. If that path is on a read-only mount (e.g. a
    Docker ``:ro`` secret), the write fails with ``[Errno 30] Read-only file
    system`` and the run errors. So we hand yt-dlp a private writable copy in
    the system temp dir (0600), then delete it afterwards. The original
    read-only file is never modified, and the temp path is never logged.

    Yields ``src`` unchanged when there's nothing to copy (no path / not a file).
    """
    if not src:
        yield src
        return
    try:
        if not Path(src).is_file():
            yield src
            return
    except OSError:
        yield src
        return
    fd, tmp = tempfile.mkstemp(prefix="ytdlp-cookies-", suffix=".txt")
    try:
        os.close(fd)
        shutil.copyfile(src, tmp)
        try:
            os.chmod(tmp, 0o600)  # cookies are secret
        except OSError:
            pass
        yield tmp
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _cookies_value(cmd: list[str]) -> str | None:
    for i, a in enumerate(cmd):
        if a == "--cookies" and i + 1 < len(cmd):
            return cmd[i + 1]
    return None


def _swap_cookies_value(cmd: list[str], new_path: str) -> list[str]:
    out = list(cmd)
    for i, a in enumerate(out):
        if a == "--cookies" and i + 1 < len(out):
            out[i + 1] = new_path
            break
    return out

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
        # "youtube:po_token=XXXX;visitor_data=YYYY"): mask values, keep keys.
        if "po_token=" in arg or "visitor_data=" in arg:
            masked = re.sub(r"(po_token=)[^,;\s]+", r"\1******", arg)
            masked = re.sub(r"(visitor_data=)[^,;\s]+", r"\1******", masked)
            out.append(masked)
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

    # Mask the cookies path too: the recorded command must not leak a secret/temp
    # cookie path (only the value-family secrets were masked before).
    display = shlex.join(redact_args(cmd, mask_cookies=True))
    command_path = log_dir / "command.txt"
    command_path.write_text(display + "\n", encoding="utf-8")

    stdout_path = log_dir / "yt-dlp.stdout.log"
    stderr_path = log_dir / "yt-dlp.stderr.log"

    logger.info("yt-dlp run: %s", display)
    with stdout_path.open("w", encoding="utf-8") as out_f, stderr_path.open(
        "w", encoding="utf-8"
    ) as err_f, writable_cookie_copy(_cookies_value(cmd)) as run_cookies:
        # yt-dlp writes the cookie jar back to --cookies; use a writable copy so a
        # read-only cookies mount does not cause [Errno 30] Read-only file system.
        run_cmd = (
            _swap_cookies_value(cmd, run_cookies)
            if run_cookies and run_cookies != _cookies_value(cmd)
            else cmd
        )
        try:
            proc = subprocess.run(
                run_cmd,
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
        command=cmd,  # original (read-only) path; never the temp copy
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
    src_cookies = (
        settings.cookies_file
        if settings.cookies_file and Path(settings.cookies_file).is_file()
        else None
    )
    # yt-dlp's Python API also rewrites the cookie jar on close -> use a writable
    # copy so a read-only cookies mount does not raise [Errno 30].
    with writable_cookie_copy(src_cookies) as cf:
        if cf:
            opts["cookiefile"] = cf
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
