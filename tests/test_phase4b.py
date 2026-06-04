"""Phase 4B tests: scheduler comment integration, 429 backoff, live chat refresh.

All tests run on the SQLite fixture DB; yt-dlp is faked by monkeypatching
``app.worker.tasks.run_ytdlp`` to drop the expected output files.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Job, LiveChatMessage, MetadataSnapshot, Video
from app.services import comment_policy
from app.services import jobs as jobs_svc
from app.services import live_chat as live_chat_svc
from app.services.profiles import BUILTIN_PROFILES, BuildContext, build_ytdlp_args
from app.services.scheduler import run_once
from app.services.ytdlp import CompletedRun

runner = CliRunner()
VID = "dQw4w9WgXcQ"


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _video(session, vid=VID, **kw):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}", title="t", **kw)
    session.add(v)
    session.flush()
    return v


# --------------------------------------------------------------------------- #
# Live chat JSONL parsing
# --------------------------------------------------------------------------- #
def _text_action(mid, text, author="Alice", aid="UCalice", usec="1600000000000000", time="0:05"):
    return {"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {
            "id": mid,
            "message": {"runs": [{"text": text}]},
            "authorName": {"simpleText": author},
            "authorExternalChannelId": aid,
            "timestampUsec": usec,
            "timestampText": {"simpleText": time},
        }}}}]}}


def _paid_action(mid, text, amount="¥500", author="Bob", aid="UCbob", usec="1600000001000000"):
    return {"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {
        "liveChatPaidMessageRenderer": {
            "id": mid,
            "message": {"runs": [{"text": text}]},
            "authorName": {"simpleText": author},
            "authorExternalChannelId": aid,
            "timestampUsec": usec,
            "purchaseAmountText": {"simpleText": amount},
        }}}}]}}


def _jsonl(*objs) -> str:
    return "\n".join(json.dumps(o) for o in objs) + "\n"


def test_parse_live_chat_text_and_paid():
    text = _jsonl(
        _text_action("m1", "hello world"),
        _paid_action("m2", "nice stream", amount="¥500"),
        {"videoMetadataRenderer": {}},  # ignored noise line
    )
    msgs = list(live_chat_svc.parse_live_chat_jsonl(text))
    assert [m.message_id for m in msgs] == ["m1", "m2"]
    m1, m2 = msgs
    assert m1.text == "hello world" and m1.message_type == "text" and not m1.is_superchat
    assert m1.author_name == "Alice" and m1.time_text == "0:05"
    assert m1.timestamp_ms == 1600000000000
    assert m2.message_type == "paid" and m2.is_superchat
    assert m2.amount_value == 500.0 and m2.currency == "JPY"
    assert m2.amount_text == "¥500"


def test_parse_live_chat_skips_garbage_lines():
    text = "not json\n" + _jsonl(_text_action("m1", "ok")) + "{bad json\n"
    msgs = list(live_chat_svc.parse_live_chat_jsonl(text))
    assert [m.message_id for m in msgs] == ["m1"]


# --------------------------------------------------------------------------- #
# Live chat ingest (diff detection + cap)
# --------------------------------------------------------------------------- #
def test_ingest_live_chat_new_update_missing_refound(session):
    v = _video(session)
    msgs = list(live_chat_svc.parse_live_chat_jsonl(
        _jsonl(_text_action("m1", "a"), _text_action("m2", "b"))
    ))
    r1 = live_chat_svc.ingest_live_chat(session, v, msgs, mark_missing=True)
    assert r1["new"] == 2 and r1["fetched"] == 2

    # m1 edited, m2 gone, m3 new -> mark_missing on
    msgs2 = list(live_chat_svc.parse_live_chat_jsonl(
        _jsonl(_text_action("m1", "edited"), _text_action("m3", "c"))
    ))
    r2 = live_chat_svc.ingest_live_chat(session, v, msgs2, mark_missing=True)
    assert r2["new"] == 1 and r2["updated"] == 1 and r2["marked_missing"] == 1
    m2 = session.query(LiveChatMessage).filter_by(message_id="m2").one()
    assert m2.is_deleted_or_missing is True

    # m2 reappears -> refound
    msgs3 = list(live_chat_svc.parse_live_chat_jsonl(
        _jsonl(_text_action("m1", "edited"), _text_action("m2", "b"), _text_action("m3", "c"))
    ))
    r3 = live_chat_svc.ingest_live_chat(session, v, msgs3, mark_missing=True)
    assert r3["refound"] == 1 and r3["marked_missing"] == 0
    session.refresh(m2)
    assert m2.is_deleted_or_missing is False


def test_ingest_live_chat_cap_disables_mark_missing(session):
    v = _video(session)
    msgs = list(live_chat_svc.parse_live_chat_jsonl(
        _jsonl(_text_action("m1", "a"), _text_action("m2", "b"))
    ))
    live_chat_svc.ingest_live_chat(session, v, msgs, mark_missing=True)
    # batch has 2 msgs but limit=1 -> capped; m2 absent yet NOT marked missing
    msgs2 = list(live_chat_svc.parse_live_chat_jsonl(
        _jsonl(_text_action("m1", "a"), _text_action("m3", "c"))
    ))
    r = live_chat_svc.ingest_live_chat(session, v, msgs2, limit=1, mark_missing=True)
    assert r["capped"] is True and r["marked_missing"] == 0
    m2 = session.query(LiveChatMessage).filter_by(message_id="m2").one()
    assert m2.is_deleted_or_missing is False


def test_live_chat_message_unique_constraint(session):
    from sqlalchemy.exc import IntegrityError

    v = _video(session)
    session.add(LiveChatMessage(video_id=v.id, message_id="dup"))
    session.flush()
    session.add(LiveChatMessage(video_id=v.id, message_id="dup"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# --------------------------------------------------------------------------- #
# live_chat_refresh_only profile + job
# --------------------------------------------------------------------------- #
def test_live_chat_profile_args():
    spec = BUILTIN_PROFILES["live_chat_refresh_only"]
    ctx = BuildContext(output_template="/tmp/%(id)s.%(ext)s", download_archive=None,
                       no_playlist=True, default_sub_langs="ja,en", archive_sub_langs="ja,en")
    args = build_ytdlp_args(spec, ctx)
    assert "--skip-download" in args
    assert "--write-subs" in args
    assert "--sub-langs" in args and args[args.index("--sub-langs") + 1] == "live_chat"
    assert "--write-info-json" in args
    # NEVER re-download the body / comments / full-subtitle blowups:
    assert "--write-comments" not in args
    assert "all" not in args  # no --sub-langs all
    assert "-f" not in args and "--format" not in args


def test_create_live_chat_refresh_job(session):
    v = _video(session)
    job = jobs_svc.create_live_chat_refresh_job(session, v)
    assert job.type == "live_chat_refresh"
    assert job.profile_name == "live_chat_refresh_only"
    assert job.meta["target_video_id"] == VID


# --------------------------------------------------------------------------- #
# comment_policy: refreshable / backoff / live-chat scheduling
# --------------------------------------------------------------------------- #
def test_select_refreshable_due_only_vs_all(session):
    now = datetime(2026, 6, 1)
    _video(session, vid="due0000000a", next_comments_refresh_at=now - timedelta(days=1))
    _video(session, vid="future00001", next_comments_refresh_at=now + timedelta(days=5))
    _video(session, vid="frozen00001", comments_state="comments_disabled")
    due = comment_policy.select_refreshable_videos(session, now, due_only=True)
    assert {v.youtube_video_id for v in due} == {"due0000000a"}
    all_ = comment_policy.select_refreshable_videos(session, now, due_only=False)
    assert {v.youtube_video_id for v in all_} == {"due0000000a", "future00001"}  # frozen excluded


def test_count_frozen_and_recent(session):
    now = datetime(2026, 6, 1)
    _video(session, vid="frozenaaaaa", comments_state="frozen")
    _video(session, vid="recent00001", next_comments_refresh_at=now + timedelta(days=2))
    _video(session, vid="due00000002", next_comments_refresh_at=now - timedelta(days=2))
    assert comment_policy.count_frozen(session) == 1
    assert comment_policy.count_recent(session, now) == 1


def test_apply_comment_backoff_bumps_failures(session):
    settings = get_settings()
    settings.comments_refresh_retry_backoff_seconds = 3600
    settings.comments_refresh_max_retry = 2
    now = datetime(2026, 6, 1)
    v = _video(session)
    nxt = comment_policy.apply_comment_backoff(v, now, settings)
    assert v.comment_refresh_failures == 1
    assert nxt == now + timedelta(seconds=3600)
    # at/after max_retry -> backoff at least 1 day
    comment_policy.apply_comment_backoff(v, now, settings)
    assert v.comment_refresh_failures == 2
    assert v.next_comments_refresh_at >= now + timedelta(days=1)


def test_compute_next_live_chat_refresh_and_due(session):
    settings = get_settings()
    settings.live_chat_refresh_interval_seconds = 100
    now = datetime(2026, 6, 1)
    live = _video(session, vid="live0000001", has_live_chat=True,
                  next_live_chat_refresh_at=now - timedelta(seconds=10))
    _video(session, vid="frozenlive0", has_live_chat=True, live_chat_state="unavailable")
    _video(session, vid="normalvideo", has_live_chat=False)  # no live chat -> excluded
    due = comment_policy.select_due_live_chat_videos(session, now)
    assert {v.youtube_video_id for v in due} == {"live0000001"}
    assert comment_policy.compute_next_live_chat_refresh(live, now, settings) == now + timedelta(seconds=100)
    frozen = session.query(Video).filter_by(youtube_video_id="frozenlive0").one()
    assert comment_policy.compute_next_live_chat_refresh(frozen, now, settings) is None


# --------------------------------------------------------------------------- #
# Worker: _run_live_chat_refresh (faked yt-dlp)
# --------------------------------------------------------------------------- #
def _fake_run(output_files: dict, *, returncode: int = 0, stderr: str = ""):
    """Build a fake run_ytdlp that writes ``output_files`` ({ext: content}) into
    the -o template dir and returns a CompletedRun."""
    def fake(argv, log_dir, *, url=None, settings=None, timeout=None):
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        out_tpl = None
        for i, a in enumerate(argv):
            if a == "-o":
                out_tpl = argv[i + 1]
                break
        work_dir = Path(out_tpl).parent
        work_dir.mkdir(parents=True, exist_ok=True)
        vid = url.rsplit("=", 1)[-1] if url else "vid"
        for ext, content in output_files.items():
            (work_dir / f"{vid}.{ext}").write_text(content, encoding="utf-8")
        stdout_p = log_dir / "stdout.log"; stdout_p.write_text("", encoding="utf-8")
        stderr_p = log_dir / "stderr.log"; stderr_p.write_text(stderr, encoding="utf-8")
        cmd_p = log_dir / "command.txt"; cmd_p.write_text("yt-dlp ...\n", encoding="utf-8")
        return CompletedRun(returncode=returncode, command=list(argv), command_display="yt-dlp ...",
                            stdout_path=stdout_p, stderr_path=stderr_p, command_path=cmd_p)
    return fake


def test_worker_live_chat_refresh_available(session, monkeypatch):
    import app.worker.tasks as tasks

    v = _video(session)
    job = jobs_svc.create_live_chat_refresh_job(session, v)
    job_id, vpk = job.id, v.id
    session.commit()

    info = json.dumps({"id": VID, "title": "Live!"})
    chat = _jsonl(_text_action("m1", "hi"), _paid_action("m2", "thanks", amount="¥1000"))
    monkeypatch.setattr(tasks, "run_ytdlp", _fake_run({"info.json": info, "live_chat.json": chat}))

    tasks.run_job(job_id)

    with session_scope() as s:
        j = s.get(Job, job_id)
        assert j.status == "success"
        assert j.meta["live_chat_state"] == "available"
        assert j.meta["fetched_messages_count"] == 2
        assert j.meta["inserted_count"] == 2
        assert j.meta["snapshot_id"] is not None
        v2 = s.get(Video, vpk)
        assert v2.has_live_chat is True
        assert v2.live_chat_state == "available"
        assert v2.last_live_chat_refresh_at is not None
        assert v2.next_live_chat_refresh_at is not None
        msgs = s.query(LiveChatMessage).filter_by(video_id=vpk).all()
        assert len(msgs) == 2
        snaps = s.query(MetadataSnapshot).filter_by(
            video_id=vpk, snapshot_type="live_chat_refresh").all()
        assert len(snaps) == 1 and snaps[0].checksum


def test_worker_live_chat_refresh_not_available_for_normal_video(session, monkeypatch):
    """A normal (non-live) video has only info.json, no live_chat.json -> not_available,
    success (not an error), and zero media body files written."""
    import app.worker.tasks as tasks

    v = _video(session, vid="normalvid01")
    job = jobs_svc.create_live_chat_refresh_job(session, v)
    job_id, vpk = job.id, v.id
    session.commit()

    info = json.dumps({"id": "normalvid01", "title": "Not live"})
    monkeypatch.setattr(tasks, "run_ytdlp", _fake_run({"info.json": info}))  # NO live_chat.json

    tasks.run_job(job_id)

    with session_scope() as s:
        j = s.get(Job, job_id)
        assert j.status == "success"
        assert j.meta["live_chat_state"] == "not_available"
        assert j.meta["fetched_messages_count"] == 0
        v2 = s.get(Video, vpk)
        assert v2.live_chat_state == "not_available"
        assert v2.has_live_chat is False
        assert s.query(LiveChatMessage).filter_by(video_id=vpk).count() == 0


def test_worker_comments_429_backoff(session, monkeypatch):
    """On HTTP 429 the comments job is retryable and next_comments_refresh_at backs off."""
    import app.worker.tasks as tasks

    settings = get_settings()
    settings.comments_refresh_retry_backoff_seconds = 7200
    v = _video(session)
    job = jobs_svc.create_comments_refresh_job(session, v)
    job_id, vpk = job.id, v.id
    session.commit()

    monkeypatch.setattr(
        tasks, "run_ytdlp",
        _fake_run({}, returncode=1, stderr="ERROR: HTTP Error 429: Too Many Requests"),
    )
    tasks.run_job(job_id)

    with session_scope() as s:
        j = s.get(Job, job_id)
        assert j.status == "failed"
        assert j.meta["rate_limited"] is True
        assert j.meta["retryable"] is True
        v2 = s.get(Video, vpk)
        assert v2.comment_refresh_failures == 1
        assert v2.next_comments_refresh_at is not None


# --------------------------------------------------------------------------- #
# Scheduler: comment integration
# --------------------------------------------------------------------------- #
def test_scheduler_comments_enqueues_due(session):
    now = datetime(2026, 6, 1)
    _video(session, vid="duecomment1", next_comments_refresh_at=now - timedelta(days=1))
    _video(session, vid="frozencmt01", comments_state="frozen")
    session.commit()
    settings = get_settings()
    settings.scheduler_enabled = False
    settings.scheduler_comments_enabled = True
    summary = run_once(settings, reason="scheduler")
    assert summary["enabled"] is True
    assert summary["comments_jobs_created"] >= 1
    assert summary["collection_jobs_created"] == 0  # collections gated off
    assert summary["skipped_frozen"] >= 1
    with session_scope() as s:
        jobs_ = s.scalars(select_jobs("comments_refresh")).all()
        assert any((j.meta or {}).get("scheduled_by") == "scheduler_comments" for j in jobs_)


def test_scheduler_comments_disabled_skips(session):
    _video(session, vid="duecomment2", next_comments_refresh_at=datetime(2020, 1, 1))
    session.commit()
    settings = get_settings()
    settings.scheduler_enabled = False
    settings.scheduler_comments_enabled = False
    summary = run_once(settings, reason="scheduler")
    assert summary["enabled"] is False
    assert summary["comments_jobs_created"] == 0


def select_jobs(job_type):
    from sqlalchemy import select
    return select(Job).where(Job.type == job_type)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_api_comments_due(client):
    with session_scope() as s:
        _video(s, vid="apidue00001", next_comments_refresh_at=datetime(2020, 1, 1))
    body = client.get("/api/comments/due").json()
    assert body["count"] >= 1
    assert any(v["youtube_video_id"] == "apidue00001" for v in body["videos"])


def test_api_comments_refresh_all_due_only_and_all(client):
    with session_scope() as s:
        _video(s, vid="apidueall01", next_comments_refresh_at=datetime(2030, 1, 1))  # not due
    due = client.post("/api/comments/refresh-all", json={"due_only": True}).json()
    assert due["due_only"] is True
    all_ = client.post("/api/comments/refresh-all", json={"all": True}).json()
    assert all_["due_only"] is False
    assert all_["videos_selected"] >= 1  # picks the not-yet-due video too


def test_api_live_chat_refresh_target_video_alias_and_conflict(client):
    assert client.post("/api/live-chat/refresh", json={"target": VID}).status_code == 201
    assert client.post("/api/live-chat/refresh", json={"video": VID}).status_code == 201
    assert client.post(
        "/api/live-chat/refresh", json={"target": VID, "video": VID}
    ).status_code == 400
    assert client.post("/api/live-chat/refresh", json={}).status_code == 400


def test_api_live_chat_list_and_stats(client):
    with session_scope() as s:
        v = _video(s, vid="apichat0001", has_live_chat=True, live_chat_state="available")
        s.add(LiveChatMessage(video_id=v.id, message_id="m1", message="hi",
                              author_name="A", is_superchat=False, timestamp_ms=1))
        s.add(LiveChatMessage(video_id=v.id, message_id="m2", message="sc", author_name="B",
                              is_superchat=True, amount=500.0, amount_text="¥500", timestamp_ms=2))
        vid_pk = v.id
    msgs = client.get(f"/api/videos/{vid_pk}/live-chat").json()
    assert len(msgs) == 2
    assert all(m["raw_json"] is None for m in msgs)  # raw hidden by default
    sc = client.get(f"/api/videos/{vid_pk}/live-chat?superchats_only=true").json()
    assert len(sc) == 1 and sc[0]["message_id"] == "m2"
    stats = client.get(f"/api/videos/{vid_pk}/live-chat/stats").json()
    assert stats["total"] == 2 and stats["superchats"] == 1
    assert stats["has_live_chat"] is True and stats["live_chat_state"] == "available"


def test_api_scheduler_run_once_detailed(client):
    body = client.post("/api/scheduler/run-once", json={"collections": True, "comments": True}).json()
    for key in ("collections_checked", "collection_jobs_created", "due_comment_videos_checked",
                "comments_jobs_created", "skipped_frozen", "skipped_recent", "job_ids"):
        assert key in body


def test_api_scheduler_status_comments_fields(client):
    body = client.get("/api/scheduler/status").json()
    assert "comments_enabled" in body and "due_comment_videos" in body


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_live_chat_refresh_and_list_stats(settings):
    # create a video + chat row directly, then exercise list/stats
    with session_scope() as s:
        v = _video(s, vid="clichat0001", has_live_chat=True, live_chat_state="available")
        s.add(LiveChatMessage(video_id=v.id, message_id="m1", message="hello",
                              author_name="A", time_text="0:01", timestamp_ms=1))
    r_list = runner.invoke(cli_app, ["live-chat", "list", "clichat0001"])
    assert r_list.exit_code == 0 and "hello" in r_list.stdout
    r_stats = runner.invoke(cli_app, ["live-chat", "stats", "clichat0001"])
    assert r_stats.exit_code == 0 and "total messages" in r_stats.stdout
    # refresh enqueues a job (Redis down -> stays queued, no crash)
    r_ref = runner.invoke(cli_app, ["live-chat", "refresh", "clichat0001"])
    assert r_ref.exit_code == 0
    with session_scope() as s:
        assert s.query(Job).filter_by(type="live_chat_refresh").count() >= 1


def test_cli_comments_due_and_schedule(settings):
    with session_scope() as s:
        _video(s, vid="clidue00001", next_comments_refresh_at=datetime(2020, 1, 1))
    r_due = runner.invoke(cli_app, ["comments", "due"])
    assert r_due.exit_code == 0 and "clidue00001" in r_due.stdout
    r_sched = runner.invoke(cli_app, ["comments", "schedule", "clidue00001", "--now-due"])
    assert r_sched.exit_code == 0 and "next_comments_refresh_at" in r_sched.stdout


def test_cli_scheduler_run_once_flags(settings):
    r = runner.invoke(cli_app, ["scheduler", "run-once", "--comments"])
    assert r.exit_code == 0
    assert "comment_jobs=" in r.stdout and "due_comment_videos=" in r.stdout
