"""Phase 1.5 tests: log APIs/CLI, path-traversal guard, dry-run, doctor."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Job
from app.services import jobs as jobs_svc
from app.services import logs as logs_svc
from app.services import storage
from app.services.command_builder import dry_run_command
from app.services.doctor import run_diagnostics

runner = CliRunner()


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _make_job_with_logs(settings, *, status: str = "success") -> int:
    with session_scope() as s:
        job = jobs_svc.create_job_for_url(
            s, "https://youtu.be/dQw4w9WgXcQ", "metadata_only"
        )
        job_id = job.id
        log_dir = storage.job_log_dir(settings, job_id)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "command.txt").write_text(
            "yt-dlp --sub-langs ja,en --remote-components ejs:github URL\n",
            encoding="utf-8",
        )
        (log_dir / "yt-dlp.stdout.log").write_text(
            "STDOUT-LINE-1\nSTDOUT-LINE-2\n", encoding="utf-8"
        )
        (log_dir / "yt-dlp.stderr.log").write_text("STDERR-WARNING\n", encoding="utf-8")
        job.log_path = storage.log_relative(settings, log_dir)
        job.status = status
    return job_id


# --------------------------------------------------------------------------- #
# Log APIs
# --------------------------------------------------------------------------- #
def test_logs_api_reads_all_streams(client, settings):
    job_id = _make_job_with_logs(settings)
    resp = client.get(f"/api/jobs/{job_id}/logs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert "ja,en" in body["command"]
    assert "STDOUT-LINE-1" in body["stdout"]
    assert "STDERR-WARNING" in body["stderr"]


def test_logs_api_per_stream_plaintext(client, settings):
    job_id = _make_job_with_logs(settings)
    assert "ja,en" in client.get(f"/api/jobs/{job_id}/logs/command").text
    assert "STDOUT-LINE-2" in client.get(f"/api/jobs/{job_id}/logs/stdout").text
    assert "STDERR-WARNING" in client.get(f"/api/jobs/{job_id}/logs/stderr").text


def test_logs_api_tail(client, settings):
    job_id = _make_job_with_logs(settings)
    text = client.get(f"/api/jobs/{job_id}/logs/stdout", params={"tail": 1}).text
    assert "STDOUT-LINE-2" in text
    assert "STDOUT-LINE-1" not in text


def test_logs_api_404_missing_job(client):
    assert client.get("/api/jobs/999999/logs").status_code == 404
    assert client.get("/api/jobs/999999/logs/stdout").status_code == 404


def test_logs_api_404_unknown_stream(client, settings):
    job_id = _make_job_with_logs(settings)
    assert client.get(f"/api/jobs/{job_id}/logs/passwd").status_code == 404


def test_logs_api_404_when_no_log_files(client, settings):
    with session_scope() as s:
        job = jobs_svc.create_job_for_url(s, "https://youtu.be/dQw4w9WgXcQ", "metadata_only")
        job_id = job.id  # no log files written
    assert client.get(f"/api/jobs/{job_id}/logs/stdout").status_code == 404
    body = client.get(f"/api/jobs/{job_id}/logs").json()
    assert body["available"] is False


# --------------------------------------------------------------------------- #
# Path-traversal guard
# --------------------------------------------------------------------------- #
def test_path_traversal_is_rejected(settings):
    with session_scope() as s:
        evil = Job(type="download", status="failed", url="x",
                   log_path="../../../../../../etc")
        s.add(evil)
        s.flush()
        assert logs_svc.job_log_dir(settings, evil) is None
        assert logs_svc.read_log(settings, evil, "command") is None

        normal = Job(type="download", status="failed", url="y", log_path="jobs/999")
        s.add(normal)
        s.flush()
        # unknown stream name can never escape the fixed file map
        assert logs_svc.read_log(settings, normal, "../../etc/passwd") is None
        assert logs_svc.log_file_path(settings, normal, "passwd") is None


def test_job_detail_includes_log_paths(client, settings):
    job_id = _make_job_with_logs(settings)
    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["stdout_log_path"].endswith("/yt-dlp.stdout.log")
    assert body["stderr_log_path"].endswith("/yt-dlp.stderr.log")
    assert body["command_log_path"].endswith("/command.txt")
    assert body["profile"]["name"] == "metadata_only"


# --------------------------------------------------------------------------- #
# Dry-run / build-command
# --------------------------------------------------------------------------- #
def test_dry_run_metadata_only_has_safe_flags(session):
    result = dry_run_command(
        session, get_settings(), "metadata_only", "https://youtu.be/dQw4w9WgXcQ"
    )
    cmd = result["command"]
    assert "--sub-langs ja,en" in cmd
    assert "--remote-components ejs:github" in cmd
    assert "--sub-langs all" not in cmd
    assert "--skip-download" in cmd


def test_dry_run_masks_cookie_path(session, settings, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# netscape cookie file\n", encoding="utf-8")
    settings.cookies_file = str(cookies)
    result = dry_run_command(
        session, settings, "metadata_only", "https://youtu.be/dQw4w9WgXcQ"
    )
    assert str(cookies) not in result["command"]
    assert "******" in result["command"]


def test_build_command_api(client):
    resp = client.post(
        "/api/profiles/video_compressed_1080p/build-command",
        json={"url": "https://youtu.be/dQw4w9WgXcQ"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "--sub-langs ja,en" in body["command"]
    assert "--remote-components ejs:github" in body["command"]
    assert "--sub-langs all" not in body["command"]


def test_build_command_api_unknown_profile_404(client):
    resp = client.post(
        "/api/profiles/does_not_exist/build-command",
        json={"url": "https://youtu.be/dQw4w9WgXcQ"},
    )
    assert resp.status_code == 404


def test_build_command_api_bad_url_400(client):
    resp = client.post(
        "/api/profiles/metadata_only/build-command",
        json={"url": "https://example.com/x"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #
def test_doctor_reports_checks(settings):
    result = run_diagnostics(settings)
    names = {c["name"] for c in result["checks"]}
    assert {"ARCHIVE_ROOT", "LOG_ROOT", "CONFIG_ROOT", "database", "yt-dlp"} <= names
    by_name = {c["name"]: c for c in result["checks"]}
    # temp roots are writable; sqlite DB connects
    assert by_name["ARCHIVE_ROOT"]["ok"] is True
    assert by_name["LOG_ROOT"]["ok"] is True
    assert by_name["database"]["ok"] is True


def test_doctor_api(client):
    body = client.get("/api/doctor").json()
    assert "checks" in body and "ok" in body
    assert any(c["name"] == "ARCHIVE_ROOT" for c in body["checks"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_jobs_logs_command(settings):
    job_id = _make_job_with_logs(settings)
    result = runner.invoke(cli_app, ["jobs", "logs", str(job_id), "--command"])
    assert result.exit_code == 0
    assert "ja,en" in result.stdout


def test_cli_jobs_show(settings):
    job_id = _make_job_with_logs(settings)
    result = runner.invoke(cli_app, ["jobs", "show", str(job_id)])
    assert result.exit_code == 0
    assert f"Job #{job_id}" in result.stdout
    assert "metadata_only" in result.stdout


def test_cli_profiles_command(settings):
    result = runner.invoke(
        cli_app, ["profiles", "command", "metadata_only", "https://youtu.be/dQw4w9WgXcQ"]
    )
    assert result.exit_code == 0
    assert "--sub-langs ja,en" in result.stdout
    assert "--remote-components ejs:github" in result.stdout
