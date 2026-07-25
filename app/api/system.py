"""System build identity + full health (Phase 6F).

``/api/system/build-info`` reports this (web) process's build identity.
``/api/system/health/full`` adds DB/Redis reachability, the live worker
heartbeats, and whether web and worker share a build_id (stale-worker guard).

No secrets / host absolute paths are returned.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.schemas import (
    ArchiveMediaCheckOut,
    BackupReadinessOut,
    BuildInfoOut,
    FullHealthOut,
    ProductionCheckOut,
    FirstRunStatusOut,
    ReleaseReadinessOut,
    SecretsStatusOut,
    VersionOut,
)
from app.services import build_info as bi
from app.services import preflight as pf
from app.services import production_check as pc

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/build-info", response_model=BuildInfoOut)
def build_info() -> BuildInfoOut:
    return BuildInfoOut(**bi.build_info())


@router.get("/version", response_model=VersionOut)
def version() -> VersionOut:
    """Phase 10A: release/version identity (app version, commit, tree clean,
    build id/timestamp, schema head, frontend bundle id, image digest). No host
    paths, repo paths, usernames, or secret values."""
    return VersionOut(**bi.version_info())


@router.get("/secrets-status", response_model=SecretsStatusOut)
def secrets_status() -> SecretsStatusOut:
    """Cookie / PO-token configuration STATUS (Phase 7I). Booleans/masked only —
    never returns secret values or absolute paths."""
    s = get_settings()
    cf = s.cookies_file_status()  # {configured, file_configured, file_exists, readable, last_modified}
    return SecretsStatusOut(
        cookies_configured=s.cookies_configured,
        cookies_file_configured=bool(cf["file_configured"]),
        cookies_file_readable=bool(cf["readable"]),
        cookies_from_browser_configured=s.browser_cookies_configured,
        po_token_configured=s.po_token_configured,
        visitor_data_configured=s.visitor_data_configured,
        cookies_last_modified=cf.get("last_modified"),
        secret_value_exposed=False,
    )


def _audit_check_run(db, request, event_type, overall):
    from app.services import audit

    audit.record_request_event(db, get_settings(), request, event_type=event_type, category="ops",
                               severity=("warning" if overall == "fail" else "info"),
                               outcome=("failure" if overall == "fail" else "success"),
                               action="run", metadata={"overall": overall})


@router.get("/production-check", response_model=ProductionCheckOut)
def production_check(request: Request, db: Session = Depends(get_db)) -> ProductionCheckOut:
    """Phase 9B: consolidated production-readiness report (PASS/WARN/FAIL). No
    secret values or host paths — booleans/counts only."""
    r = pc.production_check(db)
    _audit_check_run(db, request, "production_check_run", r["overall"])
    return ProductionCheckOut(**r)


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(db: Session = Depends(get_db)) -> PlainTextResponse:
    """Phase 9E: Prometheus metrics (counts/gauges only, no identity labels).
    Auth-required (served under /api/*). No secrets / host paths / high cardinality."""
    from app.services import metrics as metrics_svc

    return PlainTextResponse(metrics_svc.render_prometheus(db),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/release-check", response_model=ProductionCheckOut)
def release_check(request: Request, db: Session = Depends(get_db)) -> ProductionCheckOut:
    """Phase 9D: deploy gate = production-check + archive presence + migration
    status + backup freshness + build version. No secret values or host paths."""
    r = pc.release_check(db)
    _audit_check_run(db, request, "release_check_run", r["overall"])
    return ProductionCheckOut(**r)


@router.get("/backup-readiness", response_model=BackupReadinessOut)
def backup_readiness() -> BackupReadinessOut:
    """Phase 9F: read-only backup / disaster-recovery readiness (marker ages +
    manifest summary). Basenames/counts only — no paths or secret values."""
    return BackupReadinessOut(**pc.backup_readiness(get_settings()))


@router.get("/release-readiness", response_model=ReleaseReadinessOut)
def release_readiness() -> ReleaseReadinessOut:
    """Phase 10A: read-only release / provenance readiness (version identity +
    release manifest summary + supply-chain statuses). No paths or secret values."""
    return ReleaseReadinessOut(**pc.release_readiness(get_settings()))


@router.get("/first-run", response_model=FirstRunStatusOut)
def first_run(db: Session = Depends(get_db)) -> FirstRunStatusOut:
    """Phase 11B: fresh-install setup checklist — done/next steps with safe links.
    Counts / booleans / route paths only; no secrets or host paths."""
    return FirstRunStatusOut(**pc.first_run_status(db, get_settings()))


@router.get("/archive-check", response_model=ArchiveMediaCheckOut)
def archive_check(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(default=0, ge=0, description="0 = check all video media_files"),
) -> ArchiveMediaCheckOut:
    """Phase 9B: archive-root migration guard — do DB video media_files resolve to
    real files? Missing videos are reported by public youtube id, never by path."""
    r = pc.archive_media_check(db, limit=(limit or None))
    from app.services import audit

    audit.record_request_event(db, get_settings(), request, event_type="archive_check_run",
                               category="ops", severity=("warning" if not r["ok"] else "info"),
                               outcome=("failure" if not r["ok"] else "success"), action="run",
                               metadata={"missing": r["missing"], "duplicate": r["duplicate_video_media_files"]})
    return ArchiveMediaCheckOut(**r)


@router.get("/health/full", response_model=FullHealthOut)
def health_full(db: Session = Depends(get_db)) -> FullHealthOut:
    report = pf.system_preflight(db)
    by_name = {c["name"]: c for c in report["checks"]}

    def passed(name: str) -> bool:
        return by_name.get(name, {}).get("status") == "ok"

    schema = by_name.get("schema_head", {}).get("status")
    schema_match = None if schema == "warn" else (schema == "ok")

    return FullHealthOut(
        status="ok" if report["ok"] else "degraded",
        ok=report["ok"],
        database=passed("db_connect"),
        redis=passed("redis_connect"),
        build_info=BuildInfoOut(**report["build_info"]),
        workers=report["workers"],
        worker_build_match=passed("worker_build_match"),
        schema_head_match=schema_match,
    )
