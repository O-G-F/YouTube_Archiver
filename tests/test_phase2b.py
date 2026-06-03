"""Phase 2B tests: re-crawl policies, removed_at, unique constraint,
channel-tab resolution, scheduler, refresh API/CLI."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Collection, CollectionItem, Job
from app.services import expand
from app.services.scheduler import run_once
from app.services.urls import UrlError, normalize_url, resolve_channel_tabs

runner = CliRunner()

A = "dQw4w9WgXcQ"
B = "9bZkp7q19f0"
C = "kJQP7kiw5Fk"


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _entry(vid: str) -> dict:
    return {
        "id": vid,
        "title": "t",
        "url": f"https://www.youtube.com/watch?v={vid}",
        "_type": "url",
        "ie_key": "Youtube",
    }


def _process(session, url, entries, *, capped=False, detect_removed=True, info=None):
    return expand.process_entries(
        session,
        get_settings(),
        url=url,
        info=info or {"title": "PL"},
        entries=entries,
        capped=capped,
        profile_name="metadata_only",
        parent_job_id=None,
        detect_removed=detect_removed,
    )


# --------------------------------------------------------------------------- #
# Channel-tab resolution (req 3, 11)
# --------------------------------------------------------------------------- #
def test_resolve_channel_tabs_explicit():
    p = normalize_url("https://www.youtube.com/@ex")
    assert resolve_channel_tabs(p, True, True, False) == ["videos", "shorts"]
    assert resolve_channel_tabs(p, False, False, True) == ["streams"]


def test_resolve_channel_tabs_from_tab_url():
    p = normalize_url("https://www.youtube.com/@ex/shorts")
    assert resolve_channel_tabs(p, False, False, False) == ["shorts"]


def test_resolve_channel_tabs_root_no_flags_errors():
    p = normalize_url("https://www.youtube.com/@ex")
    with pytest.raises(UrlError):
        resolve_channel_tabs(p, False, False, False)


# --------------------------------------------------------------------------- #
# Policy-driven removed_at (req 5)
# --------------------------------------------------------------------------- #
def test_refresh_updates_last_seen(session):
    url = "https://www.youtube.com/playlist?list=PLseen"
    _process(session, url, [_entry(A)])
    item = session.scalars(
        select(CollectionItem).where(CollectionItem.youtube_video_id == A)
    ).one()
    first_seen = item.last_seen_at
    _process(session, url, [_entry(A)])
    assert item.last_seen_at >= first_seen
    assert item.removed_at is None


def test_refresh_marks_removed_when_not_capped(session):
    url = "https://www.youtube.com/playlist?list=PLrm"
    _process(session, url, [_entry(A), _entry(B)])
    r = _process(session, url, [_entry(A)], capped=False, detect_removed=True)
    item_b = session.scalars(
        select(CollectionItem).where(CollectionItem.youtube_video_id == B)
    ).one()
    assert item_b.removed_at is not None
    assert r.removed_count == 1


def test_new_only_policy_does_not_mark_removed(session):
    url = "https://www.youtube.com/playlist?list=PLnew"
    _process(session, url, [_entry(A), _entry(B)])
    r = _process(session, url, [_entry(A)], capped=False, detect_removed=False)
    item_b = session.scalars(
        select(CollectionItem).where(CollectionItem.youtube_video_id == B)
    ).one()
    assert item_b.removed_at is None
    assert r.removed_count == 0


def test_capped_does_not_mark_removed(session):
    url = "https://www.youtube.com/playlist?list=PLcap2"
    _process(session, url, [_entry(A), _entry(B)])
    _process(session, url, [_entry(A)], capped=True, detect_removed=True)
    item_b = session.scalars(
        select(CollectionItem).where(CollectionItem.youtube_video_id == B)
    ).one()
    assert item_b.removed_at is None


def test_rediscovery_clears_removed_at(session):
    url = "https://www.youtube.com/playlist?list=PLredisc"
    _process(session, url, [_entry(A), _entry(B)])
    _process(session, url, [_entry(A)], detect_removed=True)
    item_b = session.scalars(
        select(CollectionItem).where(CollectionItem.youtube_video_id == B)
    ).one()
    assert item_b.removed_at is not None
    # B reappears
    _process(session, url, [_entry(A), _entry(B)], detect_removed=True)
    assert item_b.removed_at is None


# --------------------------------------------------------------------------- #
# DB unique constraint (req 4)
# --------------------------------------------------------------------------- #
def test_collection_items_unique_constraint(session):
    c = Collection(type="playlist", url="https://www.youtube.com/playlist?list=PLuq")
    session.add(c)
    session.flush()
    session.add(CollectionItem(collection_id=c.id, youtube_video_id=A))
    session.flush()
    session.add(CollectionItem(collection_id=c.id, youtube_video_id=A))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# --------------------------------------------------------------------------- #
# Scheduler (req 1, 2)
# --------------------------------------------------------------------------- #
def test_scheduler_run_once_manual_creates_jobs(session):
    _process(session, "https://www.youtube.com/playlist?list=PLsched", [_entry(A)])
    session.commit()
    summary = run_once(get_settings(), reason="manual")
    assert summary["collections_checked"] >= 1
    assert summary["jobs_created"] >= 1
    with session_scope() as s:
        expand_jobs = s.scalars(select(Job).where(Job.type == "expand")).all()
        assert any((j.meta or {}).get("scheduled_by") == "manual" for j in expand_jobs)


def test_scheduler_disabled_is_noop(settings):
    settings.scheduler_enabled = False
    summary = run_once(settings, reason="scheduler")
    assert summary["enabled"] is False
    assert summary["jobs_created"] == 0


def test_scheduler_skips_manual_policy(session):
    _process(session, "https://www.youtube.com/playlist?list=PLman", [_entry(A)])
    coll = session.scalars(select(Collection)).first()
    coll.crawl_policy = "manual"
    session.commit()
    summary = run_once(get_settings(), reason="manual")
    assert summary["collections_checked"] == 0


# --------------------------------------------------------------------------- #
# API (req 9)
# --------------------------------------------------------------------------- #
def _make_collection(url="https://www.youtube.com/playlist?list=PLapi"):
    with session_scope() as s:
        _process(s, url, [_entry(A)])


def test_api_collection_refresh(client):
    _make_collection("https://www.youtube.com/playlist?list=PLref")
    cid = client.get("/api/collections").json()[0]["id"]
    r = client.post(f"/api/collections/{cid}/refresh")
    assert r.status_code == 201
    body = r.json()
    assert body["type"] == "expand"
    assert body["meta"]["scheduled_by"] == "manual_refresh"
    assert body["meta"]["detect_removed"] is True


def test_api_refresh_all(client):
    _make_collection("https://www.youtube.com/playlist?list=PLrefall")
    r = client.post("/api/collections/refresh-all")
    assert r.status_code == 200
    assert r.json()["jobs_created"] >= 1


def test_api_enable_disable_and_patch(client):
    _make_collection("https://www.youtube.com/playlist?list=PLed")
    cid = client.get("/api/collections").json()[0]["id"]
    assert client.post(f"/api/collections/{cid}/disable").json()["enabled"] is False
    assert client.post(f"/api/collections/{cid}/enable").json()["enabled"] is True
    r = client.patch(f"/api/collections/{cid}", json={"crawl_policy": "refresh"})
    assert r.status_code == 200 and r.json()["crawl_policy"] == "refresh"
    assert client.patch(
        f"/api/collections/{cid}", json={"crawl_policy": "bogus"}
    ).status_code == 400


def test_api_scheduler_status(client):
    body = client.get("/api/scheduler/status").json()
    assert "enabled" in body and "interval_seconds" in body


# --------------------------------------------------------------------------- #
# CLI (req 3, 8)
# --------------------------------------------------------------------------- #
def test_cli_add_channel_root_no_flags_errors(settings):
    result = runner.invoke(
        cli_app,
        ["source", "add-channel", "https://www.youtube.com/@ex", "--profile", "metadata_only"],
    )
    assert result.exit_code != 0


def test_cli_add_channel_videos_creates_expand(settings):
    result = runner.invoke(
        cli_app,
        ["source", "add-channel", "https://www.youtube.com/@ex", "--videos",
         "--profile", "metadata_only"],
    )
    assert result.exit_code == 0
    with session_scope() as s:
        jobs_ = s.scalars(select(Job).where(Job.type == "expand")).all()
        assert any(j.url.endswith("/videos") for j in jobs_)


def test_cli_scheduler_run_once(settings):
    result = runner.invoke(cli_app, ["scheduler", "run-once"])
    assert result.exit_code == 0
    assert "collections_checked=" in result.stdout


def test_cli_collections_set_policy_and_disable(settings):
    with session_scope() as s:
        _process(s, "https://www.youtube.com/playlist?list=PLpol", [_entry(A)])
        cid = s.scalars(select(Collection)).first().id
    assert runner.invoke(cli_app, ["collections", "set-policy", str(cid), "refresh"]).exit_code == 0
    assert runner.invoke(cli_app, ["collections", "disable", str(cid)]).exit_code == 0
    with session_scope() as s:
        c = s.get(Collection, cid)
        assert c.crawl_policy == "refresh"
        assert c.enabled is False
