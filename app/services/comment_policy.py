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

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Video

FROZEN_STATES = ("comments_disabled", "unavailable", "frozen")


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
