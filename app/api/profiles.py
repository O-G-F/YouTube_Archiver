"""Download profile endpoints (requirement 9.3 + Phase 1.5 dry-run)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.models import DownloadProfile
from app.schemas import BuildCommandOut, BuildCommandRequest, ProfileOut
from app.services.command_builder import dry_run_command
from app.services.urls import UrlError

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileOut])
def list_profiles(db: Session = Depends(get_db)) -> list[DownloadProfile]:
    return list(db.scalars(select(DownloadProfile).order_by(DownloadProfile.name)))


@router.post("/{profile_name}/build-command", response_model=BuildCommandOut)
def build_command(
    profile_name: str,
    req: BuildCommandRequest,
    db: Session = Depends(get_db),
) -> BuildCommandOut:
    """Dry-run: return the yt-dlp command that would run (no execution).

    Cookie/secret paths are masked. Useful for verifying profile behaviour
    (e.g. that ``--sub-langs ja,en`` and ``--remote-components ejs:github`` are
    applied) without touching the network.
    """
    try:
        result = dry_run_command(db, get_settings(), profile_name, req.url)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown profile: {profile_name!r}")
    except UrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return BuildCommandOut(**result)
