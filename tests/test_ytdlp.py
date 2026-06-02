"""yt-dlp wrapper plumbing & log saving (requirement 15: test log saving)."""

from __future__ import annotations

from app.services.ytdlp import build_command, run_ytdlp


def test_build_command_appends_url(settings):
    settings.ytdlp_binary = "yt-dlp"
    cmd = build_command(settings, ["--write-info-json"], url="https://youtu.be/x")
    assert cmd[0] == "yt-dlp"
    assert cmd[-1] == "https://youtu.be/x"
    assert "--write-info-json" in cmd


def test_build_command_batch_file(settings):
    cmd = build_command(settings, [], batch_file="/tmp/urls.txt")
    assert cmd[-2:] == ["-a", "/tmp/urls.txt"]


def test_run_ytdlp_writes_logs_and_command(settings, tmp_path):
    settings.ytdlp_binary = "/bin/echo"  # deterministic, no network
    log_dir = tmp_path / "joblog"
    run = run_ytdlp(
        ["--simulate", "--password", "hunter2"],
        log_dir,
        url="https://youtu.be/dQw4w9WgXcQ",
        settings=settings,
    )
    assert run.ok
    assert run.returncode == 0
    # three artifacts are always written
    assert run.command_path.is_file()
    assert run.stdout_path.is_file()
    assert run.stderr_path.is_file()
    # echo printed the args (incl. URL) to stdout
    assert "dQw4w9WgXcQ" in run.stdout_path.read_text()
    # command.txt records the command but masks the password value
    command_text = run.command_path.read_text()
    assert "/bin/echo" in command_text
    assert "hunter2" not in command_text
    assert "******" in command_text


def test_run_ytdlp_missing_binary(settings, tmp_path):
    settings.ytdlp_binary = "definitely_not_a_real_binary_xyz"
    run = run_ytdlp(["--version"], tmp_path / "log2", settings=settings)
    assert not run.ok
    assert run.returncode == 127
    assert "not found" in run.stderr_path.read_text().lower()
