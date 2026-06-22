"""Phase 7I tests: cookie/PO-token status (masked), preflight secret-safety,
safe metadata-run with rate-limit gating, and retryable-only retry.

Secrets (cookie file path, PO-token value) must NEVER appear in any API /
preflight output — only configured/readable booleans + masked timestamps.
Permanent failures (private/deleted/unavailable) are never retried.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import Job, LikedVideo, Video
from app.services import liked_archive as la
from app.services import preflight as pf


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _liked_stub(s, vid):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
              title="stub", channel_title="C", first_seen_at=datetime(2025, 1, 1))
    s.add(v)
    s.flush()
    s.add(LikedVideo(source="takeout_my_activity", youtube_video_id=vid, title="stub",
                     url=f"https://youtu.be/{vid}", liked_at=datetime(2025, 1, 1), video_id=v.id))
    s.flush()
    return v


def _failed_meta_job(s, vid, err, *, status="failed"):
    j = Job(type="download", status=status, url=f"https://youtu.be/{vid}",
            profile_name="metadata_only", error_message=err,
            meta={"source_action": la.SOURCE_ACTION, "youtube_video_id": vid})
    s.add(j)
    s.flush()
    return j


# --------------------------------------------------------------------------- #
# secrets-status (booleans/masked only)
# --------------------------------------------------------------------------- #
def test_secrets_status_booleans_only(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "youtube_po_token", "SUPER_SECRET_TOKEN_VALUE")
    monkeypatch.setattr(settings, "youtube_visitor_data", "SECRET_VISITOR_DATA")
    r = client.get("/api/system/secrets-status")
    body = r.text
    j = r.json()
    assert j["po_token_configured"] is True and j["visitor_data_configured"] is True
    assert j["secret_value_exposed"] is False
    # the actual secret values must NOT appear anywhere in the response
    assert "SUPER_SECRET_TOKEN_VALUE" not in body
    assert "SECRET_VISITOR_DATA" not in body


def test_secrets_status_no_cookies(client, settings):
    j = client.get("/api/system/secrets-status").json()
    assert j["cookies_configured"] is False and j["po_token_configured"] is False


def test_preflight_reports_cookie_status_without_values(settings, session, monkeypatch):
    monkeypatch.setattr(settings, "youtube_po_token", "TOKENXYZ")
    rep = pf.system_preflight(session, settings)
    names = {c["name"]: c for c in rep["checks"]}
    assert names["cookies_file"]["status"] == "warn"  # not configured
    assert names["po_token"]["status"] == "ok"        # configured
    assert names["secret_value_exposed"]["status"] == "ok"
    import json
    assert "TOKENXYZ" not in json.dumps(rep)


# --------------------------------------------------------------------------- #
# rate-limit decision
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rl,att,level", [
    (0, 10, "ok"), (4, 10, "ok"), (5, 10, "warn"), (7, 10, "warn"),
    (8, 10, "stop"), (10, 10, "stop"), (0, 0, "ok"),
])
def test_metadata_rate_decision(rl, att, level):
    d = la.metadata_rate_decision(rl, att, warn_ratio=0.5, stop_ratio=0.8)
    assert d["level"] == level


# --------------------------------------------------------------------------- #
# metadata-run
# --------------------------------------------------------------------------- #
def test_metadata_run_dry_run_plans_without_jobs(settings):
    from app.db import session_scope
    with session_scope() as s:
        for i in range(5):
            _liked_stub(s, f"vidplan{i:05d}")
        s.commit()
    res = la.metadata_run(get_settings(), target_limit=100, apply=False)
    assert res["ok"] is True and res["apply"] is False
    assert res["plan_selected"] == 5 and res["metadata_missing"] == 5
    with session_scope() as s:
        assert s.query(Job).count() == 0  # dry-run created no jobs


def test_metadata_run_apply_requires_worker(settings):
    # dead redis (test env) -> no worker heartbeat -> must NOT enqueue
    from app.db import session_scope
    with session_scope() as s:
        _liked_stub(s, "vidnowork01")
        s.commit()
    res = la.metadata_run(get_settings(), target_limit=10, apply=True)
    assert res["ok"] is False and "worker" in (res["message"] or "")
    with session_scope() as s:
        assert s.query(Job).count() == 0


# --------------------------------------------------------------------------- #
# retry-metadata: retryable only, permanent excluded
# --------------------------------------------------------------------------- #
def test_retry_metadata_excludes_permanent(settings, session):
    _failed_meta_job(session, "vidrate0001", "HTTP Error 429: Too Many Requests")  # retryable
    _failed_meta_job(session, "vidnet00001", "Unable to download webpage: getaddrinfo")  # retryable
    _failed_meta_job(session, "vidpriv0001", "Private video")        # permanent
    _failed_meta_job(session, "viddel00001", "This video has been removed by the uploader")  # permanent
    session.commit()
    # metadata_only retry, no submit (Redis dead)
    job_ids = la.retry_failed_liked(session, get_settings(), limit=50, submit=False, metadata_only=True)
    session.commit()
    retried = {session.get(Job, jid).url for jid in job_ids}
    assert any("vidrate0001" in u for u in retried)
    assert any("vidnet00001" in u for u in retried)
    # permanent NOT retried
    assert not any("vidpriv0001" in u for u in retried)
    assert not any("viddel00001" in u for u in retried)
    assert len(job_ids) == 2


def test_retry_metadata_reason_filter(settings, session):
    _failed_meta_job(session, "vidrate0002", "HTTP Error 429: Too Many Requests")
    _failed_meta_job(session, "vidnet00002", "connection reset by peer")
    session.commit()
    job_ids = la.retry_failed_liked(session, get_settings(), reason="rate_limited", limit=50,
                                    submit=False, metadata_only=True)
    assert len(job_ids) == 1
    assert "vidrate0002" in session.get(Job, job_ids[0]).url


def test_retry_metadata_skips_body_jobs(settings, session):
    # a body (non-metadata) retryable job must be skipped by metadata_only retry
    j = Job(type="download", status="failed", url="https://youtu.be/vidbody0001",
            profile_name="video_compressed_1080p", error_message="HTTP Error 429",
            meta={"source_action": la.SOURCE_ACTION})
    session.add(j)
    session.commit()
    job_ids = la.retry_failed_liked(session, get_settings(), limit=50, submit=False, metadata_only=True)
    assert job_ids == []  # body job not picked up by metadata-only retry
