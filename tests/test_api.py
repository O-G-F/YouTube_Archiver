"""Web API tests via FastAPI TestClient (SQLite, no Redis)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["database"] is True
    assert body["redis"] is False  # no Redis in tests
    assert body["status"] == "ok"


def test_profiles_listed(client):
    resp = client.get("/api/profiles")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert "video_best_archive" in names
    assert "video_best_archive_all_subs" in names
    assert len(names) == 8


def test_archive_url_creates_queued_job(client):
    resp = client.post(
        "/api/archive/url",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "profile": "video_best_archive"},
    )
    assert resp.status_code == 201
    job = resp.json()
    assert job["type"] == "download"
    assert job["status"] == "queued"  # Redis down -> stays queued, no crash
    assert job["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    # visible in listing + detail
    listing = client.get("/api/jobs").json()
    assert any(j["id"] == job["id"] for j in listing)
    assert client.get(f"/api/jobs/{job['id']}").status_code == 200


def test_archive_invalid_url_400(client):
    resp = client.post("/api/archive/url", json={"url": "https://example.com/x"})
    assert resp.status_code == 400


def test_archive_unknown_profile_400(client):
    resp = client.post(
        "/api/archive/url",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "profile": "nope"},
    )
    assert resp.status_code == 400


def test_archive_batch_mixed(client):
    resp = client.post(
        "/api/archive/batch",
        json={
            "urls": ["https://youtu.be/dQw4w9WgXcQ", "https://example.com/bad"],
            "profile": "metadata_only",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 1
    assert body["failed"] == 1
