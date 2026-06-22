"""System build identity + full health (Phase 6F).

``/api/system/build-info`` reports this (web) process's build identity.
``/api/system/health/full`` adds DB/Redis reachability, the live worker
heartbeats, and whether web and worker share a build_id (stale-worker guard).

No secrets / host absolute paths are returned.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.schemas import BuildInfoOut, FullHealthOut, SecretsStatusOut
from app.services import build_info as bi
from app.services import preflight as pf

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/build-info", response_model=BuildInfoOut)
def build_info() -> BuildInfoOut:
    return BuildInfoOut(**bi.build_info())


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
