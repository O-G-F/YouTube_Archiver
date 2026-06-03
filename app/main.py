"""FastAPI application entrypoint.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
or via CLI: archiver server
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app import __version__
from app.api import (
    archive,
    collections,
    doctor,
    health,
    jobs,
    profiles,
    scheduler,
    sources,
    videos,
)
from app.bootstrap import startup_bootstrap
from app.logging_setup import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    startup_bootstrap()
    yield


app = FastAPI(
    title="YouTube Archiver",
    version=__version__,
    description="Local YouTube archiver (Phase 0-1): URL/playlist registration, "
    "profile-based yt-dlp downloads, comment refresh, job management.",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(doctor.router)
app.include_router(archive.router)
app.include_router(sources.router)
app.include_router(collections.router)
app.include_router(scheduler.router)
app.include_router(jobs.router)
app.include_router(profiles.router)
app.include_router(videos.router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")
