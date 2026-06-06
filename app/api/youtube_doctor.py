"""YouTube fetch-stabilization doctor + diagnostics endpoints (Phase 7B).

``GET /api/doctor/youtube`` is a static (no-network) status. The ``run`` /
``youtube-diagnostics/run`` endpoints create a ``youtube_diagnostic`` job that
runs yt-dlp into a throwaway temp dir (never persisting a media body). No
secret value, cookie path, or token is ever returned.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.schemas import JobOut, YouTubeDiagnosticRequest, YouTubeDoctorOut
from app.services import jobs as jobs_svc
from app.services import youtube_doctor

router = APIRouter(tags=["youtube-doctor"])


@router.get("/api/doctor/youtube", response_model=YouTubeDoctorOut)
def doctor_youtube() -> YouTubeDoctorOut:
    """Static YouTube fetch-stability checks (no network, no secrets)."""
    return YouTubeDoctorOut(**youtube_doctor.static_checks(get_settings()))


def _create_diag(db: Session, req: YouTubeDiagnosticRequest) -> JobOut:
    job = jobs_svc.create_youtube_diagnostic_job(
        db,
        req.url,
        profile=req.profile,
        include_video_download=req.include_video_download,
        timeout=req.timeout,
    )
    db.commit()
    try:
        job.rq_job_id = jobs_svc.submit_job(job.id)
        db.commit()
    except Exception:  # noqa: BLE001 - Redis may be down; job stays queued
        pass
    return JobOut.model_validate(db.get(type(job), job.id))


@router.post("/api/doctor/youtube/run", response_model=JobOut, status_code=201)
def doctor_youtube_run(req: YouTubeDiagnosticRequest, db: Session = Depends(get_db)) -> JobOut:
    """Run a quick diagnostic (metadata + subtitles; video off unless requested)."""
    return _create_diag(db, req)


@router.post("/api/youtube-diagnostics/run", response_model=JobOut, status_code=201)
def youtube_diagnostics_run(
    req: YouTubeDiagnosticRequest, db: Session = Depends(get_db)
) -> JobOut:
    """Create a youtube_diagnostic job (metadata + subtitles + optional video)."""
    return _create_diag(db, req)
