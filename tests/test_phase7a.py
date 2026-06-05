"""Phase 7A tests: classification v3, retry/backoff, retryable API/CLI,
subtitles_refresh (no body), subtitle-failure extraction, secret masking."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Job, MediaFile, Subtitle, Video
from app.services import jobs as jobs_svc
from app.services.job_classify import (
    classify_text,
    compute_next_retry_at,
    subtitles_only_failure,
)
from app.services.profiles import BUILTIN_PROFILES, BuildContext, build_ytdlp_args
from app.services.ytdlp import CompletedRun, redact_args


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _video(s, vid, **kw):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}", title="T", **kw)
    s.add(v)
    s.flush()
    return v


# --------------------------------------------------------------------------- #
# Classification v3
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("ERROR: HTTP Error 429: Too Many Requests", "rate_limited"),
        ("WARNING: Incomplete data received. Retrying", "incomplete_data"),
        ("ERROR: unable to download video data: fragment 3 not found", "fragments_failed"),
        ("WARNING: unable to download subtitles for 'en'", "subtitles_failed"),
        ("ERROR: unable to download comments", "comments_failed"),
        ("WARNING: could not find a suitable impersonate target", "impersonation"),
        ("quotaExceeded: The request cannot be completed", "quota_exceeded"),
        ("invalid_grant: Token has been expired or revoked", "token_expired"),
    ],
)
def test_classify_categories(text, expected):
    c = classify_text("failed", text, {})
    assert expected in c["reasons"]


def test_retryable_is_precise():
    # plain failure (video deleted) -> NOT retryable
    assert classify_text("failed", "ERROR: Video unavailable", {})["retryable"] is False
    # 429 -> retryable
    assert classify_text("failed", "HTTP Error 429", {})["retryable"] is True
    # partial_success -> retryable
    assert classify_text("partial_success", "", {})["retryable"] is True
    # auth_required / forbidden -> NOT retryable (need setup)
    assert classify_text("failed", "auth_required: not configured", {})["retryable"] is False
    assert classify_text("failed", "forbidden: accessNotConfigured", {})["retryable"] is False


def test_subtitles_only_failure_helper():
    assert subtitles_only_failure(classify_text("partial_success", "unable to download subtitles", {})) is True
    # 429 alongside subtitles -> not a pure subtitle-only failure
    assert subtitles_only_failure(classify_text("failed", "HTTP Error 429; unable to download subtitles", {})) is False


def test_compute_next_retry_backoff(settings):
    settings.download_retry_max_attempts = 3
    settings.download_retry_backoff_seconds = 100
    settings.download_retry_backoff_multiplier = 2.0
    settings.download_retry_jitter_seconds = 0
    now = datetime(2026, 1, 1)
    assert compute_next_retry_at(["rate_limited"], 0, settings, now) == now + timedelta(seconds=100)
    assert compute_next_retry_at(["rate_limited"], 1, settings, now) == now + timedelta(seconds=200)
    assert compute_next_retry_at(["rate_limited"], 2, settings, now) == now + timedelta(seconds=400)
    # over the cap -> None
    assert compute_next_retry_at(["rate_limited"], 3, settings, now) is None
    # non-retryable reason -> None
    assert compute_next_retry_at(["auth_required"], 0, settings, now) is None


def test_apply_classification_schedules_retry(settings):
    settings.download_retry_max_attempts = 5
    settings.download_retry_backoff_seconds = 60
    with session_scope() as s:
        j = Job(type="download", status="failed", error_message="HTTP Error 429")
        s.add(j)
        s.flush()
        c = jobs_svc.apply_classification(s, j, settings, "HTTP Error 429")
        assert c["rate_limited"] and c["retryable"]
        assert j.meta["classification"]["reasons"] == ["rate_limited"]
        assert j.next_retry_at is not None  # backoff scheduled
        # a success clears the retry schedule
        j2 = Job(type="download", status="success")
        s.add(j2)
        s.flush()
        jobs_svc.apply_classification(s, j2, settings, "")
        assert j2.next_retry_at is None


# --------------------------------------------------------------------------- #
# Retryable API + retry cap
# --------------------------------------------------------------------------- #
def test_retryable_api_and_retry_all(client, settings):
    with session_scope() as s:
        s.add(Job(type="download", status="failed", url="a", error_message="HTTP Error 429"))
        s.add(Job(type="download", status="failed", url="b", error_message="ERROR: Video unavailable"))
        s.add(Job(type="comments_refresh", status="partial_success", url="c",
                  error_message="unable to download subtitles"))
    rows = client.get("/api/jobs/retryable").json()
    urls = {r["url"] for r in rows}
    assert "a" in urls and "c" in urls and "b" not in urls  # 'b' is not retryable
    # filter by reason
    only_rl = client.get("/api/jobs/retryable?reason=rate_limited").json()
    assert all("rate_limited" in r["classification"]["reasons"] for r in only_rl)
    # retry-all
    res = client.post("/api/jobs/retry-all", json={"limit": 50}).json()
    assert res["retried"] >= 2


def test_retry_cap_enforced(client, settings):
    settings.download_retry_max_attempts = 2
    with session_scope() as s:
        j = Job(type="download", status="failed", url="x", error_message="HTTP Error 429", retry_count=2)
        s.add(j)
        s.flush()
        jid = j.id
    r = client.post(f"/api/jobs/{jid}/retry")
    assert r.status_code == 409  # cap reached
    assert client.post(f"/api/jobs/{jid}/retry?force=true").status_code == 200  # override


def test_retry_increments_count(settings):
    with session_scope() as s:
        j = Job(type="download", status="failed", retry_count=0, next_retry_at=datetime(2026, 1, 1))
        s.add(j)
        s.flush()
        jobs_svc.retry_job(s, j)
        assert j.status == "queued" and j.retry_count == 1 and j.next_retry_at is None


# --------------------------------------------------------------------------- #
# subtitles_refresh: profile (no body) + worker (no body) + API
# --------------------------------------------------------------------------- #
def test_subtitles_profile_no_body():
    spec = BUILTIN_PROFILES["subtitles_refresh_only"]
    ctx = BuildContext(output_template="/tmp/%(id)s.%(ext)s", download_archive=None,
                       no_playlist=True, default_sub_langs="ja,en", archive_sub_langs="ja,en")
    args = build_ytdlp_args(spec, ctx)
    assert "--skip-download" in args
    assert "--write-subs" in args and "--write-auto-subs" in args
    assert "--sub-langs" in args and args[args.index("--sub-langs") + 1] == "ja,en"
    assert "--remote-components" in args and args[args.index("--remote-components") + 1] == "ejs:github"
    assert "-f" not in args and "--format" not in args  # NO body format
    assert "--write-comments" not in args
    assert "all" not in args


def _fake_run(files: dict, *, returncode=0, stderr=""):
    def fake(argv, log_dir, *, url=None, settings=None, timeout=None):
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        out_tpl = next(argv[i + 1] for i, a in enumerate(argv) if a == "-o")
        d = Path(out_tpl).parent
        d.mkdir(parents=True, exist_ok=True)
        for fname, content in files.items():
            (d / fname).write_text(content)
        sp = log_dir / "stdout.log"; sp.write_text("")
        ep = log_dir / "stderr.log"; ep.write_text(stderr)
        cp = log_dir / "command.txt"; cp.write_text("yt-dlp")
        return CompletedRun(returncode=returncode, command=list(argv), command_display="yt-dlp",
                            stdout_path=sp, stderr_path=ep, command_path=cp)
    return fake


def test_subtitles_refresh_worker_creates_subs_no_body(settings, monkeypatch):
    import app.worker.tasks as tasks

    with session_scope() as s:
        v = _video(s, "subw0000001", channel_id="UCx")
        job = jobs_svc.create_subtitles_refresh_job(s, v)
        s.commit()
        jid, vpk = job.id, v.id

    monkeypatch.setattr(
        tasks, "run_ytdlp", _fake_run({"My Vid [subw0000001].en.vtt": "WEBVTT\n"})
    )
    tasks.run_job(jid)

    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.status == "success"
        assert job.meta["subtitle_files_created"] == 1
        assert job.meta["subtitles_failed"] is False
        body = s.query(MediaFile).filter(
            MediaFile.video_id == vpk, MediaFile.media_type.in_(("video", "audio"))
        ).count()
        assert body == 0  # NEVER a body
        assert s.query(Subtitle).filter_by(video_id=vpk).count() == 1


def test_subtitles_api_refresh_and_failed_extraction(client, settings):
    # a metadata job that failed subtitles -> appears in /subtitles/failed
    with session_scope() as s:
        v = _video(s, "subfail0001")
        s.add(Job(type="metadata_refresh", status="partial_success", url="u", video_id=v.id,
                  error_message="WARNING: unable to download subtitles for 'en': HTTP Error 429"))
    failed = client.get("/api/subtitles/failed").json()
    assert any(j["video_id"] for j in failed)
    # refresh by target
    r = client.post("/api/subtitles/refresh", json={"target": "subfail0001"})
    assert r.status_code == 201 and r.json()["type"] == "subtitles_refresh"
    assert r.json()["profile_name"] == "subtitles_refresh_only"
    # refresh-failed creates jobs for failed-subtitle videos
    rf = client.post("/api/subtitles/refresh-failed").json()
    assert rf["jobs_created"] >= 1


# --------------------------------------------------------------------------- #
# Secret masking (PO token)
# --------------------------------------------------------------------------- #
def test_po_token_masked_in_command_and_logs():
    args = ["yt-dlp", "--extractor-args", "youtube:po_token=SECRETPOTOKEN123", "url"]
    red = redact_args(args)
    assert "SECRETPOTOKEN123" not in " ".join(red)
    assert "po_token=******" in " ".join(red)
    # read-time log masking too
    from app.services.logs import mask_secrets

    out = mask_secrets("loaded youtube:po_token=SECRETPOTOKEN123 ok")
    assert "SECRETPOTOKEN123" not in out
