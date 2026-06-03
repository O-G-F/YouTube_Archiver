"""Ingest tests: info.json -> Video, output-dir scan, comment upsert."""

from __future__ import annotations

from app.models import Comment, MediaFile, Subtitle, Video
from app.services import storage
from app.services.ingest import (
    ingest_comments_from_info,
    register_outputs,
    upsert_video_from_info,
)


def _info():
    return {
        "id": "dQw4w9WgXcQ",
        "title": "Never Gonna Give You Up",
        "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw",
        "channel": "Rick Astley",
        "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "duration": 213,
        "upload_date": "20091025",
        "description": "official video",
        "availability": "public",
        "live_status": "not_live",
    }


def test_upsert_video_is_idempotent(session, settings):
    v1 = upsert_video_from_info(session, _info(), settings)
    assert v1.title == "Never Gonna Give You Up"
    assert v1.channel_id == "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert v1.is_live is False

    v2 = upsert_video_from_info(session, _info(), settings)
    assert v2.id == v1.id  # no duplicate
    assert session.query(Video).count() == 1


def test_register_outputs_scans_directory(session, settings):
    video = upsert_video_from_info(session, _info(), settings)
    out_dir = storage.video_output_dir(settings, "video", video.channel_id, video.youtube_video_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    # fabricate yt-dlp outputs
    (out_dir / "title [dQw4w9WgXcQ].mkv").write_bytes(b"\x00\x01video")
    (out_dir / "title [dQw4w9WgXcQ].info.json").write_text("{}", encoding="utf-8")
    (out_dir / "title [dQw4w9WgXcQ].description").write_text("desc", encoding="utf-8")
    (out_dir / "title [dQw4w9WgXcQ].jpg").write_bytes(b"\xff\xd8jpg")
    (out_dir / "title [dQw4w9WgXcQ].en.vtt").write_text("WEBVTT", encoding="utf-8")
    (out_dir / "title [dQw4w9WgXcQ].live_chat.json").write_text("[]", encoding="utf-8")

    counts = register_outputs(session, video, out_dir, "video_best_archive", settings)
    assert counts["media"] == 1
    assert counts["subtitle"] == 1
    assert counts["thumbnail"] == 1
    assert counts["info_json"] == 1

    media = session.query(MediaFile).filter_by(video_id=video.id).all()
    types = {m.media_type for m in media}
    assert {"video", "thumbnail", "info_json", "description", "live_chat"} <= types
    # paths are stored relative to ARCHIVE_ROOT
    vid_media = next(m for m in media if m.media_type == "video")
    assert not vid_media.path.startswith("/")
    assert vid_media.path.startswith("youtube/videos/")
    assert vid_media.filesize == len(b"\x00\x01video")

    assert video.thumbnail_path is not None
    assert video.raw_info_json_path is not None
    assert session.query(Subtitle).filter_by(video_id=video.id).count() == 1

    # idempotent re-scan: no duplicate media rows
    register_outputs(session, video, out_dir, "video_best_archive", settings)
    assert session.query(MediaFile).filter_by(video_id=video.id).count() == len(media)


def test_ingest_comments_upsert(session, settings):
    video = upsert_video_from_info(session, _info(), settings)
    info = {
        "comments": [
            {"id": "c1", "parent": "root", "text": "first", "author": "A",
             "author_id": "UCa", "like_count": 3, "timestamp": 1600000000},
            {"id": "c2", "parent": "c1", "text": "reply", "author": "B",
             "like_count": 1, "timestamp": 1600000100},
        ]
    }
    summary = ingest_comments_from_info(session, video, info)
    assert summary["fetched"] == 2 and summary["new"] == 2 and summary["updated"] == 0
    assert session.query(Comment).filter_by(video_id=video.id).count() == 2

    reply = session.query(Comment).filter_by(comment_id="c2").one()
    assert reply.parent_comment_id == "c1"

    # second ingest with an edited like_count -> only c1 counts as updated
    info["comments"][0]["like_count"] = 99
    summary2 = ingest_comments_from_info(session, video, info)
    assert summary2["new"] == 0 and summary2["updated"] == 1 and summary2["unchanged"] == 1
    assert session.query(Comment).filter_by(comment_id="c1").one().like_count == 99
