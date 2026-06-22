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
    TakeoutDiscoverEntry,
    TakeoutDiscoverOut,
    TakeoutFileEntryOut,
    TakeoutFilesOut,
    TakeoutImportAllOut,
    TakeoutImportAllRequest,
    TakeoutImportOut,
    TakeoutImportPlaylistsRequest,
    JobOut,
    TakeoutBenchmarkLargeOut,
    TakeoutBenchmarkLargeRequest,
    TakeoutBenchmarkOut,
    TakeoutBenchmarkRequest,
    TakeoutImportJobRequest,
    TakeoutImportProgressOut,
    TakeoutSessionCleanupOut,
    TakeoutSessionCleanupRequest,
    TakeoutImportSessionOut,
    TakeoutInspectOut,
    TakeoutImportRequest,
    TakeoutCleanupStatusOut,
    TakeoutImportReportOut,
    TakeoutPlaylistsPreviewOut,
    TakeoutPreflightLargeOut,
    TakeoutPreflightLargeRequest,
    TakeoutPreviewOut,
    TakeoutPreviewRequest,
    TakeoutRegistrySource,
    TakeoutVerifyImportOut,
)
from app.services import jobs as jobs_svc
from app.services import takeout

router = APIRouter(prefix="/api/takeout", tags=["takeout"])


@router.get("/files", response_model=TakeoutFilesOut)
def takeout_files() -> TakeoutFilesOut:
    """List ZIP files under TAKEOUT_IMPORT_ROOT with a kind hint (no traversal outside)."""
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
                kind: str | None = None
                try:
                    with takeout.open_archive(rp) as a:
                        kind = a.archive_kind()
                except takeout.TakeoutError:
                    kind = "unknown_takeout"
                entries.append(
                    TakeoutFileEntryOut(
                        name=str(rp.relative_to(root)),
                        size=st.st_size,
                        modified_at=datetime.fromtimestamp(
                            st.st_mtime, tz=timezone.utc
                        ).replace(tzinfo=None),
                        is_zip=True,
                        archive_kind=kind,
                    )
                )
            except (OSError, ValueError):
                continue
    return TakeoutFilesOut(root=str(root), files=entries)


@router.get("/discover", response_model=TakeoutDiscoverOut)
def takeout_discover(deep: bool = Query(default=False)) -> TakeoutDiscoverOut:
    """Classify every ZIP under TAKEOUT_IMPORT_ROOT (youtube / my_activity / index / unknown).

    ``deep=true`` also parses a liked-count hint (slower for large My Activity exports).
    """
    settings = get_settings()
    archives = [TakeoutDiscoverEntry(**e) for e in takeout.discover(settings, deep=deep)]
    return TakeoutDiscoverOut(root=str(settings.takeout_import_root.resolve()), archives=archives)


@router.get("/inspect", response_model=TakeoutInspectOut)
def takeout_inspect(
    path: str = Query(...), deep: bool = Query(default=False)
) -> TakeoutInspectOut:
    """Structural classification of a single ZIP (kind + detected liked source).

    ``deep=true`` also returns the structured source registry (Phase 6C).
    """
    settings = get_settings()
    try:
        zip_path = takeout.resolve_takeout_path(settings, path)
        with takeout.open_archive(zip_path) as a:
            info = a.inspect()
            registry = [TakeoutRegistrySource(**r) for r in a.registry()] if deep else []
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TakeoutInspectOut(path=path, registry=registry, **info)


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
@router.post("/import-watch-history", response_model=TakeoutImportOut)
def takeout_import(
    req: TakeoutImportRequest, db: Session = Depends(get_db)
) -> TakeoutImportOut:
    """Import watch history (incremental: dedup vs existing). Also at /import-watch-history."""
    settings = get_settings()
    try:
        result = takeout.run_import(
            db, settings, req.path, limit=req.limit, dry_run=req.dry_run, store_raw_json=req.store_raw_json
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return TakeoutImportOut(**result)


@router.post("/import-search-history", response_model=TakeoutImportOut)
def takeout_import_search_history(
    req: TakeoutImportRequest, db: Session = Depends(get_db)
) -> TakeoutImportOut:
    """Import search history (incremental: dedup vs existing)."""
    try:
        result = takeout.run_import_search(
            db, get_settings(), req.path, limit=req.limit, dry_run=req.dry_run, store_raw_json=req.store_raw_json
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
            db, get_settings(), req.path, limit=req.limit, dry_run=req.dry_run, store_raw_json=req.store_raw_json
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
            store_raw_json=req.store_raw_json,
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return TakeoutImportAllOut(**result)


@router.post("/benchmark", response_model=TakeoutBenchmarkOut)
def takeout_benchmark(
    req: TakeoutBenchmarkRequest, db: Session = Depends(get_db)
) -> TakeoutBenchmarkOut:
    """Measure parse/import throughput + peak memory (dry_run default). No content returned."""
    try:
        result = takeout.benchmark(
            db, get_settings(), req.path, kind=req.kind, limit=req.limit, dry_run=req.dry_run
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not req.dry_run:
        db.commit()
    return TakeoutBenchmarkOut(**result)


def _create_import_job(db: Session, import_kind: str, req: TakeoutImportJobRequest) -> JobOut:
    try:
        job, _row = takeout.create_import_job(
            db, get_settings(), import_kind=import_kind, path=req.path,
            limit=req.limit, dry_run=req.dry_run, store_raw_json=req.store_raw_json,
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    try:
        rq_id = jobs_svc.submit_job(job.id)
        if rq_id:
            job.rq_job_id = rq_id
            db.commit()
    except Exception:  # noqa: BLE001 - Redis down; job stays queued
        pass
    return JobOut.model_validate(db.get(type(job), job.id))


@router.post("/import-liked-videos-job", response_model=JobOut, status_code=201)
def takeout_import_liked_job(req: TakeoutImportJobRequest, db: Session = Depends(get_db)) -> JobOut:
    """Run a liked-videos import as a background job (large imports). No body DL."""
    return _create_import_job(db, "liked_videos", req)


@router.post("/import-watch-history-job", response_model=JobOut, status_code=201)
def takeout_import_watch_job(req: TakeoutImportJobRequest, db: Session = Depends(get_db)) -> JobOut:
    return _create_import_job(db, "watch_history", req)


@router.post("/import-search-history-job", response_model=JobOut, status_code=201)
def takeout_import_search_job(req: TakeoutImportJobRequest, db: Session = Depends(get_db)) -> JobOut:
    return _create_import_job(db, "search_history", req)


@router.post("/benchmark-large", response_model=TakeoutBenchmarkLargeOut)
def takeout_benchmark_large(
    req: TakeoutBenchmarkLargeRequest, db: Session = Depends(get_db)
) -> TakeoutBenchmarkLargeOut:
    """Full-scan dry-run benchmark for liked + watch (+ optional search). No content."""
    try:
        result = takeout.benchmark_large(db, get_settings(), req.path, include_search=req.include_search)
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TakeoutBenchmarkLargeOut(**result)


@router.post("/preflight-large", response_model=TakeoutPreflightLargeOut)
def takeout_preflight_large(
    req: TakeoutPreflightLargeRequest, db: Session = Depends(get_db)
) -> TakeoutPreflightLargeOut:
    """Quick go/no-go before a large import (ZIP/parser/sample bench/DB counts)."""
    try:
        result = takeout.preflight_large(
            db, get_settings(), req.path, kind=req.kind, sample_limit=req.sample_limit
        )
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TakeoutPreflightLargeOut(**result)


@router.get("/import-sessions/cleanup-status", response_model=TakeoutCleanupStatusOut)
def takeout_cleanup_status(db: Session = Depends(get_db)) -> TakeoutCleanupStatusOut:
    """Auto session-cleanup config + last run result (Phase 6F)."""
    return TakeoutCleanupStatusOut(**takeout.cleanup_status(get_settings()))


@router.get("/import-sessions/{session_id}/verify", response_model=TakeoutVerifyImportOut)
def takeout_verify_import(session_id: str, db: Session = Depends(get_db)) -> TakeoutVerifyImportOut:
    """Post-import inspection: outcome + DB stats + raw_json blobs + leak grep."""
    result = takeout.verify_import(db, get_settings(), session_id=session_id)
    if not result.get("session_id"):
        raise HTTPException(status_code=404, detail="import session not found")
    return TakeoutVerifyImportOut(**result)


@router.get("/import-report/latest", response_model=TakeoutImportReportOut)
def takeout_import_report_latest(db: Session = Depends(get_db)) -> TakeoutImportReportOut:
    """Operation report for the most recent import session (Phase 6G)."""
    result = takeout.import_report(db, get_settings(), latest=True)
    if not result.get("session_id"):
        raise HTTPException(status_code=404, detail="no import sessions yet")
    return TakeoutImportReportOut(**result)


@router.get("/import-report/{session_id}", response_model=TakeoutImportReportOut)
def takeout_import_report(session_id: str, db: Session = Depends(get_db)) -> TakeoutImportReportOut:
    """Operation report for one import session (Phase 6G)."""
    result = takeout.import_report(db, get_settings(), session_id=session_id)
    if not result.get("session_id"):
        raise HTTPException(status_code=404, detail="import session not found")
    return TakeoutImportReportOut(**result)


@router.post("/import-sessions/cleanup", response_model=TakeoutSessionCleanupOut)
def takeout_sessions_cleanup(
    req: TakeoutSessionCleanupRequest, db: Session = Depends(get_db)
) -> TakeoutSessionCleanupOut:
    """Prune old import sessions. Deletes ONLY session rows — never jobs / imported data."""
    res = takeout.cleanup_import_sessions(
        db, keep_last=req.keep_last, older_than_days=req.older_than_days, dry_run=req.dry_run
    )
    if not req.dry_run:
        db.commit()
    return TakeoutSessionCleanupOut(**res)


@router.get("/import-sessions/{session_id}/progress", response_model=TakeoutImportProgressOut)
def takeout_import_progress(session_id: str, db: Session = Depends(get_db)) -> TakeoutImportProgressOut:
    row = takeout.get_import_session(db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="import session not found")
    return TakeoutImportProgressOut(
        session_id=row.session_id, status=row.status, current_phase=row.current_phase,
        scanned=row.scanned, imported=row.imported, skipped_duplicate=row.skipped_duplicate,
        updated=row.updated, failed=row.failed, entries_per_second=row.entries_per_second,
        cancel_requested=row.cancel_requested, job_id=row.job_id, last_update_at=row.last_update_at,
    )


@router.post("/import-sessions/{session_id}/cancel", response_model=TakeoutImportProgressOut)
def takeout_import_cancel(session_id: str, db: Session = Depends(get_db)) -> TakeoutImportProgressOut:
    """Request cancellation of a running import (the worker stops at the next checkpoint)."""
    ok = takeout.request_cancel(db, session_id)
    if not ok:
        row = takeout.get_import_session(db, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="import session not found")
        raise HTTPException(status_code=409, detail=f"cannot cancel a {row.status} session")
    db.commit()
    return takeout_import_progress(session_id, db)


@router.get("/import-sessions", response_model=list[TakeoutImportSessionOut])
def takeout_import_sessions(
    db: Session = Depends(get_db),
    import_kind: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
) -> list[TakeoutImportSessionOut]:
    """Import history (counts only; no full path / raw_json / personal rows)."""
    rows = takeout.list_import_sessions(db, import_kind=import_kind, limit=limit)
    return [TakeoutImportSessionOut.model_validate(r) for r in rows]


@router.get("/import-sessions/{session_id}", response_model=TakeoutImportSessionOut)
def takeout_import_session_detail(
    session_id: str, db: Session = Depends(get_db)
) -> TakeoutImportSessionOut:
    row = takeout.get_import_session(db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="import session not found")
    return TakeoutImportSessionOut.model_validate(row)


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
