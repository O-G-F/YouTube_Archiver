"""Phase 2A tests: expand diff logic, dedup, skip-existing, max_items, API/CLI."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Collection, CollectionItem, Job
from app.services import expand
from app.services import jobs as jobs_svc

runner = CliRunner()

A = "dQw4w9WgXcQ"
B = "9bZkp7q19f0"
C = "kJQP7kiw5Fk"


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _entry(vid: str, title: str = "t") -> dict:
    return {
        "id": vid,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "_type": "url",
        "ie_key": "Youtube",
    }


def _process(session, url, entries, *, capped=False, info=None, profile="metadata_only"):
    return expand.process_entries(
        session,
        get_settings(),
        url=url,
        info=info or {"title": "PL"},
        entries=entries,
        capped=capped,
        profile_name=profile,
        parent_job_id=None,
    )


# --------------------------------------------------------------------------- #
# Core diff logic
# --------------------------------------------------------------------------- #
def test_expand_creates_collection_items_and_jobs(session):
    url = "https://www.youtube.com/playlist?list=PLcreate"
    result = _process(session, url, [_entry(A), _entry(B)], info={"title": "My PL"})
    assert result.discovered_count == 2
    assert result.created_jobs_count == 2
    assert result.skipped_existing_count == 0

    c = session.get(Collection, result.collection_id)
    assert c.type == "playlist"
    assert c.title == "My PL"
    assert c.youtube_playlist_id == "PLcreate"

    items = session.scalars(
        select(CollectionItem).where(CollectionItem.collection_id == c.id)
    ).all()
    assert {i.youtube_video_id for i in items} == {A, B}

    jobs_ = session.scalars(select(Job).where(Job.type == "download")).all()
    assert len(jobs_) == 2
    assert all(j.collection_id == c.id for j in jobs_)
    assert {j.url for j in jobs_} == {
        f"https://www.youtube.com/watch?v={A}",
        f"https://www.youtube.com/watch?v={B}",
    }


def test_expand_dedups_within_run_and_skips_invalid(session):
    url = "https://www.youtube.com/playlist?list=PLdup"
    result = _process(session, url, [_entry(A), _entry(A), _entry("too-short"), {}])
    assert result.discovered_count == 1
    assert result.created_jobs_count == 1
    items = session.scalars(
        select(CollectionItem).where(
            CollectionItem.collection_id == result.collection_id
        )
    ).all()
    assert len(items) == 1


def test_expand_skips_existing_download_job(session):
    # a video that already has an active (queued) download job
    jobs_svc.create_download_child_job(session, A, "metadata_only")
    url = "https://www.youtube.com/playlist?list=PLskip"
    result = _process(session, url, [_entry(A), _entry(B)])
    assert result.discovered_count == 2
    assert result.created_jobs_count == 1  # only B
    assert result.skipped_existing_count == 1
    # still recorded as collection items (both)
    items = session.scalars(
        select(CollectionItem).where(
            CollectionItem.collection_id == result.collection_id
        )
    ).all()
    assert {i.youtube_video_id for i in items} == {A, B}


def test_expand_rerun_diff_updates_and_marks_removed(session):
    url = "https://www.youtube.com/playlist?list=PLdiff"
    r1 = _process(session, url, [_entry(A), _entry(B)])
    assert r1.created_jobs_count == 2

    # run 2: A stays, C is new, B disappeared (full listing -> not capped)
    r2 = _process(session, url, [_entry(A), _entry(C)])
    assert r2.discovered_count == 2
    assert r2.created_jobs_count == 1  # A already has a job -> only C
    assert r2.skipped_existing_count == 1
    assert r2.removed_count == 1

    items = {
        i.youtube_video_id: i
        for i in session.scalars(
            select(CollectionItem).where(
                CollectionItem.collection_id == r2.collection_id
            )
        )
    }
    assert set(items) == {A, B, C}  # no duplicates
    assert items[B].removed_at is not None
    assert items[A].removed_at is None
    assert items[C].removed_at is None
    # same collection reused across runs
    assert r1.collection_id == r2.collection_id


def test_expand_capped_does_not_mark_removed(session):
    url = "https://www.youtube.com/playlist?list=PLcap"
    _process(session, url, [_entry(A), _entry(B)], capped=False)
    # rerun sees only A, but capped -> B must NOT be marked removed
    r = _process(session, url, [_entry(A)], capped=True)
    assert r.capped is True
    items = {
        i.youtube_video_id: i
        for i in session.scalars(
            select(CollectionItem).where(
                CollectionItem.collection_id == r.collection_id
            )
        )
    }
    assert items[B].removed_at is None


def test_create_job_for_url_stores_max_items(session):
    job = jobs_svc.create_job_for_url(
        session, "https://www.youtube.com/playlist?list=PLm", "metadata_only", max_items=7
    )
    assert job.type == "expand"
    assert job.meta == {"max_items": 7}


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_api_archive_expand(client):
    r = client.post(
        "/api/archive/expand",
        json={"url": "https://www.youtube.com/playlist?list=PLapi", "profile": "metadata_only"},
    )
    assert r.status_code == 201
    assert r.json()["type"] == "expand"


def test_api_sources_playlist_with_max_items(client):
    r = client.post(
        "/api/sources/playlist",
        json={
            "url": "https://www.youtube.com/playlist?list=PLapi2",
            "profile": "metadata_only",
            "max_items": 10,
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["type"] == "expand"
    assert body["meta"]["max_items"] == 10


def test_api_sources_playlist_rejects_non_playlist(client):
    r = client.post("/api/sources/playlist", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 400


def test_api_sources_channel_multi_tab(client):
    r = client.post(
        "/api/sources/channel",
        json={"url": "https://www.youtube.com/@ex", "videos": True, "shorts": True,
              "profile": "metadata_only"},
    )
    assert r.status_code == 201
    jobs_ = r.json()
    assert len(jobs_) == 2
    urls = {j["url"] for j in jobs_}
    assert any(u.endswith("/videos") for u in urls)
    assert any(u.endswith("/shorts") for u in urls)


def test_api_collections_endpoints(client):
    with session_scope() as s:
        _process(s, "https://www.youtube.com/playlist?list=PLcoll", [_entry(A), _entry(B)],
                 info={"title": "Coll PL"})
    listing = client.get("/api/collections").json()
    assert len(listing) >= 1
    target = next(c for c in listing if c["url"].endswith("PLcoll"))
    assert target["item_count"] == 2
    cid = target["id"]
    assert client.get(f"/api/collections/{cid}").status_code == 200
    items = client.get(f"/api/collections/{cid}/items").json()
    assert len(items) == 2
    assert client.get("/api/collections/999999").status_code == 404


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_source_expand_creates_job(settings):
    result = runner.invoke(
        cli_app,
        ["source", "expand", "https://www.youtube.com/playlist?list=PLcli", "--profile", "metadata_only"],
    )
    assert result.exit_code == 0
    with session_scope() as s:
        jobs_ = s.scalars(select(Job).where(Job.type == "expand")).all()
        assert any(j.url.endswith("PLcli") for j in jobs_)


def test_cli_collections_list_and_items(settings):
    with session_scope() as s:
        _process(s, "https://www.youtube.com/playlist?list=PLcli2", [_entry(A)],
                 info={"title": "CLI PL"})
    res_list = runner.invoke(cli_app, ["collections", "list"])
    assert res_list.exit_code == 0
    assert "CLI PL" in res_list.stdout or "PLcli2" in res_list.stdout
