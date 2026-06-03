"""Phase 4A tests: comment diff, refresh job args, adaptive policy, API/CLI."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.main import app
from app.models import Comment, Video
from app.services import comment_policy
from app.services import jobs as jobs_svc
from app.services.ingest import ingest_comments_from_info
from app.services.profiles import BUILTIN_PROFILES, BuildContext, build_ytdlp_args

runner = CliRunner()


def _info(comments):
    return {"id": "dQw4w9WgXcQ", "title": "t", "comments": comments}


def _c(cid, text="x", like=0, parent="root", author="A", aid="UCa"):
    return {"id": cid, "parent": parent, "text": text, "like_count": like,
            "author": author, "author_id": aid, "timestamp": 1600000000}


def _video(session, vid="dQw4w9WgXcQ", **kw):
    v = Video(youtube_video_id=vid, title="t", **kw)
    session.add(v)
    session.flush()
    return v


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# Comment diff
# --------------------------------------------------------------------------- #
def test_comments_diff_new_update_missing_refound(session):
    v = _video(session)
    r1 = ingest_comments_from_info(session, v, _info([_c("c1"), _c("c2")]), mark_missing=True)
    assert r1["new"] == 2 and r1["marked_missing"] == 0

    # c1 edited, c2 disappears, c3 new -> mark_missing on
    r2 = ingest_comments_from_info(
        session, v, _info([_c("c1", text="edited"), _c("c3")]), mark_missing=True
    )
    assert r2["new"] == 1 and r2["updated"] == 1 and r2["marked_missing"] == 1
    c2 = session.query(Comment).filter_by(comment_id="c2").one()
    assert c2.is_deleted_or_missing is True

    # c2 reappears -> refound, missing cleared
    r3 = ingest_comments_from_info(
        session, v, _info([_c("c1", text="edited"), _c("c2"), _c("c3")]), mark_missing=True
    )
    assert r3["refound"] == 1 and r3["marked_missing"] == 0
    session.refresh(c2)
    assert c2.is_deleted_or_missing is False


def test_comments_no_mark_missing_when_capped(session):
    v = _video(session)
    ingest_comments_from_info(session, v, _info([_c("c1"), _c("c2")]), mark_missing=True)
    # capped fetch -> mark_missing False -> c2 NOT flagged
    r = ingest_comments_from_info(session, v, _info([_c("c1")]), mark_missing=False)
    assert r["marked_missing"] == 0
    assert session.query(Comment).filter_by(comment_id="c2").one().is_deleted_or_missing is False


def test_comment_unique_constraint(session):
    v = _video(session)
    session.add(Comment(video_id=v.id, comment_id="c1"))
    session.flush()
    session.add(Comment(video_id=v.id, comment_id="c1"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# --------------------------------------------------------------------------- #
# Refresh job args (never re-downloads body)
# --------------------------------------------------------------------------- #
def test_comments_refresh_profile_args():
    ctx = BuildContext(output_template="/snap/%(id)s.%(ext)s", max_comments=20)
    args = build_ytdlp_args(BUILTIN_PROFILES["comments_refresh_only"], ctx)
    assert "--skip-download" in args
    assert "--write-comments" in args
    assert "--no-download-archive" in args
    assert "--write-info-json" in args
    # COMMENT_REFRESH_MAX_COMMENTS-style cap applied
    idx = args.index("--extractor-args")
    assert "max_comments=20" in args[idx + 1]
    # never a video body: no format selection / merge
    assert "-f" not in args
    assert "--merge-output-format" not in args
    # YouTube JS challenge solver retained; no "all" subtitles
    assert "--remote-components" in args
    assert "--sub-langs" not in args


def test_create_comments_refresh_job(session):
    v = _video(session)
    job = jobs_svc.create_comments_refresh_job(session, v)
    assert job.type == "comments_refresh"
    assert job.video_id == v.id
    assert job.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert job.meta["target_video_id"] == "dQw4w9WgXcQ"


def test_resolve_or_create_video(session):
    v = jobs_svc.resolve_or_create_video(session, "https://youtu.be/dQw4w9WgXcQ")
    assert v.youtube_video_id == "dQw4w9WgXcQ"
    v2 = jobs_svc.resolve_or_create_video(session, "dQw4w9WgXcQ")
    assert v2.id == v.id  # no duplicate
    assert jobs_svc.resolve_or_create_video(session, "https://example.com/x") is None


# --------------------------------------------------------------------------- #
# Adaptive policy
# --------------------------------------------------------------------------- #
def test_compute_next_comment_refresh():
    now = datetime(2026, 6, 1)
    assert comment_policy.compute_next_comment_refresh(
        Video(youtube_video_id="a" * 11, upload_date="20260531"), now
    ) == now + timedelta(days=1)
    assert comment_policy.compute_next_comment_refresh(
        Video(youtube_video_id="b" * 11, upload_date="20200101"), now
    ) == now + timedelta(days=30)
    assert comment_policy.compute_next_comment_refresh(
        Video(youtube_video_id="c" * 11, comments_state="comments_disabled"), now
    ) is None


def test_select_due_videos(session):
    now = datetime(2026, 6, 1)
    session.add_all([
        Video(youtube_video_id="a" * 11),  # never refreshed -> due
        Video(youtube_video_id="b" * 11, next_comments_refresh_at=now - timedelta(days=1)),  # overdue
        Video(youtube_video_id="c" * 11, next_comments_refresh_at=now + timedelta(days=10)),  # not due
        Video(youtube_video_id="d" * 11, comments_state="frozen"),  # frozen -> excluded
    ])
    session.flush()
    ids = {v.youtube_video_id for v in comment_policy.select_due_videos(session, now, None)}
    assert "a" * 11 in ids and "b" * 11 in ids
    assert "c" * 11 not in ids and "d" * 11 not in ids


def test_classify_comment_state():
    assert comment_policy.classify_comment_state("ERROR: Comments are turned off.") == "comments_disabled"
    assert comment_policy.classify_comment_state("ERROR: Video unavailable") == "unavailable"
    assert comment_policy.classify_comment_state("WARNING: nothing important") is None


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_api_comments_list_stats_refresh(client):
    from app.db import session_scope

    with session_scope() as s:
        v = _video(s, upload_date="20260530")
        ingest_comments_from_info(s, v, _info([_c("c1", like=5), _c("c2", like=2)]))
        vid_pk = v.id

    lst = client.get(f"/api/videos/{vid_pk}/comments").json()
    assert len(lst) == 2 and all(e["raw_json"] is None for e in lst)
    raw = client.get(f"/api/videos/{vid_pk}/comments", params={"include_raw": True}).json()
    assert raw[0]["raw_json"] is not None

    stats = client.get(f"/api/videos/{vid_pk}/comments/stats").json()
    assert stats["total"] == 2 and stats["active"] == 2 and stats["missing"] == 0

    assert client.post("/api/comments/refresh", json={"target": "dQw4w9WgXcQ"}).json()["type"] == "comments_refresh"
    assert client.post(f"/api/videos/{vid_pk}/comments/refresh").status_code == 201
    assert client.post("/api/comments/refresh-all", json={"limit_videos": 5}).status_code == 200
    assert client.get(f"/api/videos/{vid_pk}/snapshots").status_code == 200


def test_api_comments_refresh_accepts_target_and_video_alias(client):
    # official field
    r1 = client.post("/api/comments/refresh", json={"target": "dQw4w9WgXcQ", "now": True})
    assert r1.status_code == 201 and r1.json()["type"] == "comments_refresh"
    # backward-compat alias
    r2 = client.post("/api/comments/refresh", json={"video": "dQw4w9WgXcQ", "now": True})
    assert r2.status_code == 201 and r2.json()["type"] == "comments_refresh"


def test_api_comments_refresh_both_fields_400(client):
    r = client.post(
        "/api/comments/refresh", json={"target": "dQw4w9WgXcQ", "video": "dQw4w9WgXcQ"}
    )
    assert r.status_code == 400


def test_api_comments_refresh_missing_target_400(client):
    assert client.post("/api/comments/refresh", json={}).status_code == 400


def test_api_comments_refresh_bad_video_400(client):
    assert client.post("/api/comments/refresh", json={"target": "https://example.com/x"}).status_code == 400


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_comments(settings):
    from app.db import session_scope

    with session_scope() as s:
        v = _video(s)
        ingest_comments_from_info(s, v, _info([_c("c1", like=5)]))

    assert runner.invoke(cli_app, ["comments", "list", "dQw4w9WgXcQ"]).exit_code == 0
    assert "total comments" in runner.invoke(cli_app, ["comments", "stats", "dQw4w9WgXcQ"]).stdout
    r = runner.invoke(cli_app, ["comments", "refresh", "dQw4w9WgXcQ"])
    assert r.exit_code == 0 and "comments_refresh job" in r.stdout
