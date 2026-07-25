"""Phase 8C: orphan download-job reconcile + duplicate-media check.

A job left ``running`` in the DB but absent from RQ (worker crash / host sleep)
is an orphan. reconcile detects it and (on --apply) safely re-queues it without
re-downloading anything already saved. Jobs still in RQ or too recent are never
touched; if RQ is unreadable, no action is taken.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import get_settings
from app.models import Job, MediaFile, Video
from app.services import reconcile

NOW = datetime(2026, 6, 26, 12, 0, 0)


def _video(s, vid, *, body=False):
    v = Video(youtube_video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
              title="t", channel_title="C", first_seen_at=datetime(2025, 1, 1))
    s.add(v); s.flush()
    if body:
        s.add(MediaFile(video_id=v.id, media_type="video", path=f"{vid}.mp4",
                        profile="video_compressed_1080p_light"))
    s.flush()
    return v


def _job(s, v, *, status="running", rq_job_id="rq-x", age_min=60):
    j = Job(type="download", status=status, url=v.url, video_id=v.id,
            profile_name="video_compressed_1080p_light", rq_job_id=rq_job_id,
            meta={"source_action": "liked_archive"})
    s.add(j); s.flush()
    j.created_at = NOW - timedelta(minutes=age_min)
    j.started_at = NOW - timedelta(minutes=age_min)
    s.flush()
    return j


def test_orphan_detected_dry_run(settings, session):
    v = _video(session, "vidorph0001")
    j = _job(session, v, rq_job_id="gone")
    session.commit()
    r = reconcile.reconcile_orphans(session, get_settings(), apply=False,
                                    older_than_minutes=30, now=NOW, rq_ids=set())
    assert r["orphan_found"] == 1
    assert r["requeued"] == 1                 # would requeue (no body)
    assert r["skipped_already_has_body"] == 0
    assert session.get(Job, j.id).status == "running"   # dry-run: unchanged


def test_job_in_rq_not_orphan(settings, session):
    v = _video(session, "vidinrq0001")
    j = _job(session, v, rq_job_id="present")
    session.commit()
    # rq_ids is the set of DB job_ids RQ references (matched via RQ job args).
    r = reconcile.reconcile_orphans(session, get_settings(), apply=False,
                                    older_than_minutes=30, now=NOW, rq_ids={j.id})
    assert r["orphan_found"] == 0
    assert r["skipped_rq_present"] == 1


def test_present_by_args_even_without_rq_job_id(settings, session):
    # REGRESSION (Phase 8E): archive jobs may have rq_job_id=None but still be
    # queued in RQ (matched by the DB job_id in the RQ job's args). Must NOT be
    # flagged as an orphan, or --apply would double-enqueue live jobs.
    v = _video(session, "vidnorqid001")
    j = _job(session, v, rq_job_id=None)
    session.commit()
    r = reconcile.reconcile_orphans(session, get_settings(), apply=True,
                                    older_than_minutes=30, now=NOW, rq_ids={j.id})
    assert r["orphan_found"] == 0
    assert r["skipped_rq_present"] == 1
    assert session.get(Job, j.id).status == "running"   # untouched


def test_recent_job_skipped(settings, session):
    v = _video(session, "vidrecent001")
    _job(session, v, rq_job_id="gone", age_min=5)   # younger than 30m
    session.commit()
    r = reconcile.reconcile_orphans(session, get_settings(), apply=False,
                                    older_than_minutes=30, now=NOW, rq_ids=set())
    assert r["orphan_found"] == 0
    assert r["skipped_recent"] == 1


def test_apply_requeues_orphan_without_body(settings, session, monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.jobs.submit_job", lambda jid: calls.append(jid) or "rq-new")
    v = _video(session, "vidnobody001")
    j = _job(session, v, rq_job_id="gone")
    session.commit()
    r = reconcile.reconcile_orphans(session, get_settings(), apply=True,
                                    older_than_minutes=30, now=NOW, rq_ids=set())
    assert r["requeued"] == 1
    jj = session.get(Job, j.id)
    assert jj.status == "queued"              # reset for re-run
    assert jj.rq_job_id == "rq-new"
    assert calls == [j.id]                     # re-enqueued to RQ


def test_apply_has_body_marks_success_no_redownload(settings, session, monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.jobs.submit_job", lambda jid: calls.append(jid) or "rq-new")
    v = _video(session, "vidhasbody01", body=True)   # already has a video file
    j = _job(session, v, rq_job_id="gone")
    session.commit()
    r = reconcile.reconcile_orphans(session, get_settings(), apply=True,
                                    older_than_minutes=30, now=NOW, rq_ids=set())
    assert r["orphan_found"] == 1
    assert r["skipped_already_has_body"] == 1
    assert r["requeued"] == 0
    assert session.get(Job, j.id).status == "success"   # reconciled, not re-run
    assert calls == []                                   # NO re-download


def test_rq_unreadable_takes_no_action(settings, session):
    v = _video(session, "vidunread001")
    j = _job(session, v, rq_job_id="gone")
    session.commit()
    r = reconcile.reconcile_orphans(session, get_settings(), apply=True,
                                    older_than_minutes=30, now=NOW, rq_ids=None)
    assert r["rq_unreadable"] is True
    assert r["orphan_found"] == 0
    assert session.get(Job, j.id).status == "running"   # untouched


def test_duplicate_video_media_detection(settings, session):
    v1 = _video(session, "viddup00001", body=True)
    # add a SECOND video media file for v1 (a duplicate)
    session.add(MediaFile(video_id=v1.id, media_type="video", path="dup.mp4",
                          profile="video_compressed_1080p_light"))
    v2 = _video(session, "vidok000001", body=True)   # exactly one -> not a dup
    session.commit()
    dups = reconcile.duplicate_video_media(session)
    ids = {d["video_id"] for d in dups}
    assert v1.id in ids
    assert v2.id not in ids
