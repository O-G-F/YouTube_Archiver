"""Environment diagnostics (``archiver doctor`` / ``GET /api/doctor``).

Checks storage writability, external tool versions, and DB/Redis connectivity.
Every check is isolated so one failure never hides the others.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.config import Settings, get_settings
from app.services.ytdlp import ytdlp_version


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _check_writable(name: str, path: Path) -> dict:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".doctor_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return _check(name, True, f"writable: {path}")
    except OSError as exc:
        return _check(name, False, f"NOT writable: {path} ({exc})")


def _tool_version(name: str, argv: list[str]) -> dict:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return _check(name, False, f"not available ({exc})")
    out = (proc.stdout or proc.stderr or "").strip().splitlines()
    first = out[0] if out else ""
    return _check(name, proc.returncode == 0, first or f"exit {proc.returncode}")


def _check_db() -> dict:
    try:
        from sqlalchemy import text

        from app.db import session_scope

        with session_scope() as session:
            session.execute(text("SELECT 1"))
        return _check("database", True, "SELECT 1 ok")
    except Exception as exc:  # noqa: BLE001
        return _check("database", False, f"connection failed ({exc})")


def _check_redis() -> dict:
    try:
        from app.worker.queue import get_redis

        get_redis().ping()
        return _check("redis", True, "PING ok")
    except Exception as exc:  # noqa: BLE001
        return _check("redis", False, f"connection failed ({exc})")


def run_diagnostics(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    ffmpeg_bin = (
        str(Path(settings.ffmpeg_location) / "ffmpeg")
        if settings.ffmpeg_location
        else "ffmpeg"
    )
    deno_bin = settings.deno_path or "deno"

    checks = [
        _check_writable("ARCHIVE_ROOT", settings.archive_root),
        _check_writable("LOG_ROOT", settings.log_root),
        _check_writable("CONFIG_ROOT", settings.config_root),
        _check("yt-dlp", ytdlp_version(settings) is not None, ytdlp_version(settings) or "not available"),
        _tool_version("ffmpeg", [ffmpeg_bin, "-version"]),
        _tool_version("deno", [deno_bin, "--version"]),
        _check_db(),
        _check_redis(),
    ]
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
