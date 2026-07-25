"""Phase 8A: body archive excludes permanent (private/deleted/unavailable) by
default — they can't be downloaded and are kept, never deleted. archive_plan
reports permanent_excluded / eligible_missing_body. --include-permanent overrides.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.config import get_settings
from app.models import Job, LikedVideo, MediaFile, Video
from app.services import liked_archive as la


def _video(s, vid):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
              title="t", channel_title="C", first_seen_at=datetime(2025, 1, 1))
    s.add(v); s.flush()
    return v


def _liked(s, vid, video, *, liked_at=datetime(2025, 1, 1)):
    s.add(LikedVideo(source="takeout_my_activity", youtube_video_id=vid, title="t",
                     url=f"https://youtu.be/{vid}", liked_at=liked_at, video_id=video.id))
    s.flush()


def _mjob(s, video, err):
    # a metadata_only job whose classification marks the video permanent
    j = Job(type="download", status="failed", url=video.url, video_id=video.id,
            profile_name="metadata_only", error_message=err,
            meta={"source_action": la.SOURCE_ACTION})
    s.add(j); s.flush()
    return j


def _info_json(s, video):
    s.add(MediaFile(video_id=video.id, media_type="info_json",
                    path=f"{video.youtube_video_id}.info.json", profile="metadata_only"))
    s.flush()


def _setup(s):
    # permanent (excluded from body archive)
    vp = _video(s, "vidprivate1"); _liked(s, "vidprivate1", vp); _mjob(s, vp, "Private video")
    vd = _video(s, "viddeleted1"); _liked(s, "viddeleted1", vd); _mjob(s, vd, "This video has been removed by the uploader")
    vu = _video(s, "vidunavail1"); _liked(s, "vidunavail1", vu); _mjob(s, vu, "Video unavailable")
    # normal, info_json complete, missing body (eligible for body archive)
    for n in range(4):
        vn = _video(s, f"vidnormal{n}"); _liked(s, f"vidnormal{n}", vn); _info_json(s, vn)
    s.commit()


def test_body_archive_excludes_permanent_by_default(settings, session):
    _setup(session)
    r = la.enqueue_archive(
        session, get_settings(),
        filters=la.LikedFilters(missing_body=True), limit=10, dry_run=True,
    )
    assert r.skipped_permanent == 3          # private/deleted/unavailable skipped
    assert r.selected_count == 4             # only the 4 normal videos
    assert r.downloads_body is True


def test_body_archive_include_permanent_override(settings, session):
    _setup(session)
    r = la.enqueue_archive(
        session, get_settings(),
        filters=la.LikedFilters(missing_body=True), limit=10, dry_run=True,
        exclude_permanent=False,
    )
    assert r.skipped_permanent == 0
    assert r.selected_count == 7             # 3 permanent + 4 normal


def test_archive_plan_reports_permanent_excluded(settings, session):
    _setup(session)
    plan = la.archive_plan(session, get_settings(), filters=la.LikedFilters())
    assert plan.permanent_excluded == 3
    assert plan.missing_body == 7            # raw missing-body count
    assert plan.eligible_missing_body == 4   # minus permanent


def test_body_archive_prioritizes_info_json_complete(settings, session):
    s = session
    # newest liked = description-only (no info_json); older = info_json complete
    vnew = _video(s, "vidnewdesc1")
    _liked(s, "vidnewdesc1", vnew, liked_at=datetime(2026, 1, 2))
    s.add(MediaFile(video_id=vnew.id, media_type="description",
                    path="vidnewdesc1.description", profile="metadata_only")); s.flush()
    vold = _video(s, "vidoldinfo1")
    _liked(s, "vidoldinfo1", vold, liked_at=datetime(2026, 1, 1)); _info_json(s, vold)
    s.commit()
    # limit 1 must pick the info_json-complete video despite it being older.
    r = la.enqueue_archive(session, get_settings(),
                           filters=la.LikedFilters(missing_body=True), limit=1, dry_run=False, submit=False)
    assert r.jobs_created == 1
    job = s.get(Job, r.job_ids[0])
    assert job.video_id == vold.id           # info_json-complete chosen first
