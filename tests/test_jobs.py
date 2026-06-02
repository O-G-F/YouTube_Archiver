"""Job creation, DB persistence, and status transitions
(requirement 15: test job creation, DB save)."""

from __future__ import annotations

import pytest

from app.models import Job, Video
from app.services import jobs as jobs_svc


def test_create_download_job_for_video(session):
    job = jobs_svc.create_job_for_url(
        session, "https://youtu.be/dQw4w9WgXcQ", "video_best_archive"
    )
    assert job.id is not None
    assert job.type == "download"
    assert job.status == "queued"
    assert job.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert job.profile_name == "video_best_archive"
    # persisted
    assert session.get(Job, job.id) is job


def test_playlist_url_creates_expand_job(session):
    job = jobs_svc.create_job_for_url(
        session, "https://www.youtube.com/playlist?list=PLabc", "video_compressed_1080p"
    )
    assert job.type == "expand"


def test_channel_url_creates_expand_job(session):
    job = jobs_svc.create_job_for_url(
        session, "https://www.youtube.com/@example/videos", "video_compressed_1080p"
    )
    assert job.type == "expand"


def test_invalid_url_raises(session):
    with pytest.raises(Exception):
        jobs_svc.create_job_for_url(session, "https://example.com", "metadata_only")


def test_metadata_refresh_job(session):
    video = Video(youtube_video_id="dQw4w9WgXcQ", title="x")
    session.add(video)
    session.flush()
    job = jobs_svc.create_metadata_refresh_job(session, video)
    assert job.type == "metadata_refresh"
    assert job.video_id == video.id
    assert job.profile_name == "comments_refresh_only"
    assert job.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_status_transitions(session):
    job = jobs_svc.create_job_for_url(session, "https://youtu.be/dQw4w9WgXcQ", "metadata_only")

    jobs_svc.mark_running(session, job)
    assert job.status == "running" and job.started_at is not None

    jobs_svc.mark_failed(session, job, "boom")
    assert job.status == "failed"
    assert job.error_message == "boom"
    assert job.finished_at is not None

    jobs_svc.retry_job(session, job)
    assert job.status == "queued"
    assert job.error_message is None
    assert job.started_at is None

    jobs_svc.mark_running(session, job)
    jobs_svc.mark_success(session, job)
    assert job.status == "success" and job.progress == 100.0


def test_partial_success(session):
    job = jobs_svc.create_job_for_url(session, "https://youtu.be/dQw4w9WgXcQ", "metadata_only")
    jobs_svc.mark_running(session, job)
    jobs_svc.mark_partial_success(session, job, "yt-dlp exited 1 but subs were saved. HTTP 429")
    assert job.status == "partial_success"
    assert job.finished_at is not None
    assert "429" in job.error_message
