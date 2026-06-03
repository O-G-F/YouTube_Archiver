"""Source registration endpoints: playlist / channel (Phase 2A)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.models import Job
from app.schemas import ChannelSourceRequest, JobOut, PlaylistSourceRequest
from app.services import jobs as jobs_svc
from app.services.profiles import get_profile_spec
from app.services.urls import (
    UrlError,
    channel_tab_url,
    normalize_url,
    resolve_channel_tabs,
)

router = APIRouter(prefix="/api/sources", tags=["sources"])


def _resolve_profile(db: Session, profile: str | None) -> str:
    name = profile or get_settings().default_profile
    try:
        get_profile_spec(db, name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown profile: {name!r}")
    return name


@router.post("/playlist", response_model=JobOut, status_code=201)
def add_playlist(req: PlaylistSourceRequest, db: Session = Depends(get_db)) -> Job:
    profile = _resolve_profile(db, req.profile)
    try:
        parsed = normalize_url(req.url)
    except UrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if parsed.kind != "playlist":
        raise HTTPException(status_code=400, detail="not a playlist URL")
    return jobs_svc.create_and_submit(db, req.url, profile, max_items=req.max_items)


@router.post("/channel", response_model=list[JobOut], status_code=201)
def add_channel(req: ChannelSourceRequest, db: Session = Depends(get_db)) -> list[Job]:
    profile = _resolve_profile(db, req.profile)
    try:
        parsed = normalize_url(req.url)
    except UrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if parsed.kind != "channel":
        raise HTTPException(status_code=400, detail="not a channel URL")

    try:
        tabs = resolve_channel_tabs(parsed, req.videos, req.shorts, req.streams)
    except UrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    jobs: list[Job] = []
    for tab in tabs:
        tab_url = channel_tab_url(parsed, tab)
        jobs.append(
            jobs_svc.create_and_submit(db, tab_url, profile, max_items=req.max_items)
        )
    return jobs
