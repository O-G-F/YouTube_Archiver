"""Subscription read + enqueue endpoints (Phase 3B).

Subscriptions are stored as ``collections`` with ``type=channel`` (disabled,
crawl_policy=manual). Enqueue turns selected subscriptions into expand jobs for
the chosen tabs (reusing the Phase 2A/2B channel-expand path).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import get_settings
from app.models import Collection
from app.schemas import (
    SubscriptionEnqueueOut,
    SubscriptionEnqueueRequest,
    SubscriptionOut,
)
from app.services import jobs as jobs_svc
from app.services.profiles import get_profile_spec
from app.services.urls import UrlError, channel_tab_url, normalize_url

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])


@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(
    db: Session = Depends(get_db),
    limit: int = Query(default=200, le=2000),
    offset: int = Query(default=0, ge=0),
) -> list[SubscriptionOut]:
    rows = list(
        db.scalars(
            select(Collection)
            .where(Collection.type == "channel")
            .order_by(Collection.id)
            .limit(limit)
            .offset(offset)
        )
    )
    return [
        SubscriptionOut(
            id=c.id,
            channel_id=c.youtube_channel_id,
            channel_title=c.title,
            url=c.url,
            enabled=c.enabled,
        )
        for c in rows
    ]


@router.post("/enqueue", response_model=SubscriptionEnqueueOut)
def enqueue_subscriptions(
    req: SubscriptionEnqueueRequest, db: Session = Depends(get_db)
) -> SubscriptionEnqueueOut:
    profile = req.profile or get_settings().default_profile
    try:
        get_profile_spec(db, profile)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown profile: {profile!r}")

    tabs = [
        t for t, on in (("videos", req.videos), ("shorts", req.shorts), ("streams", req.streams)) if on
    ]
    if not tabs:
        raise HTTPException(
            status_code=400, detail="specify at least one of videos / shorts / streams"
        )

    stmt = select(Collection).where(Collection.type == "channel").order_by(Collection.id)
    if req.limit:
        stmt = stmt.limit(req.limit)
    subs = list(db.scalars(stmt))

    job_ids: list[int] = []
    for c in subs:
        url = c.url or (
            f"https://www.youtube.com/channel/{c.youtube_channel_id}"
            if c.youtube_channel_id
            else None
        )
        if not url:
            continue
        try:
            parsed = normalize_url(url)
        except UrlError:
            continue
        for tab in tabs:
            job = jobs_svc.create_and_submit(
                db, channel_tab_url(parsed, tab), profile, max_items=req.max_items
            )
            job_ids.append(job.id)
    return SubscriptionEnqueueOut(
        channels=len(subs), jobs_created=len(job_ids), job_ids=job_ids
    )
