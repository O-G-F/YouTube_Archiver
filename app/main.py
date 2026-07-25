"""FastAPI application entrypoint.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
or via CLI: archiver server

Serves the JSON API under ``/api/*`` and (when a built frontend is present)
the Phase 5A admin SPA at ``/`` with a history-API fallback for client routes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import (
    archive,
    audit as audit_api,
    auth as auth_api,
    collections,
    comments,
    dashboard,
    doctor,
    health,
    jobs,
    library,
    liked_videos,
    live_chat,
    profiles,
    queue as queue_api,
    scheduler,
    search,
    search_history,
    settings as settings_api,
    sources,
    storage as storage_api,
    subscriptions,
    subtitles,
    system as system_api,
    takeout,
    videos,
    watch_history,
    youtube_api,
    youtube_doctor,
)
from app.bootstrap import startup_bootstrap
from app.config import get_settings
from app.logging_setup import setup_logging
from app.middleware import AuthMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    startup_bootstrap()
    yield


app = FastAPI(
    title="YouTube Archiver",
    version=__version__,
    description="Local YouTube archiver: URL/playlist registration, profile-based "
    "yt-dlp downloads, comment/live-chat refresh, job management, and an admin UI.",
    lifespan=lifespan,
)

# Phase 9C: access control. AuthMiddleware (added first => inner) enforces auth
# for /api/*; CORS (added last => outermost) handles preflight + wraps auth
# responses with CORS headers. In development (AUTH_MODE=disabled) the middleware
# is a pass-through, so behaviour is unchanged.
_settings = get_settings()
app.add_middleware(AuthMiddleware)
# When cookie-session auth is on with specific origins, browsers require
# credentialed CORS + explicit headers; a wildcard origin cannot use credentials.
_cors_credentials = (
    (_settings.auth_mode or "disabled").lower() == "local" and not _settings.cors_is_wildcard
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins_list,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=(["Content-Type", "X-CSRF-Token", "Authorization"] if _cors_credentials else ["*"]),
)

# ---- API routers ----
app.include_router(health.router)
app.include_router(auth_api.router)
app.include_router(audit_api.router)
app.include_router(doctor.router)
app.include_router(dashboard.router)
app.include_router(settings_api.router)
app.include_router(archive.router)
app.include_router(sources.router)
app.include_router(collections.router)
app.include_router(scheduler.router)
app.include_router(takeout.router)
app.include_router(watch_history.router)
app.include_router(search_history.router)
app.include_router(subscriptions.router)
app.include_router(comments.router)
app.include_router(live_chat.router)
app.include_router(subtitles.router)
app.include_router(jobs.router)
app.include_router(profiles.router)
app.include_router(videos.router)
app.include_router(search.router)
app.include_router(library.router)
app.include_router(liked_videos.router)
app.include_router(youtube_api.router)
app.include_router(youtube_doctor.router)
app.include_router(queue_api.router)
app.include_router(storage_api.router)
app.include_router(system_api.router)


# --------------------------------------------------------------------------- #
# Frontend (built SPA) — optional. Served only when a build exists.
# --------------------------------------------------------------------------- #
def _resolve_ui_dir() -> Path | None:
    s = get_settings()
    if not s.web_ui_enabled:
        return None
    candidate = (
        Path(s.web_ui_dir)
        if s.web_ui_dir
        else Path(__file__).resolve().parent.parent / "frontend" / "dist"
    )
    index = candidate / "index.html"
    return candidate if index.is_file() else None


_ui_dir = _resolve_ui_dir()

if _ui_dir is not None:
    _assets = _ui_dir / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")

    def _index_response() -> FileResponse:
        # index.html must NOT be heuristically cached: it references content-hashed
        # asset bundles, so a stale index.html would keep loading an OLD UI after a
        # deploy (Phase 11A). `no-cache` forces revalidation (ETag) on each load;
        # the content-hashed /assets/* files stay long-cacheable.
        return FileResponse(_ui_dir / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def _spa_root() -> FileResponse:
        return _index_response()

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa_fallback(full_path: str):
        # Never shadow the API: unknown /api/* paths return a JSON 404.
        if full_path.startswith("api/") or full_path in {
            "openapi.json",
            "docs",
            "redoc",
        }:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # Serve a real static file if it exists (favicon, manifest, etc.).
        candidate = (_ui_dir / full_path).resolve()
        try:
            candidate.relative_to(_ui_dir.resolve())
            if candidate.is_file():
                return FileResponse(candidate)
        except (ValueError, OSError):
            pass
        # Otherwise hand off to the SPA router (history-API fallback).
        return _index_response()

else:

    @app.get("/", include_in_schema=False)
    def _root_redirect() -> RedirectResponse:
        # No built UI present (e.g. local dev without `npm run build`).
        return RedirectResponse(url="/docs")
