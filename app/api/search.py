"""Cross-entity search (Phase 5B).

ILIKE/LIKE over videos, comments, live chat, and collections. Privacy: never
returns raw_json; comment/live-chat hits return only a short text snippet (and
the already-displayed author name) plus a link target.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Collection, Comment, LiveChatMessage, Video
from app.schemas import SearchOut, SearchResultOut

router = APIRouter(tags=["search"])

_ALL_TYPES = ("video", "comment", "live_chat", "collection")


def _snippet(text: str | None, n: int = 160) -> str | None:
    if not text:
        return None
    t = " ".join(text.split())
    return t if len(t) <= n else t[:n] + "…"


@router.get("/api/search", response_model=SearchOut)
def search(
    db: Session = Depends(get_db),
    q: str = Query(..., min_length=1, description="search text"),
    types: str | None = Query(default=None, description="csv of video,comment,live_chat,collection"),
    limit: int = Query(default=20, ge=1, le=100, description="max results per type"),
) -> SearchOut:
    wanted = (
        [t.strip() for t in types.split(",") if t.strip() in _ALL_TYPES]
        if types
        else list(_ALL_TYPES)
    )
    like = f"%{q}%"
    results: list[SearchResultOut] = []

    if "video" in wanted:
        for v in db.scalars(
            select(Video)
            .where(
                or_(
                    Video.title.ilike(like),
                    Video.channel_title.ilike(like),
                    Video.youtube_video_id.ilike(like),
                )
            )
            .order_by(Video.first_seen_at.desc())
            .limit(limit)
        ):
            results.append(
                SearchResultOut(
                    type="video",
                    title=v.title or v.youtube_video_id,
                    snippet=None,
                    video_id=v.id,
                    youtube_video_id=v.youtube_video_id,
                    extra=v.channel_title,
                )
            )

    if "comment" in wanted:
        for c, v in db.execute(
            select(Comment, Video)
            .join(Video, Video.id == Comment.video_id)
            .where(Comment.text.ilike(like))
            .order_by(Comment.like_count.desc().nullslast())
            .limit(limit)
        ).all():
            results.append(
                SearchResultOut(
                    type="comment",
                    title=v.title or v.youtube_video_id,
                    snippet=_snippet(c.text),
                    video_id=v.id,
                    youtube_video_id=v.youtube_video_id,
                    author_name=c.author_name,
                    extra=f"♥ {c.like_count or 0}",
                )
            )

    if "live_chat" in wanted:
        for m, v in db.execute(
            select(LiveChatMessage, Video)
            .join(Video, Video.id == LiveChatMessage.video_id)
            .where(LiveChatMessage.message.ilike(like))
            .order_by(LiveChatMessage.id.desc())
            .limit(limit)
        ).all():
            results.append(
                SearchResultOut(
                    type="live_chat",
                    title=v.title or v.youtube_video_id,
                    snippet=_snippet(m.message),
                    video_id=v.id,
                    youtube_video_id=v.youtube_video_id,
                    author_name=m.author_name,
                    extra=m.amount_text or m.message_type,
                )
            )

    if "collection" in wanted:
        for c in db.scalars(
            select(Collection)
            .where(or_(Collection.title.ilike(like), Collection.url.ilike(like)))
            .order_by(Collection.id.desc())
            .limit(limit)
        ):
            results.append(
                SearchResultOut(
                    type="collection",
                    title=c.title or c.url,
                    snippet=None,
                    collection_id=c.id,
                    extra=c.type,
                )
            )

    return SearchOut(query=q, total=len(results), results=results)
