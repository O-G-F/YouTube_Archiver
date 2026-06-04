"""Live chat parsing & normalization (Phase 4B).

yt-dlp writes archived live chat as a ``<id>.live_chat.json`` file — a JSONL
stream (one JSON object per line) of YouTube replay chat actions. This module
parses the common renderers (text / super chat / super sticker / membership)
and upserts them into ``live_chat_messages`` with diff detection.

Personal data: ``raw_json``/author info is handled carefully (API hides raw_json
by default).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LiveChatMessage, Video, utcnow


@dataclass
class ChatMessage:
    message_id: str
    author_name: str | None = None
    author_channel_id: str | None = None
    text: str | None = None
    timestamp_ms: int | None = None
    time_text: str | None = None
    amount_text: str | None = None
    amount_value: float | None = None
    currency: str | None = None
    message_type: str = "text"  # text | paid | sticker | membership
    is_superchat: bool = False
    is_member_message: bool = False
    published_at: datetime | None = None
    raw: dict = field(default_factory=dict)


_RENDERERS = {
    "liveChatTextMessageRenderer": "text",
    "liveChatPaidMessageRenderer": "paid",
    "liveChatPaidStickerRenderer": "sticker",
    "liveChatMembershipItemRenderer": "membership",
}

_CURRENCY_SYMBOLS = (
    ("R$", "BRL"), ("A$", "AUD"), ("CA$", "CAD"), ("NT$", "TWD"), ("HK$", "HKD"),
    ("MX$", "MXN"), ("¥", "JPY"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"),
    ("₩", "KRW"), ("₹", "INR"), ("₱", "PHP"), ("฿", "THB"), ("₫", "VND"),
)


def _runs_text(runs: list | None) -> str | None:
    if not runs:
        return None
    parts: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if "text" in run:
            parts.append(str(run["text"]))
        elif "emoji" in run:
            emoji = run["emoji"] or {}
            shortcuts = emoji.get("shortcuts") or []
            parts.append(shortcuts[0] if shortcuts else (emoji.get("emojiId") or ""))
    out = "".join(parts).strip()
    return out or None


def _dt_from_usec(usec) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(usec) / 1_000_000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def _parse_amount(amount_text: str | None) -> tuple[float | None, str | None]:
    if not amount_text:
        return None, None
    currency = None
    iso = re.match(r"^\s*([A-Z]{2,3})\b", amount_text)
    if iso:
        currency = iso.group(1)
    else:
        for sym, code in _CURRENCY_SYMBOLS:
            if sym in amount_text:
                currency = code
                break
    value = None
    nums = re.findall(r"[\d][\d.,]*", amount_text)
    if nums:
        raw = nums[0]
        if "," in raw and "." in raw:
            if raw.rfind(",") > raw.rfind("."):  # European: 1.234,56
                raw = raw.replace(".", "").replace(",", ".")
            else:
                raw = raw.replace(",", "")
        elif "," in raw:
            raw = raw.replace(",", ".") if re.search(r",\d{2}$", raw) else raw.replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            value = None
    return value, currency


def _has_member_badge(renderer: dict) -> bool:
    for badge in renderer.get("authorBadges") or []:
        b = (badge or {}).get("liveChatAuthorBadgeRenderer") or {}
        tooltip = (b.get("tooltip") or "").lower()
        if "member" in tooltip or "メンバー" in (b.get("tooltip") or ""):
            return True
    return False


def _extract(item: dict) -> ChatMessage | None:
    for key, mtype in _RENDERERS.items():
        renderer = item.get(key)
        if renderer:
            break
    else:
        return None
    mid = renderer.get("id")
    if not mid:
        return None
    text = _runs_text((renderer.get("message") or {}).get("runs"))
    if mtype == "membership" and not text:
        text = _runs_text((renderer.get("headerSubtext") or {}).get("runs")) or _runs_text(
            (renderer.get("headerPrimaryText") or {}).get("runs")
        )
    ts_usec = renderer.get("timestampUsec")
    amount_text = (renderer.get("purchaseAmountText") or {}).get("simpleText")
    amount_value, currency = _parse_amount(amount_text)
    return ChatMessage(
        message_id=mid,
        author_name=(renderer.get("authorName") or {}).get("simpleText"),
        author_channel_id=renderer.get("authorExternalChannelId"),
        text=text,
        timestamp_ms=(int(ts_usec) // 1000) if ts_usec and str(ts_usec).lstrip("-").isdigit() else None,
        time_text=(renderer.get("timestampText") or {}).get("simpleText"),
        amount_text=amount_text,
        amount_value=amount_value,
        currency=currency,
        message_type=mtype,
        is_superchat=mtype in ("paid", "sticker"),
        is_member_message=(mtype == "membership") or _has_member_badge(renderer),
        published_at=_dt_from_usec(ts_usec),
        raw=renderer,
    )


def parse_live_chat_jsonl(text: str) -> Iterator[ChatMessage]:
    """Parse a yt-dlp ``.live_chat.json`` (JSONL) stream into ChatMessages."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        replay = obj.get("replayChatItemAction")
        actions = (replay.get("actions") if replay else None) or (
            [obj] if "addChatItemAction" in obj else []
        )
        for action in actions:
            add = (action or {}).get("addChatItemAction")
            if not add:
                continue
            item = add.get("item") or {}
            msg = _extract(item)
            if msg is not None:
                yield msg


def ingest_live_chat(
    session: Session,
    video: Video,
    messages: Iterator[ChatMessage] | list[ChatMessage],
    *,
    snapshot_id: int | None = None,
    limit: int | None = None,
    mark_missing: bool = False,
) -> dict:
    """Upsert chat messages into ``live_chat_messages`` (dedup by message_id)."""
    existing = {
        m.message_id: m
        for m in session.scalars(
            select(LiveChatMessage).where(LiveChatMessage.video_id == video.id)
        )
        if m.message_id
    }
    now = utcnow()
    summary = {"fetched": 0, "new": 0, "updated": 0, "refound": 0,
               "marked_missing": 0, "unchanged": 0, "capped": False}
    seen: set[str] = set()
    processed = 0
    for cm in messages:
        if limit is not None and limit > 0 and processed >= limit:
            summary["capped"] = True
            break
        processed += 1
        summary["fetched"] += 1
        mid = cm.message_id
        if not mid:
            continue
        seen.add(mid)
        row = existing.get(mid)
        if row is None:
            row = LiveChatMessage(video_id=video.id, message_id=mid)
            session.add(row)
            summary["new"] += 1
        else:
            was_missing = bool(row.is_deleted_or_missing)
            changed = (row.message != cm.text) or was_missing
            if was_missing:
                summary["refound"] += 1
            if changed:
                summary["updated"] += 1
            else:
                summary["unchanged"] += 1
        row.author_name = cm.author_name
        row.author_channel_id = cm.author_channel_id
        row.message = cm.text
        row.timestamp_ms = cm.timestamp_ms
        row.time_text = cm.time_text
        row.amount = cm.amount_value
        row.amount_text = cm.amount_text
        row.currency = cm.currency
        row.message_type = cm.message_type
        row.is_superchat = cm.is_superchat
        row.is_member_message = cm.is_member_message
        row.published_at = cm.published_at
        row.fetched_at = now
        row.is_deleted_or_missing = False
        row.raw_json = cm.raw

    if mark_missing and not summary["capped"]:
        for mid, row in existing.items():
            if mid not in seen and not row.is_deleted_or_missing:
                row.is_deleted_or_missing = True
                summary["marked_missing"] += 1

    session.flush()
    return summary
