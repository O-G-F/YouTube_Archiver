"""Phase 7C tests: liked-videos bulk archive + throttling-aware queue.

Covers plan/dry-run, enqueue metadata (missing-only, no body), enqueue archive
(missing-body-only, body profile), dedup of queued/running jobs, has_body /
has_metadata state, liked job.meta tagging, retryable liked extraction +
retry-failed (cap/reason), and the metadata_only no-body invariant.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.db import session_scope
from app.main import app
from app.models import Job, LikedVideo, MediaFile, Video
from app.services import jobs as jobs_svc
from app.services import liked_archive as la


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _video(s, vid, *, title=None, body=False, meta_file=False):
    v = Video(
        youtube_video_id=vid,
        url=f"https://www.youtube.com/watch?v={vid}",
        title=title,
        channel_title="Chan",
        first_seen_at=datetime(2025, 1, 1),
    )
    s.add(v)
    s.flush()
    if body:
        s.add(MediaFile(video_id=v.id, media_type="video", path=f"{vid}.mp4", profile="video_compressed_1080p"))
    if meta_file:
        s.add(MediaFile(video_id=v.id, media_type="info_json", path=f"{vid}.info.json"))
    s.flush()
    return v


def _liked(s, vid, *, source="takeout_my_activity", title=None, video=None, liked_at=None):
    lv = LikedVideo(
        source=source,
        youtube_video_id=vid,
        title=title,
        channel_title="Chan",
        url=f"https://youtu.be/{vid}",
        liked_at=liked_at or datetime(2025, 1, 1),
        video_id=video.id if video else None,
    )
    s.add(lv)
    s.flush()
    return lv


def _seed(s):
    # A: metadata + body ; B: metadata only ; C: nothing (no Video)
    va = _video(s, "aaaAAA11111", title="HasBody", body=True, meta_file=True)
    vb = _video(s, "bbbBBB22222", title="MetaOnly", meta_file=True)
    _liked(s, "aaaAAA11111", video=va, liked_at=datetime(2025, 1, 1))
    _liked(s, "bbbBBB22222", video=vb, liked_at=datetime(2025, 1, 2))
    _liked(s, "cccCCC33333", source="youtube_data_api", title="Nothing", liked_at=datetime(2025, 1, 3))
    s.commit()


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def test_video_state_distinguishes_body_and_metadata(settings, session):
    _seed(session)
    a = session.query(Video).filter_by(youtube_video_id="aaaAAA11111").one()
    b = session.query(Video).filter_by(youtube_video_id="bbbBBB22222").one()
    sa = la.video_state(session, a)
    sb = la.video_state(session, b)
    assert sa["has_body"] is True and sa["body_media_count"] == 1 and sa["has_metadata"] is True
    assert sb["has_body"] is False and sb["body_media_count"] == 0 and sb["has_metadata"] is True
    assert la.video_state(session, None)["has_body"] is False


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_archive_plan_counts(settings, session):
    _seed(session)
    plan = la.archive_plan(session, settings, filters=la.LikedFilters())
    assert plan.total_candidates == 3
    assert plan.missing_metadata == 1  # only C
    assert plan.missing_body == 2  # B + C
    assert plan.has_body == 1  # A
    assert plan.recommended_profile == settings.liked_archive_default_profile
    assert plan.recommended_delay_seconds > 0


# --------------------------------------------------------------------------- #
# enqueue metadata (missing-only, never body)
# --------------------------------------------------------------------------- #
def test_enqueue_metadata_missing_only(settings, session):
    _seed(session)
    r = la.enqueue_metadata(
        session, settings, filters=la.LikedFilters(missing_metadata=True), limit=10, submit=False
    )
    assert r.selected_count == 1  # only C
    assert r.jobs_created == 1
    assert r.downloads_body is False
    job = session.get(Job, r.job_ids[0])
    assert job.profile_name == "metadata_only"
    assert job.meta["source_action"] == "liked_archive"
    assert job.meta["requested_profile"] == "metadata_only"
    assert job.video_id is not None


def test_metadata_enqueue_creates_no_body(settings, session):
    _seed(session)
    la.enqueue_metadata(session, settings, filters=la.LikedFilters(missing_metadata=True), limit=10, submit=False)
    body = session.query(MediaFile).filter(MediaFile.media_type.in_(("video", "audio"))).count()
    assert body == 1  # only the pre-seeded A body; enqueue added none


# --------------------------------------------------------------------------- #
# enqueue archive (missing-body-only, body profile)
# --------------------------------------------------------------------------- #
def test_enqueue_archive_missing_body_only(settings, session):
    _seed(session)
    r = la.enqueue_archive(
        session, settings, filters=la.LikedFilters(missing_body=True), limit=10,
        profile="video_compressed_1080p", submit=False,
    )
    assert r.selected_count == 2  # B + C (A has a body -> skipped)
    assert r.skipped_already_has_body == 1
    assert r.downloads_body is True
    for jid in r.job_ids:
        job = session.get(Job, jid)
        assert job.profile_name == "video_compressed_1080p"
        assert job.type == "download"
        assert job.meta["source_action"] == "liked_archive"


def test_enqueue_archive_dry_run_creates_nothing(settings, session):
    _seed(session)
    before = session.query(Job).count()
    r = la.enqueue_archive(
        session, settings, filters=la.LikedFilters(missing_body=True), limit=10, dry_run=True, submit=False
    )
    assert r.selected_count == 2 and r.jobs_created == 0 and r.dry_run is True
    assert session.query(Job).count() == before


# --------------------------------------------------------------------------- #
# dedup
# --------------------------------------------------------------------------- #
def test_dedup_skips_active_jobs(settings, session):
    _seed(session)
    r1 = la.enqueue_archive(
        session, settings, filters=la.LikedFilters(missing_body=True), limit=10,
        profile="video_compressed_1080p", submit=False,
    )
    assert r1.jobs_created == 2
    # second run: both already have an active queued job -> all skipped
    r2 = la.enqueue_archive(
        session, settings, filters=la.LikedFilters(missing_body=True), limit=10,
        profile="video_compressed_1080p", submit=False,
    )
    assert r2.jobs_created == 0
    assert r2.skipped_existing_job == 2


def test_dedup_is_per_profile(settings, session):
    _seed(session)
    # a metadata_only job for C should NOT block a video archive job for C
    la.enqueue_metadata(session, settings, filters=la.LikedFilters(missing_metadata=True), limit=10, submit=False)
    r = la.enqueue_archive(
        session, settings, filters=la.LikedFilters(missing_body=True), limit=10,
        profile="video_compressed_1080p", submit=False,
    )
    # C still archivable (different profile), B archivable -> 2
    assert r.jobs_created == 2


# --------------------------------------------------------------------------- #
# retryable (liked-tagged only)
# --------------------------------------------------------------------------- #
def test_retryable_liked_only_and_retry(settings, session):
    _seed(session)
    r = la.enqueue_archive(
        session, settings, filters=la.LikedFilters(missing_body=True), limit=10,
        profile="video_compressed_1080p", submit=False,
    )
    # make one liked job a retryable failure (429), and one NON-liked failure.
    # mark_failed sets error_message (classify_job re-derives from it), like the worker.
    _429 = "ERROR: HTTP Error 429: Too Many Requests"
    liked_job = session.get(Job, r.job_ids[0])
    jobs_svc.mark_failed(session, liked_job, _429)
    jobs_svc.apply_classification(session, liked_job, settings, _429)
    other = Job(type="download", status="queued", url="https://www.youtube.com/watch?v=zzzZZZ99999",
                profile_name="video_compressed_1080p")
    session.add(other)
    session.flush()
    jobs_svc.mark_failed(session, other, _429)
    jobs_svc.apply_classification(session, other, settings, _429)
    session.commit()

    rows = la.retryable_liked(session, settings, reason=None, limit=50)
    ids = {j.id for j, _c in rows}
    assert liked_job.id in ids
    assert other.id not in ids  # non-liked job excluded
    # reason filter
    assert all("rate_limited" in c["reasons"] for _j, c in la.retryable_liked(session, settings, reason="rate_limited"))

    # retry-failed re-queues the liked job (increments retry_count)
    before = liked_job.retry_count or 0
    out = la.retry_failed_liked(session, settings, reason="rate_limited", limit=10)
    assert liked_job.id in out
    session.refresh(liked_job)
    assert liked_job.status == "queued"
    assert (liked_job.retry_count or 0) == before + 1


def test_retryable_respects_attempt_cap(settings, session):
    _seed(session)
    r = la.enqueue_archive(session, settings, filters=la.LikedFilters(missing_body=True), limit=1,
                           profile="video_compressed_1080p", submit=False)
    j = session.get(Job, r.job_ids[0])
    jobs_svc.mark_failed(session, j, "ERROR: HTTP Error 429: Too Many Requests")
    j.retry_count = settings.download_retry_max_attempts  # at cap
    jobs_svc.apply_classification(session, j, settings, "ERROR: HTTP Error 429: Too Many Requests")
    session.commit()
    assert la.retryable_liked(session, settings) == []


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_api_plan_and_enqueue(client):
    with session_scope() as s:
        _seed(s)
    plan = client.post("/api/liked-videos/archive-plan", json={}).json()
    assert plan["total_candidates"] == 3 and plan["missing_body"] == 2

    # dry-run archive creates nothing
    r = client.post("/api/liked-videos/enqueue-archive", json={"missing_body": True, "dry_run": True, "limit": 5}).json()
    assert r["selected_count"] == 2 and r["jobs_created"] == 0 and r["downloads_body"] is True

    # real archive (limit 1) creates 1 download job with the body profile
    r = client.post("/api/liked-videos/enqueue-archive", json={"missing_body": True, "limit": 1, "profile": "video_compressed_1080p"}).json()
    assert r["jobs_created"] == 1
    with session_scope() as s:
        job = s.get(Job, r["job_ids"][0])
        assert job.type == "download" and job.profile_name == "video_compressed_1080p"
        assert job.meta["source_action"] == "liked_archive"


def test_api_list_exposes_state(client):
    with session_scope() as s:
        _seed(s)
    rows = {r["youtube_video_id"]: r for r in client.get("/api/liked-videos").json()}
    assert rows["aaaAAA11111"]["has_body"] is True and rows["aaaAAA11111"]["body_media_count"] == 1
    assert rows["bbbBBB22222"]["has_body"] is False and rows["bbbBBB22222"]["has_metadata"] is True
    # filter only_missing_body
    miss = client.get("/api/liked-videos?only_missing_body=true").json()
    assert all(r["has_body"] is False for r in miss)
    assert "aaaAAA11111" not in {r["youtube_video_id"] for r in miss}


def test_api_unknown_profile_400(client):
    with session_scope() as s:
        _seed(s)
    r = client.post("/api/liked-videos/enqueue-archive", json={"profile": "does_not_exist", "limit": 1})
    assert r.status_code == 400


def test_api_enqueue_metadata_backward_compatible(client):
    with session_scope() as s:
        _seed(s)
    r = client.post("/api/liked-videos/enqueue-metadata", json={"only_missing_metadata": True, "limit": 5}).json()
    assert r["jobs_created"] == 1  # only C missing metadata
    assert "videos_selected" in r  # old schema preserved


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_plan_archive(settings):
    with session_scope() as s:
        _seed(s)
    res = CliRunner().invoke(cli_app, ["liked-videos", "plan-archive"])
    assert res.exit_code == 0, res.output
    assert "liked archive plan" in res.output
    assert "missing body" in res.output


def test_cli_enqueue_archive_dry_run(settings):
    with session_scope() as s:
        _seed(s)
    res = CliRunner().invoke(cli_app, ["liked-videos", "enqueue-archive", "--limit", "2", "--missing-body-only", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "VIDEO BODY DOWNLOAD" in res.output
    assert "dry-run" in res.output.lower()
    with session_scope() as s:
        assert s.query(Job).filter(Job.profile_name == "video_compressed_1080p").count() == 0


def test_cli_enqueue_metadata_no_body(settings):
    with session_scope() as s:
        _seed(s)
    res = CliRunner().invoke(cli_app, ["liked-videos", "enqueue-metadata", "--limit", "3", "--missing-only"])
    assert res.exit_code == 0, res.output
    assert "body NOT downloaded" in res.output
    with session_scope() as s:
        assert s.query(Job).filter(Job.profile_name == "metadata_only").count() >= 1
        assert s.query(MediaFile).filter(MediaFile.media_type.in_(("video", "audio"))).count() == 1
