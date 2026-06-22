"""Phase 7H tests: production archive operation on imported liked videos.

Focus on the NEW pieces: granular failure classification (private / deleted /
unavailable / network / rate_limited / unknown) and the per-reason failure
breakdown for liked-archive jobs. Plus dedup-skip on re-enqueue. Failed videos
are recorded with a reason — never deleted.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import Job, LikedVideo, MediaFile, Video
from app.services import job_classify as jc
from app.services import liked_archive as la


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _video(s, vid, *, title=None, body=False, meta_file=False):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
              title=title, channel_title="Chan", first_seen_at=datetime(2025, 1, 1))
    s.add(v)
    s.flush()
    if body:
        s.add(MediaFile(video_id=v.id, media_type="video", path=f"{vid}.mp4", profile="video_compressed_1080p"))
    if meta_file:
        s.add(MediaFile(video_id=v.id, media_type="info_json", path=f"{vid}.info.json"))
    s.flush()
    return v


def _liked(s, vid, *, source="takeout_my_activity", title=None, video=None):
    lv = LikedVideo(source=source, youtube_video_id=vid, title=title, channel_title="Chan",
                    url=f"https://youtu.be/{vid}", liked_at=datetime(2025, 1, 1),
                    video_id=video.id if video else None)
    s.add(lv)
    s.flush()
    return lv


def _failed_liked_job(s, vid, error_text, *, status="failed", profile="metadata_only"):
    j = Job(type="download", status=status, url=f"https://youtu.be/{vid}",
            profile_name=profile, error_message=error_text,
            meta={"source_action": la.SOURCE_ACTION, "youtube_video_id": vid})
    s.add(j)
    s.flush()
    return j


# --------------------------------------------------------------------------- #
# classification taxonomy (private/deleted/unavailable/network/rate_limited/unknown)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected,retryable", [
    ("ERROR: xxx: Private video. Sign in if you've been granted access", "private", False),
    ("ERROR: Video unavailable. This video has been removed by the uploader", "deleted", False),
    ("ERROR: Video unavailable. This video is not available", "unavailable", False),
    ("ERROR: This video is not available in your country", "unavailable", False),
    ("ERROR: Unable to download webpage: Temporary failure in name resolution", "network", True),
    ("ERROR: HTTP Error 503: Service Unavailable", "network", True),
    ("ERROR: HTTP Error 429: Too Many Requests", "rate_limited", True),
    ("ERROR: some unrecognized failure", "unknown", False),
])
def test_failure_classification(text, expected, retryable):
    c = jc.classify_text("failed", text, {})
    assert c["primary_reason"] == expected
    assert c["retryable"] is retryable
    assert c["permanent"] is (expected in {"private", "deleted", "unavailable"})


def test_primary_reason_prefers_deleted_over_unavailable():
    # a deleted video error text matches BOTH markers; deleted wins
    c = jc.classify_text("failed", "Video unavailable. This video has been removed by the uploader", {})
    assert "unavailable" in c["reasons"] and "deleted" in c["reasons"]
    assert c["primary_reason"] == "deleted"


def test_success_has_no_primary_reason():
    c = jc.classify_text("success", "", {})
    assert c["primary_reason"] is None and c["permanent"] is False


# --------------------------------------------------------------------------- #
# failure breakdown
# --------------------------------------------------------------------------- #
def test_failure_breakdown_counts_by_reason(settings, session):
    _failed_liked_job(session, "vidprivate1", "Private video")
    _failed_liked_job(session, "viddeleted1", "This video has been removed by the uploader")
    _failed_liked_job(session, "vidunavail1", "Video unavailable")
    _failed_liked_job(session, "vidnetwork1", "Unable to download webpage: getaddrinfo failed")
    _failed_liked_job(session, "vidweird01", "totally unexpected")
    _failed_liked_job(session, "vidpartial", "Some fragments failed", status="partial_success")
    # a NON-liked failed job must NOT be counted
    session.add(Job(type="download", status="failed", url="x", error_message="Private video", meta={}))
    session.commit()

    fb = la.failure_breakdown(session)
    assert fb["total_failed"] == 5 and fb["total_partial"] == 1
    assert fb["by_reason"]["private"] == 1
    assert fb["by_reason"]["deleted"] == 1
    assert fb["by_reason"]["unavailable"] == 1
    assert fb["by_reason"]["network"] == 1
    assert fb["by_reason"]["unknown"] == 1
    assert fb["permanent"] == 3  # private + deleted + unavailable
    assert fb["retryable"] >= 2  # network + partial (fragments)


def test_failure_breakdown_api(client, settings):
    from app.db import session_scope
    with session_scope() as s:
        _failed_liked_job(s, "apiPriv0001", "Private video")
        _failed_liked_job(s, "apiNet00001", "connection reset by peer")
        s.commit()
    r = client.get("/api/liked-videos/failure-breakdown").json()
    assert r["total_failed"] == 2
    assert r["by_reason"]["private"] == 1 and r["by_reason"]["network"] == 1
    assert r["permanent"] == 1


def test_failure_breakdown_api_no_leak(client, settings):
    from app.db import session_scope
    with session_scope() as s:
        # error text contains a URL / path-like string; the breakdown returns
        # COUNTS only, never the raw error text.
        _failed_liked_job(s, "leaky00001", "Private video https://youtube.com/watch?v=secret /Users/x")
        s.commit()
    blob = client.get("/api/liked-videos/failure-breakdown").text
    for needle in ("/Users/", "youtube.com/watch", "titleUrl", "raw_json", "cookie", "token"):
        assert needle not in blob


# --------------------------------------------------------------------------- #
# enqueue dedup / skip (existing behavior, verified for 7H runbook)
# --------------------------------------------------------------------------- #
def test_enqueue_metadata_skips_videos_with_metadata(settings, session):
    vb = _video(session, "metapresent", title="HasMeta", meta_file=True)
    _liked(session, "metapresent", video=vb)               # has metadata -> skipped
    _liked(session, "nometa00001", title="NoMeta")          # no Video -> selected
    session.commit()
    r = la.enqueue_metadata(session, get_settings(),
                            filters=la.LikedFilters(missing_metadata=True), limit=50,
                            dry_run=True, submit=False)
    # only the missing-metadata one is a candidate
    assert r.selected_count == 1


def test_enqueue_archive_skips_videos_with_body(settings, session):
    va = _video(session, "hasbody0001", title="HasBody", body=True)
    _liked(session, "hasbody0001", video=va)                # has body -> skipped
    vb = _video(session, "nobody00001", title="NoBody")
    _liked(session, "nobody00001", video=vb)                # no body -> selected
    session.commit()
    r = la.enqueue_archive(session, get_settings(),
                           filters=la.LikedFilters(missing_body=True), limit=50,
                           profile="video_compressed_1080p", dry_run=True, submit=False)
    assert r.selected_count == 1


def test_enqueue_metadata_never_downloads_body(settings, session):
    _liked(session, "nometabody1", title="X")
    session.commit()
    r = la.enqueue_metadata(session, get_settings(),
                            filters=la.LikedFilters(missing_metadata=True), limit=10,
                            dry_run=False, submit=False)
    assert r.jobs_created == 1
    job = session.query(Job).filter(Job.type == "download").one()
    assert job.profile_name == "metadata_only"  # body NOT downloaded
