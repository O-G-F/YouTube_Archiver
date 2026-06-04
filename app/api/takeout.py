"""Google Takeout preview / import endpoints (Phase 3A).

Reads a ZIP already placed under ``TAKEOUT_IMPORT_ROOT`` (no upload yet).
The path is validated against the import root (path-traversal protection) and
members are read in-memory (no extraction; zip-slip guarded).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.schemas import (
    LikedVideosImportOut,
    PlaylistSampleOut,
    PlaylistsImportOut,
    TakeoutFileEntryOut,
    TakeoutFilesOut,
    TakeoutImportAllOut,
    TakeoutImportAllRequest,
    TakeoutImportOut,
    TakeoutImportPlaylistsRequest,
    TakeoutImportRequest,
    TakeoutPlaylistsPreviewOut,
    TakeoutPreviewOut,
    TakeoutPreviewRequest,
)
from app.services import takeout

router = APIRouter(prefix="/api/takeout", tags=["takeout"])


@router.get("/files", response_model=TakeoutFilesOut)
def takeout_files() -> TakeoutFilesOut:
    """List ZIP files available under TAKEOUT_IMPORT_ROOT (no traversal outside)."""
    settings = get_settings()
    root = settings.takeout_import_root.resolve()
    entries: list[TakeoutFileEntryOut] = []
    if root.is_dir():
        for p in sorted(root.rglob("*.zip"))[:500]:
            try:
                rp = p.resolve()
                rp.relative_to(root)  # guard: stay within the import root
                if not rp.is_file():
                    continue
                st = rp.stat()
                entries.append(
                    TakeoutFileEntryOut(
                        name=str(rp.relative_to(root)),
                        size=st.st_size,
                        modified_at=datetime.fromtimestamp(
                            st.st_mtime, tz=timezone.utc
                        ).replace(tzinfo=None),
                        is_zip=True,
                    )
                )
            except (OSError, ValueError):
                continue
    return TakeoutFilesOut(root=str(root), files=entries)


@router.post("/preview", response_model=TakeoutPreviewOut)
def takeout_preview(req: TakeoutPreviewRequest) -> TakeoutPreviewOut:
    settings = get_settings()
    try:
        zip_path = takeout.resolve_takeout_path(settings, req.path)
        with takeout.open_archive(zip_path) as archive:
            preview = archive.preview(sample=5)
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TakeoutPreviewOut(path=req.path, **preview)


@router.post("/import", response_model=TakeoutImportOut)
def takeout_import(
    req: TakeoutImportRequest, db: Session = Depends(get_db)
) -> TakeoutImportOut:
    settings = get_settings()
    try:
        result = takeout.run_import(
            db, settings, req.path, limit=req.limit, dry_run=req.dry_run
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return TakeoutImportOut(**result)


@router.post("/import-subscriptions", response_model=TakeoutImportOut)
def takeout_import_subscriptions(
    req: TakeoutImportRequest, db: Session = Depends(get_db)
) -> TakeoutImportOut:
    try:
        result = takeout.run_import_subscriptions(
            db, get_settings(), req.path, limit=req.limit, dry_run=req.dry_run
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return TakeoutImportOut(**result)


@router.post("/import-playlists", response_model=PlaylistsImportOut)
def takeout_import_playlists(
    req: TakeoutImportPlaylistsRequest, db: Session = Depends(get_db)
) -> PlaylistsImportOut:
    try:
        result = takeout.run_import_playlists(
            db,
            get_settings(),
            req.path,
            limit_playlists=req.limit_playlists,
            limit_items=req.limit_items,
            dry_run=req.dry_run,
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return PlaylistsImportOut(**result)


@router.post("/import-liked-videos", response_model=LikedVideosImportOut)
def takeout_import_liked_videos(
    req: TakeoutImportRequest, db: Session = Depends(get_db)
) -> LikedVideosImportOut:
    try:
        result = takeout.run_import_liked_videos(
            db, get_settings(), req.path, limit=req.limit, dry_run=req.dry_run
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return LikedVideosImportOut(**result)


@router.post("/import-all", response_model=TakeoutImportAllOut)
def takeout_import_all(
    req: TakeoutImportAllRequest, db: Session = Depends(get_db)
) -> TakeoutImportAllOut:
    try:
        result = takeout.run_import_all(
            db,
            get_settings(),
            req.path,
            limit_watch=req.limit_watch,
            limit_search=req.limit_search,
            limit_subscriptions=req.limit_subscriptions,
            limit_playlists=req.limit_playlists,
            limit_items=req.limit_items,
            limit_liked=req.limit_liked,
            dry_run=req.dry_run,
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return TakeoutImportAllOut(**result)


@router.get("/playlists/preview", response_model=TakeoutPlaylistsPreviewOut)
def takeout_playlists_preview(
    path: str = Query(...), limit: int = Query(default=100, le=1000)
) -> TakeoutPlaylistsPreviewOut:
    settings = get_settings()
    try:
        zip_path = takeout.resolve_takeout_path(settings, path)
        with takeout.open_archive(zip_path) as archive:
            playlists = [
                PlaylistSampleOut(title=p.title, playlist_id=p.playlist_id, item_count=len(p.items))
                for i, p in enumerate(archive.iter_playlists())
                if i < limit
            ]
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TakeoutPlaylistsPreviewOut(path=path, playlists=playlists)
