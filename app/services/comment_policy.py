"""Adaptive comment-refresh scheduling (Phase 4A scaffolding).

Computes when a video's comments should next be refreshed based on its age, and
selects the videos that are due. Designed to be callable from the scheduler
later, but for now driven manually via ``comments refresh-all``.

Intervals (by video age):
  <= 7 days   -> daily
  <= 30 days  -> every 3 days
  <= 180 days -> weekly
  > 180 days  -> monthly
  frozen (comments_disabled / unavailable / frozen) -> never
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Video

FROZEN_STATES = ("comments_disabled", "unavailable", "frozen")
LIVE_CHAT_FROZEN_STATES = ("frozen", "unavailable")


def _upload_datetime(video: Video) -> datetime | None:
    ud = video.upload_date
    if ud and len(ud) == 8 and ud.isdigit():
        try:
            return datetime(int(ud[:4]), int(ud[4:6]), int(ud[6:8]))
        except ValueError:
            return None
    return None


def is_frozen(video: Video) -> bool:
    return (video.comments_state or "") in FROZEN_STATES


def compute_next_comment_refresh(video: Video, now: datetime) -> datetime | None:
    """Next refresh time, or None if the video is frozen (never auto-refresh)."""
    if is_frozen(video):
        return None
    up = _upload_datetime(video)
    if up is None:
        interval = timedelta(days=7)
    else:
        age_days = (now - up).days
        if age_days <= 7:
            interval = timedelta(days=1)
        elif age_days <= 30:
            interval = timedelta(days=3)
        elif age_days <= 180:
            interval = timedelta(days=7)
        else:
            interval = timedelta(days=30)
    return now + interval


def select_due_videos(session: Session, now: datetime, limit: int | None = None) -> list[Video]:
    """Videos due for comment refresh (never refreshed first, then overdue)."""
    stmt = (
        select(Video)
        .where(
            or_(Video.comments_state.is_(None), Video.comments_state.notin_(FROZEN_STATES)),
            or_(
                Video.next_comments_refresh_at.is_(None),
                Video.next_comments_refresh_at <= now,
            ),
        )
        .order_by(
            Video.next_comments_refresh_at.is_(None).desc(),
            Video.next_comments_refresh_at.asc(),
            Video.id.asc(),
        )
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def select_refreshable_videos(
    session: Session, now: datetime, limit: int | None = None, *, due_only: bool = True
) -> list[Video]:
    """Refreshable videos. ``due_only`` uses next_comments_refresh_at; otherwise
    every non-frozen video (used by ``comments refresh-all --all``)."""
    stmt = select(Video).where(
        or_(Video.comments_state.is_(None), Video.comments_state.notin_(FROZEN_STATES))
    )
    if due_only:
        stmt = stmt.where(
            or_(
                Video.next_comments_refresh_at.is_(None),
                Video.next_comments_refresh_at <= now,
            )
        )
    stmt = stmt.order_by(
        Video.next_comments_refresh_at.is_(None).desc(),
        Video.next_comments_refresh_at.asc(),
        Video.id.asc(),
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def count_frozen(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count(Video.id)).where(Video.comments_state.in_(FROZEN_STATES))
        )
        or 0
    )


def count_recent(session: Session, now: datetime) -> int:
    """Videos not yet due (next_comments_refresh_at in the future)."""
    return int(
        session.scalar(
            select(func.count(Video.id)).where(
                or_(
                    Video.comments_state.is_(None),
                    Video.comments_state.notin_(FROZEN_STATES),
                ),
                Video.next_comments_refresh_at.is_not(None),
                Video.next_comments_refresh_at > now,
            )
        )
        or 0
    )


def apply_comment_backoff(video: Video, now: datetime, settings: Settings) -> datetime:
    """On HTTP 429: bump failure count and back off ``next_comments_refresh_at``.

    After ``COMMENTS_REFRESH_MAX_RETRY`` consecutive failures, back off at least
    one day so we stop hammering a persistently rate-limited video.
    """
    video.comment_refresh_failures = (video.comment_refresh_failures or 0) + 1
    backoff = settings.comments_refresh_retry_backoff_seconds or 21600
    if video.comment_refresh_failures >= (settings.comments_refresh_max_retry or 5):
        backoff = max(backoff, 86400)
    nxt = now + timedelta(seconds=backoff)
    video.next_comments_refresh_at = nxt
    return nxt


# --------------------------------------------------------------------------- #
# Live chat scheduling (Phase 4B)
# --------------------------------------------------------------------------- #
def compute_next_live_chat_refresh(
    video: Video, now: datetime, settings: Settings
) -> datetime | None:
    if (video.live_chat_state or "") in LIVE_CHAT_FROZEN_STATES:
        return None
    return now + timedelta(seconds=settings.live_chat_refresh_interval_seconds or 2592000)


def select_due_live_chat_videos(
    session: Session, now: datetime, limit: int | None = None
) -> list[Video]:
    """Videos known to have (or to be) live, due for a live-chat refresh."""
    stmt = (
        select(Video)
        .where(
            or_(Video.has_live_chat.is_(True), Video.is_live.is_(True)),
            or_(
                Video.live_chat_state.is_(None),
                Video.live_chat_state.notin_(LIVE_CHAT_FROZEN_STATES),
            ),
            or_(
                Video.next_live_chat_refresh_at.is_(None),
                Video.next_live_chat_refresh_at <= now,
            ),
        )
        .order_by(
            Video.next_live_chat_refresh_at.is_(None).desc(),
            Video.next_live_chat_refresh_at.asc(),
            Video.id.asc(),
        )
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def classify_comment_state(stderr_text: str | None) -> str | None:
    """Best-effort classification of comment-fetch failures from yt-dlp stderr."""
    if not stderr_text:
        return None
    t = stderr_text
    low = t.lower()
    if "comments are turned off" in low or "comments are disabled" in low or "コメントは無効" in t:
        return "comments_disabled"
    if (
        "video is private" in low
        or "video unavailable" in low
        or "has been removed" in low
        or "account associated with this video has been terminated" in low
        or "members-only" in low
        or "メンバー限定" in t
    ):
        return "unavailable"
    return None
