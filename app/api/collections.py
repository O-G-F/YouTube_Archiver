"""Collection (playlist/channel) read endpoints (Phase 2A)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.models import Collection, CollectionItem, DownloadProfile, Job
from app.schemas import (
    CollectionItemOut,
    CollectionOut,
    CollectionPatch,
    JobOut,
    RefreshAllResult,
)
from app.services import jobs as jobs_svc

router = APIRouter(prefix="/api/collections", tags=["collections"])

_VALID_POLICIES = ("manual", "new_only", "refresh")


def _item_count(db: Session, collection_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(CollectionItem.id)).where(
                CollectionItem.collection_id == collection_id
            )
        )
        or 0
    )


@router.get("", response_model=list[CollectionOut])
def list_collections(
    db: Session = Depends(get_db),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[CollectionOut]:
    rows = list(
        db.scalars(
            select(Collection).order_by(Collection.id.desc()).limit(limit).offset(offset)
        )
    )
    out: list[CollectionOut] = []
    for c in rows:
        co = CollectionOut.model_validate(c)
        co.item_count = _item_count(db, c.id)
        out.append(co)
    return out


@router.get("/{collection_id}", response_model=CollectionOut)
def get_collection(collection_id: int, db: Session = Depends(get_db)) -> CollectionOut:
    c = db.get(Collection, collection_id)
    if c is None:
        raise HTTPException(status_code=404, detail="collection not found")
    co = CollectionOut.model_validate(c)
    co.item_count = _item_count(db, c.id)
    return co


@router.get("/{collection_id}/items", response_model=list[CollectionItemOut])
def list_collection_items(
    collection_id: int,
    db: Session = Depends(get_db),
    include_removed: bool = Query(default=False),
    limit: int = Query(default=200, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[CollectionItem]:
    if db.get(Collection, collection_id) is None:
        raise HTTPException(status_code=404, detail="collection not found")
    stmt = select(CollectionItem).where(CollectionItem.collection_id == collection_id)
    if not include_removed:
        stmt = stmt.where(CollectionItem.removed_at.is_(None))
    stmt = stmt.order_by(CollectionItem.position, CollectionItem.id).limit(limit).offset(offset)
    return list(db.scalars(stmt))


# --------------------------------------------------------------------------- #
# Re-crawl / management (Phase 2B)
# --------------------------------------------------------------------------- #
def _profile_name(db: Session, collection: Collection) -> str:
    if collection.download_profile_id:
        prow = db.get(DownloadProfile, collection.download_profile_id)
        if prow is not None:
            return prow.name
    return get_settings().default_profile


@router.post("/refresh-all", response_model=RefreshAllResult)
def refresh_all(db: Session = Depends(get_db)) -> RefreshAllResult:
    from app.services.scheduler import run_once

    summary = run_once(get_settings(), reason="manual_refresh_all")
    return RefreshAllResult(
        collections_checked=summary["collections_checked"],
        jobs_created=summary["jobs_created"],
        job_ids=summary["job_ids"],
    )


@router.post("/{collection_id}/refresh", response_model=JobOut, status_code=201)
def refresh_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    max_items: int | None = Query(default=None),
) -> Job:
    c = db.get(Collection, collection_id)
    if c is None:
        raise HTTPException(status_code=404, detail="collection not found")
    return jobs_svc.create_and_submit(
        db,
        c.url,
        _profile_name(db, c),
        max_items=max_items,
        extra_meta={
            "scheduled_by": "manual_refresh",
            "crawl_policy": c.crawl_policy or "new_only",
            "detect_removed": True,
            "collection_id": c.id,
        },
    )


@router.post("/{collection_id}/enable", response_model=CollectionOut)
def enable_collection(collection_id: int, db: Session = Depends(get_db)) -> CollectionOut:
    return _set_enabled(db, collection_id, True)


@router.post("/{collection_id}/disable", response_model=CollectionOut)
def disable_collection(collection_id: int, db: Session = Depends(get_db)) -> CollectionOut:
    return _set_enabled(db, collection_id, False)


@router.patch("/{collection_id}", response_model=CollectionOut)
def patch_collection(
    collection_id: int, patch: CollectionPatch, db: Session = Depends(get_db)
) -> CollectionOut:
    c = db.get(Collection, collection_id)
    if c is None:
        raise HTTPException(status_code=404, detail="collection not found")
    if patch.enabled is not None:
        c.enabled = patch.enabled
    if patch.crawl_policy is not None:
        if patch.crawl_policy not in _VALID_POLICIES:
            raise HTTPException(
                status_code=400,
                detail=f"crawl_policy must be one of {_VALID_POLICIES}",
            )
        c.crawl_policy = patch.crawl_policy
    if patch.profile is not None:
        prow = db.scalar(
            select(DownloadProfile).where(DownloadProfile.name == patch.profile)
        )
        if prow is None:
            raise HTTPException(status_code=400, detail=f"unknown profile: {patch.profile!r}")
        c.download_profile_id = prow.id
    db.flush()
    out = CollectionOut.model_validate(c)
    out.item_count = _item_count(db, c.id)
    return out


def _set_enabled(db: Session, collection_id: int, enabled: bool) -> CollectionOut:
    c = db.get(Collection, collection_id)
    if c is None:
        raise HTTPException(status_code=404, detail="collection not found")
    c.enabled = enabled
    db.flush()
    out = CollectionOut.model_validate(c)
    out.item_count = _item_count(db, c.id)
    return out
