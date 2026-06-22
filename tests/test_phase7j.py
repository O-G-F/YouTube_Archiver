"""Phase 7J fix: yt-dlp rewrites the cookie jar back to the --cookies path on
exit. When COOKIES_FILE is on a read-only mount (Docker :ro secret) this fails
with [Errno 30] Read-only file system. The runner now hands yt-dlp a WRITABLE
temp copy and deletes it afterwards; the read-only original is never written,
and the cookie path is never logged.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from app.config import get_settings
from app.services import ytdlp
from app.services.ytdlp import (
    _cookies_value,
    _swap_cookies_value,
    redact_args,
    run_ytdlp,
    writable_cookie_copy,
)


def _readonly_cookie(tmp_path: Path) -> Path:
    p = tmp_path / "cookies.txt"
    p.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tval\n")
    p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 0444 read-only
    return p


# --------------------------------------------------------------------------- #
# writable_cookie_copy
# --------------------------------------------------------------------------- #
def test_writable_copy_of_readonly_cookie(tmp_path):
    ro = _readonly_cookie(tmp_path)
    original = ro.read_text()
    with writable_cookie_copy(str(ro)) as tmp:
        assert tmp != str(ro)                       # a different (temp) path
        assert Path(tmp).is_file()
        assert Path(tmp).read_text() == original    # content copied
        # the copy is writable — simulate yt-dlp's cookie-jar write-back
        with open(tmp, "a") as f:
            f.write("# refreshed\n")                # must NOT raise Errno 30
        tmp_path_str = tmp
    assert not Path(tmp_path_str).exists()          # cleaned up after the context
    # original read-only file untouched
    assert ro.read_text() == original
    assert not (os.access(ro, os.W_OK))             # still read-only


def test_writable_copy_passthrough_for_none_and_missing(tmp_path):
    with writable_cookie_copy(None) as v:
        assert v is None
    missing = str(tmp_path / "nope.txt")
    with writable_cookie_copy(missing) as v:
        assert v == missing  # unchanged when not a file


def test_cookies_value_and_swap():
    cmd = ["yt-dlp", "--cookies", "/secrets/cookies.txt", "--no-warnings", "URL"]
    assert _cookies_value(cmd) == "/secrets/cookies.txt"
    swapped = _swap_cookies_value(cmd, "/tmp/x.txt")
    assert swapped[2] == "/tmp/x.txt"
    assert cmd[2] == "/secrets/cookies.txt"  # original not mutated
    assert _cookies_value(["yt-dlp", "URL"]) is None


# --------------------------------------------------------------------------- #
# run_ytdlp end-to-end with a fake binary that writes the cookie jar back
# --------------------------------------------------------------------------- #
def _fake_ytdlp_writing_cookies(tmp_path: Path) -> Path:
    """A stand-in yt-dlp that appends to whatever --cookies path it's given
    (like the real cookie-jar write-back). Fails if that path is read-only."""
    script = tmp_path / "fake-ytdlp.sh"
    script.write_text(
        "#!/bin/sh\n"
        'f=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "--cookies" ]; then f="$2"; fi\n'
        "  shift\n"
        "done\n"
        'if [ -n "$f" ]; then echo "session-refresh" >> "$f" || exit 30; fi\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return script


def test_run_ytdlp_readonly_cookies_no_errno30(settings, tmp_path, monkeypatch):
    ro = _readonly_cookie(tmp_path)
    before = ro.read_text()
    monkeypatch.setattr(settings, "ytdlp_binary", str(_fake_ytdlp_writing_cookies(tmp_path)))

    run = run_ytdlp(
        ["--cookies", str(ro), "--no-warnings"],
        tmp_path / "logs",
        url="https://www.youtube.com/watch?v=abc",
        settings=settings,
    )
    # The write-back hit the WRITABLE temp copy, not the read-only original.
    assert run.returncode == 0
    assert ro.read_text() == before               # original read-only file untouched
    stderr = (tmp_path / "logs" / "yt-dlp.stderr.log").read_text()
    assert "Read-only" not in stderr and "Errno 30" not in stderr


def test_run_ytdlp_masks_cookie_path_in_command_txt(settings, tmp_path, monkeypatch):
    ro = _readonly_cookie(tmp_path)
    monkeypatch.setattr(settings, "ytdlp_binary", str(_fake_ytdlp_writing_cookies(tmp_path)))
    run = run_ytdlp(["--cookies", str(ro)], tmp_path / "logs2", url="https://x", settings=settings)
    cmd_txt = run.command_path.read_text()
    assert str(ro) not in cmd_txt                  # cookie path not leaked to command.txt
    assert "--cookies" in cmd_txt and "******" in cmd_txt
    # the recorded command object keeps the original path (never the temp copy)
    assert "/ytdlp-cookies-" not in " ".join(run.command)


def test_redact_args_unchanged_default_keeps_path():
    # redact_args itself is unchanged: default keeps the path, mask_cookies hides it
    args = ["--cookies", "/secrets/c.txt", "--password", "p"]
    assert "/secrets/c.txt" in redact_args(args)            # default
    assert "/secrets/c.txt" not in redact_args(args, mask_cookies=True)
    assert "p" not in redact_args(args)                     # value-secret always masked
