"""Phase 7B tests: YouTube fetch-stabilization doctor + diagnostics.

Covers:
  - command builder consolidation (cookies / cookies-from-browser / PO token /
    visitor data / extractor-args) and secret redaction (write-time + read-time)
  - static doctor returns configured yes/no ONLY (no secret value / cookie path)
  - settings API exposes cookie file status (yes/no, no path)
  - youtube_diagnostic worker: mock success / 429 / incomplete-data ->
    classification + recommendations, and NEVER persists a media body
  - doctor / diagnostics API + CLI
"""

from __future__ import annotations

import unittest.mock as mock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Job, MediaFile
from app.services import jobs as jobs_svc
from app.services import youtube_doctor
from app.services.command_builder import external_ctx
from app.services.logs import mask_secrets
from app.services.profiles import BUILTIN_PROFILES, BuildContext, build_ytdlp_args
from app.services.ytdlp import CompletedRun, redact_args


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


SECRET_COOKIE = "/secrets/SENTINEL_COOKIE_PATH.txt"
SECRET_PO = "SENTINEL_PO_TOKEN_123"
SECRET_VD = "SENTINEL_VISITOR_DATA_456"


def _ctx_with_secrets(tmp_path: Path) -> BuildContext:
    return BuildContext(
        output_template=str(tmp_path / "%(id)s.%(ext)s"),
        download_archive=None,
        no_playlist=True,
        default_sub_langs="ja,en",
        archive_sub_langs="ja,en",
        cookies_file=SECRET_COOKIE,
        po_token=SECRET_PO,
        visitor_data=SECRET_VD,
        extractor_args_extra="youtube:player_client=web",
    )


# --------------------------------------------------------------------------- #
# 1. Command builder consolidation + extractor-args
# --------------------------------------------------------------------------- #
def test_extractor_args_combine_po_and_visitor(tmp_path):
    ctx = _ctx_with_secrets(tmp_path)
    args = build_ytdlp_args(BUILTIN_PROFILES["metadata_only"], ctx)
    extractor = [args[i + 1] for i, a in enumerate(args) if a == "--extractor-args"]
    # po_token + visitor_data combined into one youtube: arg; raw extra appended.
    assert any("po_token=" in e and "visitor_data=" in e and e.startswith("youtube:") for e in extractor)
    assert "youtube:player_client=web" in extractor
    # cookies file present
    assert "--cookies" in args and SECRET_COOKIE in args
    # remote-components + deno preserved
    assert "--remote-components" in args
    joined = " ".join(args)
    assert "ejs:github" in joined


def test_cookies_from_browser_used_when_no_cookies_file(tmp_path):
    ctx = BuildContext(
        output_template=str(tmp_path / "%(id)s.%(ext)s"),
        download_archive=None,
        no_playlist=True,
        default_sub_langs="ja,en",
        archive_sub_langs="ja,en",
        cookies_from_browser="chrome:Default",
    )
    args = build_ytdlp_args(BUILTIN_PROFILES["metadata_only"], ctx)
    assert "--cookies-from-browser" in args
    assert "--cookies" not in args  # mutually exclusive


# --------------------------------------------------------------------------- #
# 2. Secret redaction (write-time redact_args + read-time mask_secrets)
# --------------------------------------------------------------------------- #
def test_redact_args_masks_po_and_visitor(tmp_path):
    args = build_ytdlp_args(BUILTIN_PROFILES["metadata_only"], _ctx_with_secrets(tmp_path))
    red = " ".join(redact_args(["yt-dlp", *args]))
    assert SECRET_PO not in red and SECRET_VD not in red
    assert "po_token=******" in red and "visitor_data=******" in red


def test_redact_args_can_mask_cookie_path(tmp_path):
    args = build_ytdlp_args(BUILTIN_PROFILES["metadata_only"], _ctx_with_secrets(tmp_path))
    # default keeps the path (re-runnable command.txt); mask_cookies hides it.
    assert SECRET_COOKIE in " ".join(redact_args(["yt-dlp", *args]))
    assert SECRET_COOKIE not in " ".join(redact_args(["yt-dlp", *args], mask_cookies=True))


def test_mask_secrets_redacts_cookies_browser_po_visitor():
    line = (
        f"yt-dlp --cookies {SECRET_COOKIE} --cookies-from-browser chrome:Default "
        f"--extractor-args youtube:po_token={SECRET_PO};visitor_data={SECRET_VD}"
    )
    masked = mask_secrets(line)
    assert SECRET_COOKIE not in masked
    assert SECRET_PO not in masked and SECRET_VD not in masked
    assert "--cookies ***REDACTED***" in masked
    assert "--cookies-from-browser ***REDACTED***" in masked


# --------------------------------------------------------------------------- #
# 3. Static doctor: configured yes/no only, NO secret value / path
# --------------------------------------------------------------------------- #
def _settings_with_secrets(monkeypatch, tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("# netscape\n")
    monkeypatch.setenv("COOKIES_FILE", str(cookie))
    monkeypatch.setenv("YTDLP_COOKIES_FROM_BROWSER", "chrome")
    monkeypatch.setenv("YOUTUBE_PO_TOKEN", SECRET_PO)
    monkeypatch.setenv("YOUTUBE_VISITOR_DATA", SECRET_VD)
    get_settings.cache_clear()
    return get_settings(), str(cookie)


def test_static_checks_no_secret_leak(monkeypatch, tmp_path, settings):
    s, cookie_path = _settings_with_secrets(monkeypatch, tmp_path)
    import json

    blob = json.dumps(youtube_doctor.static_checks(s))
    for sentinel in (SECRET_PO, SECRET_VD, cookie_path, "SENTINEL"):
        assert sentinel not in blob
    d = youtube_doctor.static_checks(s)
    assert d["po_token_configured"] is True
    assert d["visitor_data_configured"] is True
    assert d["browser_cookies_configured"] is True
    assert d["cookies"]["configured"] is True
    assert d["cookies"]["file_exists"] is True
    assert d["cookies"]["readable"] is True
    # no raw path field anywhere in the cookies status
    assert set(d["cookies"]).issubset({"configured", "file_configured", "file_exists", "readable", "last_modified"})


def test_doctor_youtube_api_no_secret(client, monkeypatch, tmp_path):
    _settings_with_secrets(monkeypatch, tmp_path)
    r = client.get("/api/doctor/youtube")
    assert r.status_code == 200
    body = r.json()
    blob = r.text
    assert SECRET_PO not in blob and SECRET_VD not in blob and "SENTINEL" not in blob
    assert body["po_token_configured"] is True
    assert body["cookies"]["configured"] is True
    assert "recommendations" in body and isinstance(body["recommendations"], list)


def test_settings_api_cookie_status_yes_no(client, monkeypatch, tmp_path):
    _settings_with_secrets(monkeypatch, tmp_path)
    r = client.get("/api/settings")
    assert r.status_code == 200
    items = {it["key"]: it["value"] for it in r.json()["items"]}
    assert items["cookies_configured"] == "yes"
    assert items["cookies_file_exists"] == "yes"
    assert items["cookies_file_readable"] == "yes"
    assert items["browser_cookies_configured"] == "yes"
    assert items["po_token_configured"] == "yes"
    assert items["visitor_data_configured"] == "yes"
    # every value is a yes/no/scalar, never a path/secret
    assert SECRET_PO not in r.text and SECRET_VD not in r.text and "SENTINEL" not in r.text


# --------------------------------------------------------------------------- #
# 4. youtube_diagnostic worker: mock success / 429 / incomplete -> body=0
# --------------------------------------------------------------------------- #
def _fake_run(stderr: str, rc: int):
    def fake(argv, log_dir, *, url=None, settings=None, timeout=None):
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        sp = log_dir / "yt-dlp.stdout.log"
        sp.write_text("")
        ep = log_dir / "yt-dlp.stderr.log"
        ep.write_text(stderr)
        cp = log_dir / "command.txt"
        cp.write_text("yt-dlp")
        return CompletedRun(
            returncode=rc, command=list(argv), command_display="yt-dlp",
            stdout_path=sp, stderr_path=ep, command_path=cp,
        )

    return fake


def _run_diag(settings, stderr: str, rc: int, *, include_video=False):
    import app.worker.tasks as tasks

    with session_scope() as s:
        job = jobs_svc.create_youtube_diagnostic_job(
            s, "https://youtu.be/dQw4w9WgXcQ", include_video_download=include_video
        )
        s.commit()
        jid = job.id
    with mock.patch.object(youtube_doctor, "run_ytdlp", _fake_run(stderr, rc)):
        tasks.run_job(jid)
    return jid


def test_diagnostic_mock_success(settings):
    jid = _run_diag(settings, "", 0)
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.status == "success"
        assert j.meta["overall"] == "success"
        steps = {st["name"]: st for st in j.meta["diagnostic"]["steps"]}
        assert steps["metadata_only"]["status"] == "success"
        assert steps["subtitles"]["status"] == "success"
        assert all(st["media_body_created"] is False for st in steps.values())
        assert j.meta["recommendations"]
        # NEVER persists a media body
        assert s.query(MediaFile).filter(MediaFile.media_type.in_(("video", "audio"))).count() == 0


def test_diagnostic_mock_429_recommends_retry(settings):
    jid = _run_diag(settings, "ERROR: HTTP Error 429: Too Many Requests", 1)
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j.meta["overall"] == "failed"
        assert "rate_limited" in j.meta["diagnostic"]["reasons"]
        recs = " ".join(j.meta["recommendations"]).lower()
        assert "429" in recs or "retry" in recs
        assert s.query(MediaFile).filter(MediaFile.media_type.in_(("video", "audio"))).count() == 0


def test_diagnostic_mock_incomplete_data(settings):
    jid = _run_diag(settings, "WARNING: Incomplete data received. Retrying", 1)
    with session_scope() as s:
        j = s.get(Job, jid)
        assert "incomplete_data" in j.meta["diagnostic"]["reasons"]
        recs = " ".join(j.meta["recommendations"]).lower()
        assert "incomplete" in recs or "throttl" in recs


def test_diagnostic_no_secret_in_logs(settings, monkeypatch, tmp_path):
    # Even with PO token configured, the diagnostic's command log masks it.
    _settings_with_secrets(monkeypatch, tmp_path)
    s2 = get_settings()
    jid = _run_diag(s2, "", 0)
    with session_scope() as s:
        from app.services.logs import read_log

        j = s.get(Job, jid)
        # The job's top-level command log (if any) must not leak secrets.
        for stream in ("command", "stdout", "stderr"):
            txt = read_log(get_settings(), j, stream) or ""
            assert SECRET_PO not in txt and SECRET_VD not in txt


# --------------------------------------------------------------------------- #
# 5. Diagnostics API creates a youtube_diagnostic job
# --------------------------------------------------------------------------- #
def test_diagnostics_run_api_creates_job(client):
    r = client.post("/api/youtube-diagnostics/run", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 201
    body = r.json()
    assert body["type"] == "youtube_diagnostic"
    assert body["status"] in ("queued", "running")
    # video download default OFF
    with session_scope() as s:
        j = s.get(Job, body["id"])
        assert j.meta["include_video_download"] is False


def test_doctor_youtube_run_api(client):
    r = client.post("/api/doctor/youtube/run", json={"url": "https://youtu.be/x", "include_video_download": True})
    assert r.status_code == 201
    with session_scope() as s:
        j = s.get(Job, r.json()["id"])
        assert j.type == "youtube_diagnostic"
        assert j.meta["include_video_download"] is True


# --------------------------------------------------------------------------- #
# 6. CLI: doctor youtube (static) + youtube-diagnostics run
# --------------------------------------------------------------------------- #
def test_cli_doctor_youtube_static(settings, monkeypatch, tmp_path):
    _settings_with_secrets(monkeypatch, tmp_path)
    res = CliRunner().invoke(cli_app, ["doctor", "youtube"])
    assert res.exit_code == 0, res.output
    assert "static checks" in res.output
    assert "recommendations" in res.output.lower()
    # configured yes/no shown, secret values not
    assert SECRET_PO not in res.output and SECRET_VD not in res.output
    assert "po_token=True" in res.output or "po_token=True" in res.output.replace(" ", "")


def test_cli_doctor_general_still_works(settings):
    # Phase 0 `archiver doctor` (bare) must keep working after the group change.
    res = CliRunner().invoke(cli_app, ["doctor"])
    # exit 1 acceptable (redis down in tests) but it must run + show db check.
    assert "database" in res.output


def test_cli_youtube_diagnostics_run_creates_job(settings):
    res = CliRunner().invoke(
        cli_app, ["youtube-diagnostics", "run", "--url", "https://youtu.be/dQw4w9WgXcQ"]
    )
    assert res.exit_code == 0, res.output
    assert "youtube_diagnostic job" in res.output
    with session_scope() as s:
        assert s.query(Job).filter(Job.type == "youtube_diagnostic").count() == 1
