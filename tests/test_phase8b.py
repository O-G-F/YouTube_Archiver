"""Phase 8B: comments-light body profile (video_compressed_1080p_light).

Same as video_compressed_1080p (<=1080p mp4 + info_json/description/thumbnail/
subtitles) but WITHOUT --write-comments, to avoid large DB growth when body-
archiving in bulk. The standard profile keeps comments (compat). Body-archive
profile selection + permanent-exclusion + info_json-priority still apply.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.config import get_settings
from app.models import Job, LikedVideo, MediaFile, Video
from app.services import liked_archive as la
from app.services.profiles import BUILTIN_PROFILES, BuildContext, build_ytdlp_args

LIGHT = "video_compressed_1080p_light"
STD = "video_compressed_1080p"


def test_light_profile_exists_and_disables_comments():
    assert LIGHT in BUILTIN_PROFILES
    spec = BUILTIN_PROFILES[LIGHT]
    assert spec.media_mode == "video"
    flags = spec.resolved_flags()
    assert flags["write_comments"] is False
    # keeps the rest of the body metadata
    assert flags["write_info_json"] is True
    assert flags["write_description"] is True
    assert flags["write_thumbnail"] is True
    assert flags["write_subs"] is True


def test_standard_profile_still_writes_comments():
    # compatibility: the existing profile is unchanged.
    assert BUILTIN_PROFILES[STD].resolved_flags()["write_comments"] is True


def test_light_command_has_video_but_no_comments():
    ctx = BuildContext(output_template="/tmp/%(id)s.%(ext)s", max_comments=0)
    args = build_ytdlp_args(BUILTIN_PROFILES[LIGHT], ctx)
    assert "--write-comments" not in args          # comments disabled
    assert "--write-info-json" in args             # metadata kept
    assert "--write-description" in args
    assert "--write-thumbnail" in args
    assert "--write-subs" in args
    assert "-f" in args                            # video format selector present
    assert "mp4" in args                           # merge to mp4
    assert "--skip-download" not in args           # body IS downloaded


def test_standard_command_includes_comments():
    ctx = BuildContext(output_template="/tmp/%(id)s.%(ext)s", max_comments=0)
    args = build_ytdlp_args(BUILTIN_PROFILES[STD], ctx)
    assert "--write-comments" in args


# --------------------------------------------------------------------------- #
# body-archive with the light profile still excludes permanent + prioritizes info_json
# --------------------------------------------------------------------------- #
def _video(s, vid):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
              title="t", channel_title="C", first_seen_at=datetime(2025, 1, 1))
    s.add(v); s.flush()
    return v


def _liked(s, vid, video, *, liked_at=datetime(2025, 1, 1)):
    s.add(LikedVideo(source="takeout_my_activity", youtube_video_id=vid, title="t",
                     url=f"https://youtu.be/{vid}", liked_at=liked_at, video_id=video.id))
    s.flush()


def _info_json(s, video):
    s.add(MediaFile(video_id=video.id, media_type="info_json",
                    path=f"{video.youtube_video_id}.info.json", profile="metadata_only"))
    s.flush()


def _mjob(s, video, err):
    j = Job(type="download", status="failed", url=video.url, video_id=video.id,
            profile_name="metadata_only", error_message=err,
            meta={"source_action": la.SOURCE_ACTION})
    s.add(j); s.flush()


def test_enqueue_archive_with_light_profile(settings, session):
    s = session
    vp = _video(s, "vidprivate9"); _liked(s, "vidprivate9", vp); _mjob(s, vp, "Private video")
    vnew = _video(s, "vidnewdesc9")
    _liked(s, "vidnewdesc9", vnew, liked_at=datetime(2026, 1, 2))
    s.add(MediaFile(video_id=vnew.id, media_type="description",
                    path="vidnewdesc9.description", profile="metadata_only")); s.flush()
    vold = _video(s, "vidoldinfo9")
    _liked(s, "vidoldinfo9", vold, liked_at=datetime(2026, 1, 1)); _info_json(s, vold)
    s.commit()

    # limit high enough to scan all candidates (so the permanent one is reached).
    r = la.enqueue_archive(session, get_settings(),
                           filters=la.LikedFilters(missing_body=True), limit=10,
                           profile=LIGHT, dry_run=False, submit=False)
    assert r.profile == LIGHT
    assert r.skipped_permanent == 1                 # private excluded
    assert r.jobs_created == 2                       # vold (info_json) + vnew (desc)
    first = s.get(Job, r.job_ids[0])
    assert first.profile_name == LIGHT              # profile reflected on the job
    assert first.video_id == vold.id                # info_json-complete chosen first
