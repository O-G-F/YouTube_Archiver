"""Phase 7J: metadata selection excludes permanent failures (private/deleted/
unavailable) so they aren't re-enqueued every batch. retryable / non-permanent
(rate_limited / network / impersonation / unknown) stay eligible. Permanent rows
are kept in the DB, never deleted. Body-archive selection is unaffected.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import Job, LikedVideo, MediaFile, Video
from app.services import liked_archive as la


@pytest.fixture()
def client(settings):
    with TestClient(app) as c:
        yield c


def _video(s, vid, *, title="stub", body=False):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
              title=title, channel_title="C", first_seen_at=datetime(2025, 1, 1))
    s.add(v); s.flush()
    if body:
        s.add(MediaFile(video_id=v.id, media_type="video", path=f"{vid}.mp4", profile="video_compressed_1080p"))
    s.flush()
    return v


def _liked(s, vid, video):
    s.add(LikedVideo(source="takeout_my_activity", youtube_video_id=vid, title="t",
                     url=f"https://youtu.be/{vid}", liked_at=datetime(2025, 1, 1), video_id=video.id))
    s.flush()


def _mjob(s, video, err, *, status="failed", profile="metadata_only"):
    j = Job(type="download", status=status, url=video.url, video_id=video.id,
            profile_name=profile, error_message=err, meta={"source_action": la.SOURCE_ACTION})
    s.add(j); s.flush()
    return j


def _setup(s):
    # permanent set
    vp = _video(s, "vidprivate1"); _liked(s, "vidprivate1", vp); _mjob(s, vp, "Private video")
    vd = _video(s, "viddeleted1"); _liked(s, "viddeleted1", vd); _mjob(s, vd, "This video has been removed by the uploader")
    vu = _video(s, "vidunavail1"); _liked(s, "vidunavail1", vu); _mjob(s, vu, "Video unavailable")
    # retryable / non-permanent (stay eligible)
    vr = _video(s, "vidrate0001"); _liked(s, "vidrate0001", vr); _mjob(s, vr, "HTTP Error 429: Too Many Requests")
    vn = _video(s, "vidnetwork1"); _liked(s, "vidnetwork1", vn); _mjob(s, vn, "Unable to download webpage: getaddrinfo")
    vk = _video(s, "vidunknown1"); _liked(s, "vidunknown1", vk); _mjob(s, vk, "some weird error")
    # fresh (never attempted)
    vf = _video(s, "vidfresh001"); _liked(s, "vidfresh001", vf)
    s.commit()
    return dict(vp=vp.id, vd=vd.id, vu=vu.id, vr=vr.id, vn=vn.id, vk=vk.id, vf=vf.id)


def test_permanent_set_only_private_deleted_unavailable(settings, session):
    ids = _setup(session)
    perm = la.permanent_metadata_video_ids(session)
    assert perm == {ids["vp"], ids["vd"], ids["vu"]}


def test_metadata_enqueue_excludes_permanent_by_default(settings, session):
    _setup(session)
    r = la.enqueue_metadata(session, get_settings(), filters=la.LikedFilters(missing_metadata=True),
                            limit=50, dry_run=True, submit=False)
    # 7 total missing - 3 permanent = 4 eligible (rate/network/unknown/fresh)
    assert r.selected_count == 4
    assert r.skipped_permanent == 3


def test_include_permanent_selects_all(settings, session):
    _setup(session)
    r = la.enqueue_metadata(session, get_settings(), filters=la.LikedFilters(missing_metadata=True),
                            limit=50, dry_run=True, submit=False, include_permanent=True)
    assert r.selected_count == 7 and r.skipped_permanent == 0


def test_retryable_and_unknown_stay_eligible(settings, session):
    """rate_limited / network / unknown are NOT permanent -> still selected."""
    ids = _setup(session)
    perm = la.permanent_metadata_video_ids(session)
    for key in ("vr", "vn", "vk", "vf"):
        assert ids[key] not in perm


def test_latest_attempt_wins_recovered_video_not_permanent(settings, session):
    # private THEN a later success -> latest is success -> NOT permanent
    v = _video(session, "vidrecov001"); _liked(session, "vidrecov001", v)
    _mjob(session, v, "Private video", status="failed")
    _mjob(session, v, "", status="success")  # newer job id -> latest
    session.commit()
    assert v.id not in la.permanent_metadata_video_ids(session)


def test_body_archive_excludes_permanent_by_default(settings, session):
    # Phase 8A: body archive now ALSO excludes permanent (private/deleted/unavailable)
    # by default — they can't be downloaded and are kept, never deleted.
    _setup(session)
    r = la.enqueue_archive(session, get_settings(), filters=la.LikedFilters(missing_body=True),
                           limit=50, profile="video_compressed_1080p", dry_run=True, submit=False)
    assert r.skipped_permanent == 3      # private/deleted/unavailable excluded
    assert r.selected_count == 4         # 3 retryable + 1 fresh
    # --include-permanent (exclude_permanent=False) restores the old behavior.
    r2 = la.enqueue_archive(session, get_settings(), filters=la.LikedFilters(missing_body=True),
                            limit=50, profile="video_compressed_1080p", dry_run=True,
                            submit=False, exclude_permanent=False)
    assert r2.skipped_permanent == 0
    assert r2.selected_count == 7


def test_progress_eligible_and_permanent(settings, session):
    _setup(session)
    p = la.progress(session, get_settings())
    assert p["metadata_missing"] == 7
    assert p["permanent_unique_videos"] == 3
    assert p["skipped_permanent_metadata"] == 3
    assert p["eligible_metadata_missing"] == 4


def test_failure_breakdown_unique_vs_attempts(settings, session):
    v = _video(session, "vidmulti001"); _liked(session, "vidmulti001", v)
    _mjob(session, v, "HTTP Error 429: Too Many Requests")  # attempt 1
    _mjob(session, v, "HTTP Error 429: Too Many Requests")  # attempt 2 (same video)
    session.commit()
    fb = la.failure_breakdown(session)
    assert fb["attempts_by_reason"]["rate_limited"] == 2  # 2 job attempts
    assert fb["unique_videos_by_reason"]["rate_limited"] == 1  # 1 distinct video


def test_metadata_run_dry_run_reports_skip(settings, session):
    _setup(session)
    res = la.metadata_run(get_settings(), target_limit=50, apply=False)
    assert res["ok"] and res["plan_selected"] == 4 and res["skipped_permanent"] == 3
    assert res["eligible_metadata_missing"] == 4 and res["permanent_unique_videos"] == 3


def test_progress_api_exposes_eligible(client, settings):
    from app.db import session_scope
    with session_scope() as s:
        _setup(s)
    p = client.get("/api/liked-videos/progress").json()
    assert p["eligible_metadata_missing"] == 4 and p["permanent_unique_videos"] == 3
    fb = client.get("/api/liked-videos/failure-breakdown").json()
    assert "unique_videos_by_reason" in fb and "attempts_by_reason" in fb
