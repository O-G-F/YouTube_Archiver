"""Phase 7L: rate-limit stabilization + info_json completeness.

Covers:
  * combine_run_level — a batch STOP (or rate-limit halt) elevates the reported
    run level to "stop" even when the averaged overall ratio reads "ok".
  * compute_liked_job_delay — metadata jobs get base delay + bounded jitter;
    non-metadata / non-liked jobs are unaffected (deterministic via injected fn).
  * progress() — broad metadata_fetched vs rigorous info_json_complete_count,
    description_only_count, retryable_partial_count.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.models import Job, LikedVideo, MediaFile, Video
from app.services import liked_archive as la
from app.worker.tasks import compute_liked_job_delay


# --------------------------------------------------------------------------- #
# combine_run_level (pure)
# --------------------------------------------------------------------------- #
def test_combine_level_batch_stop_elevates_ok_overall_to_stop():
    # The 2000-run shape: overall average "ok", but a batch hit STOP and halted.
    lvl = la.combine_run_level("ok", ["ok", "warn", "ok", "stop"], "rate_limit_ratio>=0.8")
    assert lvl == "stop"


def test_combine_level_rate_limit_stop_forces_stop_even_without_batch_row():
    assert la.combine_run_level("ok", [], "rate_limit_ratio>=0.8") == "stop"


def test_combine_level_benign_stop_keeps_overall():
    # nothing-left / max_batches are NOT rate-limit stops -> level stays as-is.
    assert la.combine_run_level("ok", ["ok", "ok"], "no_more_missing") == "ok"
    assert la.combine_run_level("ok", ["ok"], "max_batches") == "ok"


def test_combine_level_warn_batch_without_stop():
    assert la.combine_run_level("ok", ["ok", "warn"], None) == "warn"


# --------------------------------------------------------------------------- #
# compute_liked_job_delay (pure, injectable jitter)
# --------------------------------------------------------------------------- #
def _settings(*, dl=0.0, meta=0.0, jitter=0.0, archive=0.0):
    return SimpleNamespace(
        download_job_delay_seconds=dl,
        liked_metadata_job_delay_seconds=meta,
        liked_metadata_job_delay_jitter_seconds=jitter,
        liked_archive_job_delay_seconds=archive,
    )


def test_delay_metadata_adds_jitter():
    s = _settings(meta=3.0, jitter=1.0)
    # jitter_fn returns its upper bound -> deterministic 3.0 + 1.0
    d = compute_liked_job_delay(s, is_liked=True, is_liked_metadata=True,
                                jitter_fn=lambda lo, hi: hi)
    assert d == pytest.approx(4.0)
    # lower bound of jitter -> just the base delay
    d0 = compute_liked_job_delay(s, is_liked=True, is_liked_metadata=True,
                                 jitter_fn=lambda lo, hi: lo)
    assert d0 == pytest.approx(3.0)


def test_delay_metadata_jitter_zero_is_base_only():
    s = _settings(meta=3.0, jitter=0.0)
    d = compute_liked_job_delay(s, is_liked=True, is_liked_metadata=True,
                                jitter_fn=lambda lo, hi: hi)
    assert d == pytest.approx(3.0)


def test_delay_jitter_not_applied_to_body_or_nonliked():
    s = _settings(dl=0.5, meta=3.0, jitter=1.0, archive=2.0)
    # liked BODY archive -> archive delay, NO metadata jitter
    body = compute_liked_job_delay(s, is_liked=True, is_liked_metadata=False,
                                   jitter_fn=lambda lo, hi: hi)
    assert body == pytest.approx(2.0)
    # non-liked download -> just the base download delay
    other = compute_liked_job_delay(s, is_liked=False, is_liked_metadata=False,
                                    jitter_fn=lambda lo, hi: hi)
    assert other == pytest.approx(0.5)


def test_real_uniform_jitter_within_bounds():
    s = _settings(meta=3.0, jitter=1.0)
    for _ in range(50):
        d = compute_liked_job_delay(s, is_liked=True, is_liked_metadata=True)
        assert 3.0 <= d <= 4.0


# --------------------------------------------------------------------------- #
# progress() completeness counts
# --------------------------------------------------------------------------- #
def _video(s, vid):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
              title="t", channel_title="C", first_seen_at=datetime(2025, 1, 1))
    s.add(v); s.flush()
    return v


def _liked(s, vid, video):
    s.add(LikedVideo(source="takeout_my_activity", youtube_video_id=vid, title="t",
                     url=f"https://youtu.be/{vid}", liked_at=datetime(2025, 1, 1),
                     video_id=video.id))
    s.flush()


def _media(s, video, mtype):
    s.add(MediaFile(video_id=video.id, media_type=mtype,
                    path=f"{video.youtube_video_id}.{mtype}", profile="metadata_only"))
    s.flush()


def _mjob(s, video, err, *, status="partial_success"):
    j = Job(type="download", status=status, url=video.url, video_id=video.id,
            profile_name="metadata_only", error_message=err,
            meta={"source_action": la.SOURCE_ACTION})
    s.add(j); s.flush()
    return j


def test_progress_completeness_split(settings, session):
    s = session
    # v1: full info_json complete
    v1 = _video(s, "vidinfojson1"); _liked(s, "vidinfojson1", v1); _media(s, v1, "info_json")
    # v2: description-only, latest metadata job = rate_limited -> retryable partial
    v2 = _video(s, "viddesc429x1"); _liked(s, "viddesc429x1", v2); _media(s, v2, "description")
    _mjob(s, v2, "HTTP Error 429: Too Many Requests")
    # v3: description-only, no job -> description_only but NOT retryable_partial
    v3 = _video(s, "viddesconly1"); _liked(s, "viddesconly1", v3); _media(s, v3, "description")
    # v4: fresh (no media) -> missing
    v4 = _video(s, "vidfresh0001"); _liked(s, "vidfresh0001", v4)
    s.commit()

    p = la.progress(session, get_settings())
    assert p["total_liked"] == 4
    assert p["metadata_fetched"] == 3          # broad: v1, v2, v3
    assert p["metadata_any_count"] == 3
    assert p["info_json_complete_count"] == 1  # v1 only
    assert p["description_only_count"] == 2     # v2, v3
    assert p["retryable_partial_count"] == 1    # v2 (rate_limited); v3 has no job
    assert p["metadata_missing"] == 1           # v4
