"""Google Takeout parsing & import (Phase 3A).

Reads a Takeout ZIP in-memory (no extraction to disk), classifies the
YouTube-related members, and imports watch history into ``watch_history_events``.

Robustness notes:
  - Takeout ZIP filenames are UTF-8 but usually lack the UTF-8 flag bit, so they
    must be re-decoded from cp437 (``fix_zip_name``). Localized names differ by
    language, so files are classified by **content**, not filename, where possible.
  - The ZIP path is restricted to ``TAKEOUT_IMPORT_ROOT`` and member paths are
    checked for zip-slip (we never extract, but guard anyway).

Parsers are modular (``parse_watch_history_json`` / ``parse_watch_history_html``)
so new formats can be added later. Only watch history is persisted in Phase 3A;
search / likes / subscriptions / playlists are detected & counted for preview.
"""

from __future__ import annotations

import csv
import html as _html
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_setup import get_logger
from app.models import (
    Collection,
    CollectionItem,
    Job,
    LikedVideo,
    SearchHistoryEvent,
    Source,
    TakeoutImportSession,
    Video,
    WatchHistoryEvent,
    utcnow,
)
from app.services.urls import canonical_video_url, extract_video_id, is_video_id

logger = get_logger(__name__)


class TakeoutError(Exception):
    """Raised for bad paths, unsafe members, or unreadable archives."""


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class TakeoutFile:
    name: str        # decoded (UTF-8) member path
    kind: str        # watch_history|search_history|subscriptions|playlist|
                     # playlists_index|likes|unknown
    format: str      # json|html|csv|other
    size: int


@dataclass
class WatchEvent:
    youtube_video_id: str | None
    title: str | None
    channel_title: str | None
    watched_at: datetime | None
    raw: dict = field(default_factory=dict)


@dataclass
class SearchEvent:
    query: str | None
    searched_at: datetime | None
    raw: dict = field(default_factory=dict)


@dataclass
class Subscription:
    channel_id: str | None
    channel_url: str | None
    channel_title: str | None


@dataclass
class PlaylistItem:
    youtube_video_id: str
    position: int
    added_at: datetime | None


@dataclass
class TakeoutPlaylist:
    title: str
    playlist_id: str | None
    file_name: str
    items: list[PlaylistItem] = field(default_factory=list)


@dataclass
class LikedVideoEntry:
    youtube_video_id: str | None
    title: str | None
    channel_title: str | None
    url: str | None
    liked_at: datetime | None
    channel_id: str | None = None
    source: str = "takeout_youtube"  # takeout_youtube | takeout_my_activity
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Filename / path helpers
# --------------------------------------------------------------------------- #
def fix_zip_name(info: zipfile.ZipInfo) -> str:
    """Recover the real UTF-8 member name (Takeout often omits the UTF-8 flag)."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def resolve_takeout_path(settings: Settings, path: str) -> Path:
    """Resolve & validate a Takeout ZIP path under TAKEOUT_IMPORT_ROOT."""
    root = settings.takeout_import_root.resolve()
    p = Path(path)
    candidate = (p if p.is_absolute() else root / p).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise TakeoutError(
            f"path must be inside TAKEOUT_IMPORT_ROOT ({root})"
        )
    if not candidate.is_file():
        raise TakeoutError(f"file not found: {candidate}")
    if candidate.suffix.lower() != ".zip":
        raise TakeoutError("not a .zip file")
    return candidate


def _is_unsafe_member(name: str) -> bool:
    return name.startswith("/") or ".." in Path(name).parts


def _clip(value: str | None, maxlen: int) -> str | None:
    """Clip a string to a column's max length (PostgreSQL enforces VARCHAR(N);
    SQLite does not). Real Takeout titles can exceed 1024 chars, so importers
    clip title/channel_title/query to avoid a StringDataRightTruncation that
    would fail an entire INSERT batch."""
    if value is None:
        return None
    return value if len(value) <= maxlen else value[:maxlen]


# --------------------------------------------------------------------------- #
# Parsers (modular)
# --------------------------------------------------------------------------- #
_WATCH_PREFIXES = ("Watched ",)
_WATCH_SUFFIX_RE = re.compile(r"\s*(を視聴しました|を視聴)\s*$")
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_watch_title(title: str | None) -> str | None:
    if not title:
        return None
    t = title.strip()
    for pref in _WATCH_PREFIXES:
        if t.startswith(pref):
            t = t[len(pref):]
            break
    t = _WATCH_SUFFIX_RE.sub("", t).strip()
    return t or None


def _parse_iso_time(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_watch_history_json(entries: list[dict]) -> Iterator[WatchEvent]:
    for e in entries:
        if not isinstance(e, dict):
            continue
        url = e.get("titleUrl") or ""
        vid = extract_video_id(url) if url else None
        subtitles = e.get("subtitles") or []
        channel = subtitles[0].get("name") if subtitles and isinstance(subtitles[0], dict) else None
        yield WatchEvent(
            youtube_video_id=vid,
            title=_clean_watch_title(e.get("title")),
            channel_title=channel,
            watched_at=_parse_iso_time(e.get("time")),
            raw=e,
        )


_HTML_VIDEO_RE = re.compile(
    r'href="(https?://[^"]*?(?:watch\?v=|youtu\.be/|/shorts/)[^"]*)"[^>]*>(.*?)</a>',
    re.S,
)
_HTML_CHANNEL_RE = re.compile(
    r'href="https?://[^"]*?/(?:channel/|@|c/|user/)[^"]*"[^>]*>(.*?)</a>', re.S
)
_HTML_TIME_RES = [
    re.compile(r"(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2})"),
    re.compile(r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M)"),
]
_HTML_TIME_FORMATS = ["%Y/%m/%d %H:%M:%S", "%b %d, %Y, %I:%M:%S %p"]


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s).replace("&amp;", "&").strip()


def _parse_html_time(chunk: str) -> datetime | None:
    for rx, fmt in zip(_HTML_TIME_RES, _HTML_TIME_FORMATS):
        m = rx.search(chunk)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt)
            except ValueError:
                continue
    return None


_SEARCH_PREFIXES = ("Searched for ",)
_SEARCH_SUFFIX_RE = re.compile(r"\s*(を検索しました|を検索)\s*$")
_UC_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def _clean_search_query(title: str | None, url: str | None) -> str | None:
    if title:
        t = title.strip()
        for pref in _SEARCH_PREFIXES:
            if t.startswith(pref):
                t = t[len(pref):]
                break
        t = _SEARCH_SUFFIX_RE.sub("", t).strip()
        if t:
            return t[:512]
    if url:
        q = parse_qs(urlparse(url).query).get("search_query")
        if q and q[0]:
            return unquote(q[0])[:512]
    return None


def parse_search_history_json(entries: list[dict]) -> Iterator[SearchEvent]:
    for e in entries:
        if not isinstance(e, dict):
            continue
        yield SearchEvent(
            query=_clean_search_query(e.get("title"), e.get("titleUrl")),
            searched_at=_parse_iso_time(e.get("time")),
            raw=e,
        )


def parse_subscriptions_csv(text: str) -> Iterator[Subscription]:
    rows = list(csv.reader(io.StringIO(text)))
    for row in rows[1:]:  # skip header
        if not row or not any(c.strip() for c in row):
            continue
        cells = [c.strip() for c in row]
        cid = cells[0] if cells and _UC_RE.match(cells[0]) else None
        if cid is None:
            cid = next((c for c in cells if _UC_RE.match(c)), None)
        url = next((c for c in cells if c.lower().startswith("http")), None)
        title = cells[2] if len(cells) >= 3 else None
        if cid and not url:
            url = f"https://www.youtube.com/channel/{cid}"
        if cid or title:
            yield Subscription(channel_id=cid, channel_url=url, channel_title=title or None)


def _playlist_title_from_filename(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    base = re.sub(r"\.csv$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"\s*の動画$", "", base)            # ja: "<title> の動画"
    base = re.sub(r"[-\s]videos$", "", base, flags=re.IGNORECASE)  # en: "<title>-videos"
    return base.strip()


def parse_playlist_items_csv(text: str) -> Iterator[PlaylistItem]:
    rows = list(csv.reader(io.StringIO(text)))
    pos = 0
    for row in rows[1:]:  # skip header
        if not row:
            continue
        vid = row[0].strip()
        if not is_video_id(vid):
            continue
        added = _parse_iso_time(row[1]) if len(row) > 1 else None
        yield PlaylistItem(youtube_video_id=vid, position=pos, added_at=added)
        pos += 1


def parse_playlist_index_csv(text: str) -> dict[str, str | None]:
    """Return {playlist_title: playlist_id} from the playlists index CSV."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {}
    header = rows[0]
    title_col = None
    for i, h in enumerate(header):
        if "タイトル" in h or "title" in h.lower():
            title_col = i
            break
    index: dict[str, str | None] = {}
    for row in rows[1:]:
        if not row:
            continue
        pid = row[0].strip() if row else None
        title = (
            row[title_col].strip()
            if title_col is not None and len(row) > title_col
            else None
        )
        if title:
            index[title] = pid or None
    return index


def _csv_header_col(header: list[str], *needles: str) -> int | None:
    for i, h in enumerate(header):
        hl = h.lower()
        if any(n in hl or n in h for n in needles):
            return i
    return None


def parse_liked_videos_csv(text: str) -> Iterator[LikedVideoEntry]:
    """Parse the Takeout "Liked videos" playlist CSV (Video ID + timestamp).

    Some exports add a title column; we use it when present. Otherwise only the
    video id + liked timestamp are available (title/channel come later from a
    metadata refresh).
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return
    header = rows[0]
    # The id column is usually col 0; fall back to scanning.
    id_col = 0
    if not is_video_id((rows[1][0].strip() if len(rows) > 1 and rows[1] else "")):
        hc = _csv_header_col(header, "video id", "動画 id", "動画id")
        id_col = hc if hc is not None else 0
    ts_col = _csv_header_col(header, "timestamp", "time", "日時", "作成")
    title_col = _csv_header_col(header, "title", "タイトル")
    for row in rows[1:]:
        if not row or not any(c.strip() for c in row):
            continue
        vid = row[id_col].strip() if len(row) > id_col else ""
        if not is_video_id(vid):
            # tolerate a leading column shuffle: scan the row for an id
            vid = next((c.strip() for c in row if is_video_id(c.strip())), "")
            if not is_video_id(vid):
                continue
        liked_at = None
        if ts_col is not None and len(row) > ts_col:
            liked_at = _parse_iso_time(row[ts_col])
        if liked_at is None:  # try any cell that parses as a timestamp
            for c in row:
                liked_at = _parse_iso_time(c)
                if liked_at:
                    break
        title = row[title_col].strip() if title_col is not None and len(row) > title_col else None
        yield LikedVideoEntry(
            youtube_video_id=vid,
            title=title or None,
            channel_title=None,
            url=canonical_video_url(vid),
            liked_at=liked_at,
            raw={"video_id": vid, "liked_at": liked_at.isoformat() if liked_at else None},
        )


def parse_liked_videos_json(entries: list[dict]) -> Iterator[LikedVideoEntry]:
    """Best-effort JSON likes parser (watch-history-shaped entries)."""
    for e in entries:
        if not isinstance(e, dict):
            continue
        url = e.get("titleUrl") or e.get("url") or ""
        vid = extract_video_id(url) if url else None
        subtitles = e.get("subtitles") or []
        channel = subtitles[0].get("name") if subtitles and isinstance(subtitles[0], dict) else None
        yield LikedVideoEntry(
            youtube_video_id=vid,
            title=_clean_watch_title(e.get("title")) or e.get("title"),
            channel_title=channel,
            url=canonical_video_url(vid) if vid else (url or None),
            liked_at=_parse_iso_time(e.get("time")),
            raw=e,
        )


def parse_liked_videos_html(html: str) -> Iterator[LikedVideoEntry]:
    """Best-effort HTML likes parser (Takeout 'outer-cell' blocks)."""
    for chunk in html.split('<div class="outer-cell')[1:]:
        mv = _HTML_VIDEO_RE.search(chunk)
        if not mv:
            continue
        url, title = mv.group(1), _strip_tags(mv.group(2))
        mc = _HTML_CHANNEL_RE.search(chunk)
        vid = extract_video_id(url)
        yield LikedVideoEntry(
            youtube_video_id=vid,
            title=title or None,
            channel_title=_strip_tags(mc.group(1)) if mc else None,
            url=canonical_video_url(vid) if vid else url,
            liked_at=_parse_html_time(chunk),
            raw={"url": url, "title": title},
        )


# --------------------------------------------------------------------------- #
# My Activity (liked videos) — markers are the single configuration point.
# (Adapted from the reference YouTube-Liked_Videos project.)
# --------------------------------------------------------------------------- #
LIKE_ACTIVITY_MARKERS: tuple[str, ...] = ("liked ", "liked:", "高く評価")
NON_LIKE_ACTIVITY_MARKERS: tuple[str, ...] = (
    "watched",
    "disliked",
    "unliked",
    "removed like",
    "removed a like",
    "低く評価",
    "低評価",
    "高評価を削除",
    "を視聴",
)
_CHANNEL_ID_RE = re.compile(r"(?:youtube\.com/(?:channel/)?|^)(UC[A-Za-z0-9_-]{22})")
# My Activity export path: "<Takeout>/<My Activity|マイ アクティビティ>/YouTube/...json"
_MY_ACTIVITY_YT_RE = re.compile(
    r"(?:My Activity|マイ ?アクティビティ)/YouTube/.*\.json$", re.IGNORECASE
)


def _is_like_activity(title: str) -> bool:
    low = title.lower()
    if any(m in low for m in NON_LIKE_ACTIVITY_MARKERS):
        return False
    return any(m in low for m in LIKE_ACTIVITY_MARKERS)


def _extract_channel_id(text: str | None) -> str | None:
    if not text:
        return None
    m = _CHANNEL_ID_RE.search(text)
    return m.group(1) if m else None


def _clean_activity_title(title: str) -> str | None:
    text = _html.unescape(title).strip()
    quoted = re.search(r"[「\"](.+?)[」\"]を?高く評価", text)
    if quoted:
        return quoted.group(1).strip() or None
    if text.lower().startswith("liked "):
        return text[6:].strip() or None
    for prefix in ("高く評価しました ", "高く評価 "):
        if text.startswith(prefix):
            return text[len(prefix):].strip() or None
    for suffix in ("を高く評価しました", "を高評価しました", "を高く評価", "を高評価"):
        if text.endswith(suffix):
            return text[: -len(suffix)].strip(" 「」") or None
    return text or None


def _iter_activity_dicts(value) -> Iterator[dict]:
    """Yield activity item dicts from arbitrarily-nested My Activity JSON."""
    if isinstance(value, dict):
        if "title" in value and ("titleUrl" in value or "titleURL" in value) and "time" in value:
            yield value
        for child in value.values():
            yield from _iter_activity_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_activity_dicts(child)


def _myactivity_item_to_liked(item: dict) -> LikedVideoEntry | None:
    """Convert one My Activity item dict to a LikedVideoEntry (or None)."""
    title = str(item.get("title") or "")
    title_url = str(item.get("titleUrl") or item.get("titleURL") or "")
    vid = extract_video_id(title_url) if title_url else None
    if not vid or not _is_like_activity(title):
        return None
    channel_title = None
    channel_id = None
    subs = item.get("subtitles") or []
    if isinstance(subs, list) and subs and isinstance(subs[0], dict):
        channel_title = subs[0].get("name") or None
        channel_id = _extract_channel_id(subs[0].get("url"))
    return LikedVideoEntry(
        youtube_video_id=vid,
        title=_clean_activity_title(title),
        channel_title=channel_title,
        url=canonical_video_url(vid),
        liked_at=_parse_iso_time(item.get("time")),
        channel_id=channel_id,
        source="takeout_my_activity",
        raw={"title": title, "titleUrl": title_url, "time": item.get("time"), "subtitles": subs},
    )


def parse_myactivity_liked_json(text: str) -> Iterator[LikedVideoEntry]:
    """Extract liked-video events from a My Activity YouTube JSON export (in-memory)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    for item in _iter_activity_dicts(data):
        entry = _myactivity_item_to_liked(item)
        if entry is not None:
            yield entry


def stream_myactivity_liked_json(fileobj) -> Iterator[LikedVideoEntry]:
    """Stream liked-video events from a My Activity YouTube JSON file object.

    Uses ijson to iterate the top-level array WITHOUT loading the whole document
    into memory (My Activity exports reach 90k+ activity items). Falls back to
    the in-memory parser if ijson is unavailable or the layout is unexpected.
    """
    try:
        import ijson
    except Exception:  # noqa: BLE001 - ijson missing -> fall back
        yield from parse_myactivity_liked_json(fileobj.read().decode("utf-8-sig", errors="replace"))
        return
    try:
        for item in ijson.items(fileobj, "item"):
            if isinstance(item, dict):
                entry = _myactivity_item_to_liked(item)
                if entry is not None:
                    yield entry
            else:
                yield from (e for e in (_myactivity_item_to_liked(d) for d in _iter_activity_dicts(item)) if e)
    except Exception as exc:  # noqa: BLE001 - malformed stream -> degrade gracefully
        logger.warning("my-activity stream parse failed (%s); falling back to in-memory", exc)
        try:
            fileobj.seek(0)
        except Exception:  # noqa: BLE001
            return
        yield from parse_myactivity_liked_json(fileobj.read().decode("utf-8-sig", errors="replace"))


def parse_watch_history_html(html: str) -> Iterator[WatchEvent]:
    """Best-effort HTML watch-history parser (Takeout 'outer-cell' blocks)."""
    chunks = html.split('<div class="outer-cell')
    for chunk in chunks[1:]:
        mv = _HTML_VIDEO_RE.search(chunk)
        if not mv:
            continue
        url, title = mv.group(1), _strip_tags(mv.group(2))
        mc = _HTML_CHANNEL_RE.search(chunk)
        channel = _strip_tags(mc.group(1)) if mc else None
        yield WatchEvent(
            youtube_video_id=extract_video_id(url),
            title=_clean_watch_title(title) or title or None,
            channel_title=channel,
            watched_at=_parse_html_time(chunk),
            raw={"url": url, "title": title, "channel": channel},
        )


def _classify_json_peek(head: str) -> str:
    # Takeout JSON escapes '=' as =, so match "watch?v" (not "watch?v=").
    watch = head.count("watch?v") + head.count("youtu.be/") + head.count("/shorts/")
    search = head.count("results?search_query") + head.count("/results?")
    if search > watch and search > 0:
        return "search_history"
    if watch > 0:
        return "watch_history"
    return "unknown"


def _is_likes_name(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    low = base.lower()
    return "高く評価" in base or "liked video" in low or "liked-video" in low or low.startswith("likes")


def _classify_csv(name: str, header: str) -> str:
    base = name.rsplit("/", 1)[-1]
    low = base.lower()
    # Match the subscriptions file by basename only (header-based matching
    # over-counts other channel CSVs that also carry a "Channel Id" column).
    if "登録チャンネル" in base or "subscription" in low:
        return "subscriptions"
    if "高く評価" in base or "liked" in low or "like videos" in low:
        return "likes"
    if base in ("再生リスト.csv", "playlists.csv"):
        return "playlists_index"
    if "/再生リスト/" in name or "/playlists/" in name.lower() or " の動画" in base or low.endswith("-videos.csv"):
        return "playlist"
    return "unknown"


# --------------------------------------------------------------------------- #
# Archive
# --------------------------------------------------------------------------- #
class TakeoutArchive:
    def __init__(self, path: Path):
        try:
            self._zip = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise TakeoutError(f"could not open zip: {exc}")
        self._members: dict[str, zipfile.ZipInfo] = {}
        for info in self._zip.infolist():
            if info.is_dir():
                continue
            self._members[fix_zip_name(info)] = info

    def __enter__(self) -> "TakeoutArchive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._zip.close()

    def _read(self, name: str) -> bytes:
        if _is_unsafe_member(name):
            raise TakeoutError(f"unsafe member path: {name}")
        info = self._members.get(name)
        if info is None:
            raise TakeoutError(f"member not found: {name}")
        return self._zip.read(info)

    def _read_text(self, name: str) -> str:
        return self._read(name).decode("utf-8-sig", errors="replace")

    def _open(self, name: str):
        """Open a member as a binary stream (for ijson streaming parsing)."""
        if _is_unsafe_member(name):
            raise TakeoutError(f"unsafe member path: {name}")
        info = self._members.get(name)
        if info is None:
            raise TakeoutError(f"member not found: {name}")
        return self._zip.open(info)

    def _stream_json_array(self, name: str) -> Iterator[dict]:
        """Yield top-level array dicts from a JSON member, streaming via ijson.

        Falls back to ``json.loads`` when ijson is unavailable / the member is
        not a top-level array.
        """
        try:
            import ijson

            with self._open(name) as fh:
                for item in ijson.items(fh, "item"):
                    if isinstance(item, dict):
                        yield item
            return
        except Exception as exc:  # noqa: BLE001 - degrade to in-memory
            logger.warning("stream parse of %s failed (%s); using json.loads", name, exc)
        data = json.loads(self._read_text(name))
        if isinstance(data, list):
            yield from (d for d in data if isinstance(d, dict))

    def list_files(self) -> list[TakeoutFile]:
        files: list[TakeoutFile] = []
        for name, info in sorted(self._members.items()):
            if "youtube" not in name.lower():
                continue
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext == "json":
                fmt = "json"
                kind = "likes" if _is_likes_name(name) else _classify_json_peek(
                    self._read(name)[:8192].decode("utf-8", errors="replace")
                )
            elif ext in ("html", "htm"):
                low = name.lower()
                if _is_likes_name(name):
                    kind = "likes"
                elif "watch" in low or "視聴" in name:
                    kind = "watch_history"
                elif "search" in low or "検索" in name:
                    kind = "search_history"
                else:
                    kind = "unknown"
                fmt = "html"
            elif ext == "csv":
                first_line = self._read_text(name).split("\n", 1)[0]
                kind, fmt = _classify_csv(name, first_line), "csv"
            else:
                kind, fmt = "unknown", "other"
            files.append(TakeoutFile(name=name, kind=kind, format=fmt, size=info.file_size))
        return files

    # ----- counts -----
    def _count_history(self, f: TakeoutFile) -> int:
        if f.format == "json":
            data = json.loads(self._read_text(f.name))
            return len(data) if isinstance(data, list) else 0
        if f.format == "html":
            return self._read_text(f.name).count('<div class="outer-cell')
        return 0

    def _count_csv_rows(self, f: TakeoutFile) -> int:
        text = self._read_text(f.name)
        rows = [r for r in csv.reader(io.StringIO(text)) if r and any(c.strip() for c in r)]
        return max(0, len(rows) - 1)  # minus header

    # ----- watch events -----
    def iter_watch_events(self) -> Iterator[WatchEvent]:
        for f in self.list_files():
            if f.kind != "watch_history":
                continue
            if f.format == "json":
                # stream the (potentially huge) watch-history.json array
                yield from parse_watch_history_json(self._stream_json_array(f.name))
            elif f.format == "html":
                yield from parse_watch_history_html(self._read_text(f.name))

    def iter_search_events(self) -> Iterator[SearchEvent]:
        for f in self.list_files():
            if f.kind != "search_history" or f.format != "json":
                continue
            yield from parse_search_history_json(self._stream_json_array(f.name))

    def iter_subscriptions(self) -> Iterator[Subscription]:
        for f in self.list_files():
            if f.kind != "subscriptions":
                continue
            yield from parse_subscriptions_csv(self._read_text(f.name))

    def _playlist_index(self) -> dict[str, str | None]:
        index: dict[str, str | None] = {}
        for f in self.list_files():
            if f.kind == "playlists_index":
                index.update(parse_playlist_index_csv(self._read_text(f.name)))
        return index

    def iter_playlists(self, *, limit_items: int | None = None) -> Iterator[TakeoutPlaylist]:
        index = self._playlist_index()
        for f in self.list_files():
            if f.kind != "playlist":
                continue
            title = _playlist_title_from_filename(f.name)
            items = list(parse_playlist_items_csv(self._read_text(f.name)))
            if limit_items is not None:
                items = items[:limit_items]
            yield TakeoutPlaylist(
                title=title, playlist_id=index.get(title), file_name=f.name, items=items
            )

    def my_activity_youtube_path(self) -> str | None:
        """The 'My Activity / YouTube' JSON member, if this is a My Activity export."""
        for name in self._members:
            if _MY_ACTIVITY_YT_RE.search(name):
                return name
        return None

    def has_youtube_takeout(self) -> bool:
        return any(
            ("YouTube と YouTube Music/" in n) or ("YouTube and YouTube Music/" in n)
            for n in self._members
        )

    def has_index_only(self) -> bool:
        names = list(self._members)
        return any(n.endswith("archive_browser.html") for n in names) and not (
            self.has_youtube_takeout() or self.my_activity_youtube_path()
        )

    def archive_kind(self) -> str:
        """Classify the archive: my_activity_takeout | youtube_takeout | takeout_index | unknown_takeout."""
        if self.my_activity_youtube_path():
            return "my_activity_takeout"
        if self.has_youtube_takeout():
            return "youtube_takeout"
        if any(n.endswith("archive_browser.html") for n in self._members):
            return "takeout_index"
        return "unknown_takeout"

    def liked_source_path(self) -> tuple[str, str | None]:
        """Return (source_kind, detected_path) for the liked-videos source in this archive."""
        ma = self.my_activity_youtube_path()
        if ma:
            return "takeout_my_activity", ma
        for f in self.list_files():
            if f.kind == "likes":
                return "takeout_youtube", f.name
        return self.archive_kind(), None

    def iter_liked_videos(self) -> Iterator[LikedVideoEntry]:
        # My Activity export: stream the YouTube activity JSON for liked events.
        ma = self.my_activity_youtube_path()
        if ma:
            with self._open(ma) as fh:
                yield from stream_myactivity_liked_json(fh)
            return
        # YouTube Takeout: the "Liked videos" playlist CSV (or json/html).
        for f in self.list_files():
            if f.kind != "likes":
                continue
            if f.format == "csv":
                yield from parse_liked_videos_csv(self._read_text(f.name))
            elif f.format == "json":
                data = json.loads(self._read_text(f.name))
                if isinstance(data, list):
                    yield from parse_liked_videos_json(data)
            elif f.format == "html":
                yield from parse_liked_videos_html(self._read_text(f.name))

    def preview(self, sample: int = 5) -> dict:
        files = self.list_files()
        counts = {
            "watch_history_count": 0,
            "search_history_count": 0,
            "likes_count": 0,
            "subscriptions_count": 0,
            "playlists_count": 0,
        }
        warnings: list[str] = []
        for f in files:
            try:
                if f.kind == "watch_history":
                    counts["watch_history_count"] += self._count_history(f)
                elif f.kind == "search_history":
                    counts["search_history_count"] += self._count_history(f)
                elif f.kind == "subscriptions":
                    counts["subscriptions_count"] += self._count_csv_rows(f)
                elif f.kind == "playlist":
                    counts["playlists_count"] += 1
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{f.name}: {exc}")

        samples: list[dict] = []
        search_samples: list[str] = []
        subscription_samples: list[dict] = []
        playlist_samples: list[dict] = []
        liked_samples: list[dict] = []
        try:
            for ev in self.iter_watch_events():
                samples.append(
                    {
                        "youtube_video_id": ev.youtube_video_id,
                        "title": ev.title,
                        "channel_title": ev.channel_title,
                        "watched_at": ev.watched_at.isoformat() if ev.watched_at else None,
                    }
                )
                if len(samples) >= sample:
                    break
            for sev in self.iter_search_events():
                if sev.query:
                    search_samples.append(sev.query)
                if len(search_samples) >= sample:
                    break
            for sub in self.iter_subscriptions():
                subscription_samples.append(
                    {"channel_id": sub.channel_id, "channel_title": sub.channel_title}
                )
                if len(subscription_samples) >= sample:
                    break
            for pl in self.iter_playlists():
                playlist_samples.append(
                    {"title": pl.title, "playlist_id": pl.playlist_id, "item_count": len(pl.items)}
                )
                if len(playlist_samples) >= sample:
                    break
            # Single pass: count ALL liked videos (My Activity JSON or YouTube
            # CSV) and keep the first `sample` as previews.
            liked_total = 0
            for lv in self.iter_liked_videos():
                liked_total += 1
                if len(liked_samples) < sample:
                    liked_samples.append(
                        {
                            "youtube_video_id": lv.youtube_video_id,
                            "title": lv.title,
                            "liked_at": lv.liked_at.isoformat() if lv.liked_at else None,
                        }
                    )
            counts["likes_count"] = liked_total
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"sample parse: {exc}")

        importable = {
            "watch_history": counts["watch_history_count"] > 0,
            "search_history": counts["search_history_count"] > 0,
            "subscriptions": counts["subscriptions_count"] > 0,
            "playlists": counts["playlists_count"] > 0,
            "liked_videos": counts["likes_count"] > 0,
        }
        liked_source_kind, liked_detected_path = self.liked_source_path()
        return {
            "archive_kind": self.archive_kind(),
            "liked_source_kind": liked_source_kind,
            "liked_detected_path": liked_detected_path,
            "files": [
                {"name": f.name, "kind": f.kind, "format": f.format, "size": f.size}
                for f in files
            ],
            **counts,
            "samples": samples,
            "search_samples": search_samples,
            "subscription_samples": subscription_samples,
            "playlist_samples": playlist_samples,
            "liked_samples": liked_samples,
            "importable": importable,
            "warnings": warnings,
        }

    def inspect(self) -> dict:
        """Lightweight structural classification (no full content parse)."""
        names = list(self._members)
        return {
            "archive_kind": self.archive_kind(),
            "has_youtube_takeout": self.has_youtube_takeout(),
            "my_activity_youtube_path": self.my_activity_youtube_path(),
            "has_index": any(n.endswith("archive_browser.html") for n in names),
            "member_count": len(names),
            "liked_source_kind": self.liked_source_path()[0],
            "liked_detected_path": self.liked_source_path()[1],
        }

    def registry(self) -> list[dict]:
        """Structured list of detected Takeout sources (Phase 6C deep inspect).

        Maps members to canonical source kinds. ``member`` is the in-ZIP path
        only (no host path); counts are omitted here to keep it cheap.
        """
        sources: list[dict] = []
        ma = self.my_activity_youtube_path()
        if ma:
            sources.append({"kind": "my_activity_youtube_json", "member": ma,
                            "format": "json", "import_kinds": ["liked_videos"]})
        for f in self.list_files():
            if f.kind == "watch_history":
                sources.append({"kind": f"youtube_watch_history_{f.format}", "member": f.name,
                                "format": f.format, "import_kinds": ["watch_history"]})
            elif f.kind == "search_history":
                sources.append({"kind": f"youtube_search_history_{f.format}", "member": f.name,
                                "format": f.format, "import_kinds": ["search_history"]})
            elif f.kind == "subscriptions":
                sources.append({"kind": f"youtube_subscriptions_{f.format}", "member": f.name,
                                "format": f.format, "import_kinds": ["subscriptions"]})
            elif f.kind in ("playlist", "playlists_index"):
                sources.append({"kind": "youtube_playlists", "member": f.name,
                                "format": f.format, "import_kinds": ["playlists"]})
            elif f.kind == "likes" and not ma:
                sources.append({"kind": f"youtube_liked_videos_{f.format}", "member": f.name,
                                "format": f.format, "import_kinds": ["liked_videos"]})
        if any(n.endswith("archive_browser.html") for n in self._members):
            sources.append({"kind": "takeout_index", "member": "archive_browser.html",
                            "format": "html", "import_kinds": []})
        return sources


def open_archive(path: Path) -> TakeoutArchive:
    return TakeoutArchive(path)


def discover(settings: Settings, *, deep: bool = False) -> list[dict]:
    """List ZIPs under TAKEOUT_IMPORT_ROOT with a structural classification.

    ``deep`` parses content for a liked-count hint (slower); otherwise only the
    member list is inspected (fast, no large-JSON parse).
    """
    root = settings.takeout_import_root.resolve()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*.zip"))[:500]:
        try:
            rp = p.resolve()
            rp.relative_to(root)
            if not rp.is_file():
                continue
            entry = {"name": str(rp.relative_to(root)), "size": rp.stat().st_size}
            with open_archive(rp) as a:
                entry.update(a.inspect())
                if deep and entry["liked_detected_path"]:
                    entry["liked_count"] = sum(1 for _ in a.iter_liked_videos())
            out.append(entry)
        except (OSError, ValueError, TakeoutError):
            out.append({"name": p.name, "archive_kind": "unknown_takeout", "error": True})
    return out


# --------------------------------------------------------------------------- #
# Import (watch_history_events)
# --------------------------------------------------------------------------- #
def _dedup_key(vid: str | None, title: str | None, watched_at: datetime | None) -> tuple:
    wa = watched_at.isoformat() if watched_at else ""
    if vid:
        return ("v", vid, wa)
    return ("t", (title or "")[:200], wa)


def parser_backend() -> str:
    """Which JSON backend the streaming importers will use."""
    try:
        import ijson  # noqa: F401

        return "ijson"
    except Exception:  # noqa: BLE001
        return "json"


class ProgressTracker:
    """Throttled progress writer for a long-running (job) import (Phase 6D).

    Periodically writes scanned/imported/... into the linked
    ``TakeoutImportSession`` row (committing so the progress API can read it),
    and re-checks the session's ``cancel_requested`` flag. Used ONLY by job
    imports — synchronous imports pass ``tracker=None`` (no mid-commit).
    """

    def __init__(self, session, row, *, every: int = 1000, min_interval: float = 2.0):
        self.session = session
        self.row = row
        self.every = max(1, every)
        self.min_interval = min_interval
        self._last = 0.0
        self.cancelled = False

    def update(self, *, scanned, imported, skipped, updated, failed, phase=None, force=False) -> bool:
        now = time.monotonic()
        if not force and scanned % self.every != 0 and (now - self._last) < self.min_interval:
            return self.cancelled
        try:
            self.row.scanned = scanned
            self.row.imported = imported
            self.row.skipped_duplicate = skipped
            self.row.updated = updated
            self.row.failed = failed
            if phase:
                self.row.current_phase = phase
            self.row.last_update_at = utcnow()
            self.session.commit()  # persist partial progress (so it survives + is readable)
            self.session.refresh(self.row)
            self.cancelled = bool(self.row.cancel_requested)
        except Exception:  # noqa: BLE001 - never let progress writing break the import
            logger.exception("takeout: progress update failed")
        self._last = now
        return self.cancelled


def import_watch_history(
    session: Session,
    archive: TakeoutArchive,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    tracker: "ProgressTracker | None" = None,
    store_raw_json: bool = True,
) -> dict:
    """Import watch events into ``watch_history_events`` with dedup.

    ``limit`` caps the number of events scanned. When ``store_raw_json`` is
    False the raw activity blob is NOT persisted (normalized fields — video id /
    title / channel / watched_at — are always kept), to limit DB growth.
    """
    existing: set[tuple] = set()
    for vid, title, wa in session.execute(
        select(
            WatchHistoryEvent.youtube_video_id,
            WatchHistoryEvent.title,
            WatchHistoryEvent.watched_at,
        ).where(WatchHistoryEvent.source == "takeout")
    ):
        existing.add(_dedup_key(vid, title, wa))

    imported = skipped = failed = scanned = raw_stored = raw_skipped = 0
    seen: set[tuple] = set()
    warnings: list[str] = []
    cancelled = False

    for ev in archive.iter_watch_events():
        if limit is not None and scanned >= limit:
            break
        scanned += 1
        try:
            key = _dedup_key(ev.youtube_video_id, ev.title, ev.watched_at)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if len(warnings) < 20:
                warnings.append(str(exc))
            continue
        if key in existing or key in seen:
            skipped += 1
        else:
            seen.add(key)
            if not dry_run:
                session.add(
                    WatchHistoryEvent(
                        source="takeout",
                        youtube_video_id=ev.youtube_video_id,
                        title=_clip(ev.title, 1024),
                        channel_title=_clip(ev.channel_title, 512),
                        watched_at=ev.watched_at,
                        raw_json=ev.raw if store_raw_json else None,
                    )
                )
            raw_stored += 1 if store_raw_json else 0
            raw_skipped += 0 if store_raw_json else 1
            imported += 1
        if tracker is not None and tracker.update(
            scanned=scanned, imported=imported, skipped=skipped, updated=0,
            failed=failed, phase="watch_history",
        ):
            cancelled = True
            break

    if not dry_run:
        session.flush()

    return {
        "imported_count": imported,
        "skipped_duplicate_count": skipped,
        "updated_count": 0,
        "failed_count": failed,
        "scanned": scanned,
        "cancelled": cancelled,
        "raw_json_stored_count": raw_stored,
        "raw_json_skipped_count": raw_skipped,
        "store_raw_json": store_raw_json,
        "warnings": warnings,
    }


def import_search_history(
    session: Session, archive: TakeoutArchive, *, limit: int | None = None, dry_run: bool = False,
    tracker: "ProgressTracker | None" = None, store_raw_json: bool = True,
) -> dict:
    """Import search events into ``search_history_events`` with dedup."""
    existing: set[tuple] = set()
    for q, sa in session.execute(
        select(SearchHistoryEvent.query, SearchHistoryEvent.searched_at).where(
            SearchHistoryEvent.source == "takeout"
        )
    ):
        existing.add(((q or "")[:512], sa.isoformat() if sa else ""))

    imported = skipped = failed = scanned = raw_stored = raw_skipped = 0
    seen: set[tuple] = set()
    cancelled = False
    for ev in archive.iter_search_events():
        if limit is not None and scanned >= limit:
            break
        scanned += 1
        if not ev.query:
            failed += 1
        else:
            key = (ev.query[:512], ev.searched_at.isoformat() if ev.searched_at else "")
            if key in existing or key in seen:
                skipped += 1
            else:
                seen.add(key)
                if not dry_run:
                    session.add(
                        SearchHistoryEvent(
                            source="takeout",
                            query=_clip(ev.query, 512),
                            searched_at=ev.searched_at,
                            raw_json=ev.raw if store_raw_json else None,
                        )
                    )
                raw_stored += 1 if store_raw_json else 0
                raw_skipped += 0 if store_raw_json else 1
                imported += 1
        if tracker is not None and tracker.update(
            scanned=scanned, imported=imported, skipped=skipped, updated=0,
            failed=failed, phase="search_history",
        ):
            cancelled = True
            break
    if not dry_run:
        session.flush()
    return {
        "imported_count": imported,
        "skipped_duplicate_count": skipped,
        "updated_count": 0,
        "failed_count": failed,
        "scanned": scanned,
        "cancelled": cancelled,
        "raw_json_stored_count": raw_stored,
        "raw_json_skipped_count": raw_skipped,
        "store_raw_json": store_raw_json,
        "warnings": [],
    }


def import_subscriptions(
    session: Session, archive: TakeoutArchive, *, limit: int | None = None, dry_run: bool = False
) -> dict:
    """Import subscribed channels as Collections (type=channel, disabled)."""
    src = session.scalar(
        select(Source).where(
            Source.type == "channel_subscription", Source.api_source == "takeout"
        )
    )
    if src is None and not dry_run:
        src = Source(type="channel_subscription", api_source="takeout", name="Takeout subscriptions")
        session.add(src)
        session.flush()

    existing = {
        c for c in session.scalars(
            select(Collection.youtube_channel_id).where(Collection.type == "channel")
        ) if c
    }
    imported = skipped = failed = scanned = 0
    seen: set[str] = set()
    for sub in archive.iter_subscriptions():
        if limit is not None and scanned >= limit:
            break
        scanned += 1
        cid = sub.channel_id
        dkey = cid or sub.channel_url or sub.channel_title
        if not dkey:
            failed += 1
            continue
        if (cid and cid in existing) or dkey in seen:
            skipped += 1
            continue
        seen.add(dkey)
        if not dry_run:
            url = f"https://www.youtube.com/channel/{cid}" if cid else sub.channel_url
            session.add(
                Collection(
                    source_id=src.id if src else None,
                    type="channel",
                    youtube_channel_id=cid,
                    title=sub.channel_title,
                    url=url,
                    enabled=False,
                    crawl_policy="manual",
                )
            )
        imported += 1
    if not dry_run:
        session.flush()
    return {
        "imported_count": imported,
        "skipped_duplicate_count": skipped,
        "failed_count": failed,
        "scanned": scanned,
        "warnings": [],
    }


def _find_or_create_video(session: Session, vid: str) -> tuple[Video, bool]:
    v = session.scalar(select(Video).where(Video.youtube_video_id == vid))
    if v is None:
        v = Video(youtube_video_id=vid, url=canonical_video_url(vid), first_seen_at=utcnow())
        session.add(v)
        session.flush()
        return v, True
    return v, False


def import_playlists(
    session: Session,
    archive: TakeoutArchive,
    *,
    limit_playlists: int | None = None,
    limit_items: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Import Takeout playlists as Collections (type=takeout_playlist) + items + Video stubs."""
    src = session.scalar(
        select(Source).where(
            Source.type == "takeout_playlists", Source.api_source == "takeout"
        )
    )
    if src is None and not dry_run:
        src = Source(type="takeout_playlists", api_source="takeout", name="Takeout playlists")
        session.add(src)
        session.flush()

    existing_coll = {
        c.url: c
        for c in session.scalars(select(Collection).where(Collection.type == "takeout_playlist"))
    }
    playlists_imported = items_imported = items_skipped = videos_created = scanned_playlists = 0

    for pl in archive.iter_playlists(limit_items=limit_items):
        if limit_playlists is not None and scanned_playlists >= limit_playlists:
            break
        scanned_playlists += 1
        url = f"takeout:playlist:{pl.playlist_id or pl.title}"
        coll = existing_coll.get(url)
        if coll is None:
            playlists_imported += 1
            if not dry_run:
                coll = Collection(
                    source_id=src.id if src else None,
                    type="takeout_playlist",
                    title=pl.title,
                    url=url,
                    youtube_playlist_id=pl.playlist_id,
                    enabled=False,
                    crawl_policy="manual",
                )
                session.add(coll)
                session.flush()
                existing_coll[url] = coll

        coll_id = coll.id if coll is not None else None
        existing_items: set[str] = set()
        if coll_id is not None:
            existing_items = set(
                session.scalars(
                    select(CollectionItem.youtube_video_id).where(
                        CollectionItem.collection_id == coll_id
                    )
                )
            )
        for it in pl.items:
            if it.youtube_video_id in existing_items:
                items_skipped += 1
                continue
            existing_items.add(it.youtube_video_id)
            items_imported += 1
            if not dry_run:
                video, created = _find_or_create_video(session, it.youtube_video_id)
                if created:
                    videos_created += 1
                session.add(
                    CollectionItem(
                        collection_id=coll_id,
                        youtube_video_id=it.youtube_video_id,
                        video_id=video.id,
                        position=it.position,
                        discovered_at=utcnow(),
                        last_seen_at=utcnow(),
                        raw_json={"added_at": it.added_at.isoformat() if it.added_at else None},
                    )
                )
    if not dry_run:
        session.flush()
    return {
        "playlists_imported": playlists_imported,
        "items_imported": items_imported,
        "items_skipped": items_skipped,
        "videos_created": videos_created,
        "scanned_playlists": scanned_playlists,
        "warnings": [],
    }


def import_liked_videos(
    session: Session,
    archive: TakeoutArchive,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    tracker: "ProgressTracker | None" = None,
    store_raw_json: bool = True,
) -> dict:
    """Import liked videos into ``liked_videos`` (+ Video stubs) with dedup.

    Auto-detects the source (YouTube Takeout "Liked videos" CSV or Google
    "My Activity" YouTube JSON). Dedup is **cross-source by youtube_video_id**
    (a video liked in multiple exports/sources is stored once); id-less HTML
    rows dedup by (title, url). Returns ``source_kind`` / ``detected_path`` and
    creates/links a Video stub so a later ``metadata_only`` refresh can enrich.
    """
    source_kind, detected_path = archive.liked_source_path()

    # Existing liked videos across ALL sources (canonical: one row per video id).
    existing_ids: set[str] = set()
    existing_titlekey: set[tuple] = set()
    for vid, title, url in session.execute(
        select(LikedVideo.youtube_video_id, LikedVideo.title, LikedVideo.url)
    ):
        if vid:
            existing_ids.add(vid)
        else:
            existing_titlekey.add(((title or "")[:200], (url or "")))

    imported = skipped = failed = scanned = videos_created = updated = 0
    raw_stored = raw_skipped = 0
    seen_ids: set[str] = set()
    seen_titlekey: set[tuple] = set()
    warnings: list[str] = []

    for lv in archive.iter_liked_videos():
        if limit is not None and scanned >= limit:
            break
        scanned += 1
        if scanned % 2000 == 0:
            logger.info(
                "liked import: scanned=%d imported=%d skipped=%d updated=%d (%s)",
                scanned, imported, skipped, updated, source_kind,
            )
        if tracker is not None and tracker.update(
            scanned=scanned, imported=imported, skipped=skipped, updated=updated,
            failed=failed, phase="liked_videos",
        ):
            break  # cancel_requested -> stop (partial import persists)
        vid = lv.youtube_video_id
        if vid:
            if vid in existing_ids or vid in seen_ids:
                skipped += 1
                # incremental enrichment: fill empty Video-stub fields from the
                # re-imported entry (counts as "updated", not a new import).
                if not dry_run and (lv.title or lv.channel_title or lv.channel_id):
                    v = session.scalar(select(Video).where(Video.youtube_video_id == vid))
                    if v is not None:
                        changed = False
                        if lv.title and not v.title:
                            v.title = _clip(lv.title, 1024); changed = True
                        if lv.channel_title and not v.channel_title:
                            v.channel_title = _clip(lv.channel_title, 512); changed = True
                        if lv.channel_id and not v.channel_id:
                            v.channel_id = lv.channel_id; changed = True
                        if changed:
                            updated += 1
                continue
            seen_ids.add(vid)
        else:
            tkey = ((lv.title or "")[:200], (lv.url or ""))
            if not any(tkey):
                failed += 1
                continue
            if tkey in existing_titlekey or tkey in seen_titlekey:
                skipped += 1
                continue
            seen_titlekey.add(tkey)

        imported += 1
        if dry_run:
            continue

        video_pk = None
        if vid:
            video, created = _find_or_create_video(session, vid)
            if created:
                videos_created += 1
            # Enrich the stub from the liked entry only when fields are empty.
            if lv.title and not video.title:
                video.title = _clip(lv.title, 1024)
            if lv.channel_title and not video.channel_title:
                video.channel_title = _clip(lv.channel_title, 512)
            if lv.channel_id and not video.channel_id:
                video.channel_id = lv.channel_id
            video_pk = video.id
        session.add(
            LikedVideo(
                source=lv.source,
                youtube_video_id=vid,
                title=_clip(lv.title, 1024),
                channel_title=_clip(lv.channel_title, 512),
                url=lv.url,
                liked_at=lv.liked_at,
                video_id=video_pk,
                raw_json=lv.raw if store_raw_json else None,
            )
        )
        raw_stored += 1 if store_raw_json else 0
        raw_skipped += 0 if store_raw_json else 1

    if not dry_run:
        session.flush()
    logger.info(
        "liked import done: source=%s scanned=%d imported=%d skipped=%d videos_created=%d",
        source_kind, scanned, imported, skipped, videos_created,
    )
    return {
        "imported_count": imported,
        "skipped_duplicate_count": skipped,
        "updated_count": updated,
        "failed_count": failed,
        "scanned": scanned,
        "videos_created": videos_created,
        "source_kind": source_kind,
        "detected_path": detected_path,
        "raw_json_stored_count": raw_stored,
        "raw_json_skipped_count": raw_skipped,
        "store_raw_json": store_raw_json,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Job-wrapped runners
# --------------------------------------------------------------------------- #
def record_import_session(
    session: Session,
    *,
    import_kind: str,
    path_basename: str | None,
    source_kind: str | None,
    result: dict,
    dry_run: bool,
    started_at: datetime,
    status: str = "success",
) -> str:
    """Persist a TakeoutImportSession (Phase 6C). Fail-safe; no PII / full path.

    Returns the generated ``session_id`` (also injected into ``result``).
    """
    import uuid

    from app.models import TakeoutImportSession

    session_id = uuid.uuid4().hex[:16]
    try:
        row = TakeoutImportSession(
            session_id=session_id,
            path_basename=(path_basename or "")[:255] or None,
            source_kind=source_kind,
            import_kind=import_kind,
            started_at=started_at,
            finished_at=utcnow(),
            status=status,
            dry_run=dry_run,
            scanned=int(result.get("scanned", 0) or 0),
            imported=int(result.get("imported_count", 0) or 0),
            skipped_duplicate=int(result.get("skipped_duplicate_count", 0) or 0),
            updated=int(result.get("updated_count", 0) or 0),
            failed=int(result.get("failed_count", 0) or 0),
            meta={
                "videos_created": result.get("videos_created"),
                "detected_path_present": bool(result.get("detected_path")),
                "warning_count": len(result.get("warnings", []) or []),
                "store_raw_json": result.get("store_raw_json", True),
                "raw_json_stored_count": result.get("raw_json_stored_count", 0),
                "raw_json_skipped_count": result.get("raw_json_skipped_count", 0),
                "duration_seconds": result.get("duration_seconds"),
            },
        )
        session.add(row)
        session.flush()
    except Exception:  # noqa: BLE001 - session recording must never break import
        logger.exception("takeout: failed to record import session (%s)", import_kind)
    return session_id


def create_running_session(
    session: Session, *, import_kind: str, path_basename: str, source_kind: str | None,
    dry_run: bool, limit: int | None, job_id: int | None = None, rq_job_id: str | None = None,
    store_raw_json: bool = True,
):
    """Create a TakeoutImportSession in the 'running' state (for job imports)."""
    import uuid

    from app.models import TakeoutImportSession

    row = TakeoutImportSession(
        session_id=uuid.uuid4().hex[:16],
        path_basename=(path_basename or "")[:255] or None,
        source_kind=source_kind,
        import_kind=import_kind,
        started_at=utcnow(),
        status="running",
        dry_run=dry_run,
        parser_backend=parser_backend(),
        current_phase="starting",
        last_update_at=utcnow(),
        job_id=job_id,
        rq_job_id=rq_job_id,
        meta={"limit": limit, "store_raw_json": store_raw_json},
    )
    session.add(row)
    session.flush()
    return row


def request_cancel(session: Session, session_id: str) -> bool:
    """Flag a running import session for cancellation. Returns True if found+running."""
    row = get_import_session(session, session_id)
    if row is None or row.status not in ("running",):
        return False
    row.cancel_requested = True
    session.flush()
    return True


def run_takeout_import_job(session: Session, settings: Settings, job_id: int) -> dict:
    """Worker entrypoint: run a Takeout import as a background job (Phase 6D).

    Reads ``job.meta`` (import_kind / path / limit / dry_run / session_id), runs
    the import with a ProgressTracker writing into the session row, and finalizes
    both the session and the job. NEVER stores the host path or raw_json.
    """
    from app.models import Job

    job = session.get(Job, job_id)
    meta = job.meta or {}
    import_kind = meta.get("import_kind", "liked_videos")
    path = meta.get("path") or meta.get("path_basename") or ""
    limit = meta.get("limit")
    dry_run = bool(meta.get("dry_run"))
    store_raw_json = bool(meta.get("store_raw_json", True))
    session_id = meta.get("session_id")

    zip_path = resolve_takeout_path(settings, path)
    row = get_import_session(session, session_id) if session_id else None
    started = utcnow()
    t0 = time.monotonic()

    import tracemalloc

    tracemalloc.start()
    try:
        with open_archive(zip_path) as archive:
            archive_kind = archive.archive_kind()
            if row is not None and not row.source_kind:
                row.source_kind = archive_kind
                session.flush()
            tracker = ProgressTracker(session, row) if row is not None else None
            if import_kind == "watch_history":
                result = import_watch_history(session, archive, limit=limit, dry_run=dry_run, tracker=tracker, store_raw_json=store_raw_json)
            elif import_kind == "search_history":
                result = import_search_history(session, archive, limit=limit, dry_run=dry_run, tracker=tracker, store_raw_json=store_raw_json)
            elif import_kind == "liked_videos":
                result = import_liked_videos(session, archive, limit=limit, dry_run=dry_run, tracker=tracker, store_raw_json=store_raw_json)
            else:
                raise TakeoutError(f"unknown job import_kind: {import_kind!r}")
    except Exception as exc:  # noqa: BLE001
        tracemalloc.stop()
        # The failing flush may have poisoned the session (PendingRollbackError);
        # roll back first so the failure status actually persists (otherwise the
        # session row is left stuck "running").
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        row = get_import_session(session, session_id) if session_id else None
        job = session.get(Job, job_id)
        if row is not None:
            row.status = "failed"
            row.finished_at = utcnow()
            row.current_phase = "failed"
        if job is not None:
            job.status = "failed"
            job.error_message = f"takeout import: {exc}"[:2000]
            job.finished_at = utcnow()
        session.commit()
        raise
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    dur = time.monotonic() - t0
    scanned = int(result.get("scanned", 0) or 0)
    cancelled = bool(result.get("cancelled")) or (row is not None and row.cancel_requested)

    if row is not None:
        row.scanned = scanned
        row.imported = int(result.get("imported_count", 0) or 0)
        row.skipped_duplicate = int(result.get("skipped_duplicate_count", 0) or 0)
        row.updated = int(result.get("updated_count", 0) or 0)
        row.failed = int(result.get("failed_count", 0) or 0)
        row.entries_per_second = round(scanned / dur, 1) if dur > 0 else None
        row.peak_memory_mb = round(peak / 1024 / 1024, 2)
        row.source_kind = result.get("source_kind") or row.source_kind or archive_kind
        row.current_phase = "cancelled" if cancelled else "done"
        row.status = "cancelled" if cancelled else "success"
        row.finished_at = utcnow()
        row.meta = {
            **(row.meta or {}),
            "store_raw_json": store_raw_json,
            "raw_json_stored_count": int(result.get("raw_json_stored_count", 0) or 0),
            "raw_json_skipped_count": int(result.get("raw_json_skipped_count", 0) or 0),
            "duration_seconds": round(dur, 2),
        }
        session.flush()

    job.status = "success"
    job.progress = 100.0
    job.finished_at = utcnow()
    job.meta = {
        **(job.meta or {}),
        "scanned": scanned,
        "imported": int(result.get("imported_count", 0) or 0),
        "skipped_duplicate": int(result.get("skipped_duplicate_count", 0) or 0),
        "updated": int(result.get("updated_count", 0) or 0),
        "failed": int(result.get("failed_count", 0) or 0),
        "duration_seconds": round(dur, 2),
        "entries_per_second": round(scanned / dur, 1) if dur > 0 else None,
        "source_kind": result.get("source_kind") or archive_kind,
        "cancelled": cancelled,
    }
    session.flush()
    return {**result, "session_id": session_id, "duration_seconds": round(dur, 2),
            "peak_memory_mb": round(peak / 1024 / 1024, 2), "cancelled": cancelled}


def create_import_job(
    session: Session, settings: Settings, *,
    import_kind: str, path: str, limit: int | None = None, dry_run: bool = False,
    store_raw_json: bool = True,
):
    """Create a queued takeout_import Job + a running import session (Phase 6D).

    Returns (job, session_row). The caller commits and submits the job to RQ.
    Stores only the ZIP basename in job.meta (never the host path).
    """
    from app.models import Job

    zip_path = resolve_takeout_path(settings, path)  # validates / path-traversal guard
    source_kind = None
    try:
        with open_archive(zip_path) as a:
            source_kind = a.archive_kind()
    except TakeoutError:
        source_kind = None
    row = create_running_session(
        session, import_kind=import_kind, path_basename=zip_path.name,
        source_kind=source_kind, dry_run=dry_run, limit=limit, store_raw_json=store_raw_json,
    )
    job = Job(
        type="takeout_import",
        status="queued",
        meta={
            "import_kind": import_kind,
            "path": path,
            "path_basename": zip_path.name,
            "source_kind": source_kind,
            "limit": limit,
            "dry_run": dry_run,
            "store_raw_json": store_raw_json,
            "session_id": row.session_id,
            "enqueued_by": "takeout_import_job",
        },
    )
    session.add(job)
    session.flush()
    row.job_id = job.id
    session.flush()
    return job, row


def _run_with_job(
    session: Session,
    settings: Settings,
    path: str,
    kind: str,
    importer: Callable[[TakeoutArchive], dict],
    dry_run: bool,
    *,
    record_session: bool = True,
) -> dict:
    zip_path = resolve_takeout_path(settings, path)
    started = utcnow()
    job: Job | None = None
    if not dry_run:
        job = Job(
            type="takeout_import",
            status="running",
            started_at=utcnow(),
            meta={"file": zip_path.name, "kind": kind},
        )
        session.add(job)
        session.flush()
    archive_kind = None
    try:
        with open_archive(zip_path) as archive:
            archive_kind = archive.archive_kind()
            result = importer(archive)
    except TakeoutError:
        if job is not None:
            job.status = "failed"
            job.finished_at = utcnow()
            session.flush()
        if record_session:
            record_import_session(
                session, import_kind=kind, path_basename=zip_path.name,
                source_kind=archive_kind, result={}, dry_run=dry_run, started_at=started, status="failed",
            )
        raise
    result["dry_run"] = dry_run
    result["duration_seconds"] = round((utcnow() - started).total_seconds(), 2)
    # source_kind: liked imports report a precise source; others fall back to the
    # archive kind (my_activity_takeout / youtube_takeout / takeout_index).
    result.setdefault("source_kind", archive_kind)
    if job is not None:
        job.status = "success"
        job.finished_at = utcnow()
        job.progress = 100.0
        job.meta = {
            **(job.meta or {}),
            # persist scalar result fields (counts + source_kind/detected_path)
            **{k: v for k, v in result.items() if isinstance(v, (int, float, str)) or v is None},
        }
        session.flush()
        result["job_id"] = job.id
    else:
        result["job_id"] = None
    if record_session:
        result["session_id"] = record_import_session(
            session, import_kind=kind, path_basename=zip_path.name,
            source_kind=result.get("source_kind"), result=result,
            dry_run=dry_run, started_at=started,
        )
    return result


_BENCH_KINDS = {
    "liked_videos": import_liked_videos,
    "watch_history": import_watch_history,
    "search_history": import_search_history,
}


def benchmark(
    session: Session, settings: Settings, path: str, *,
    kind: str = "liked_videos", limit: int | None = None, dry_run: bool = True,
) -> dict:
    """Measure parse/import throughput + peak memory for a Takeout source.

    dry_run defaults to True (safe). Returns counts + duration + entries_per_second
    + peak_memory_mb + parser_backend. No raw_json / personal content is returned.
    """
    import time as _time
    import tracemalloc

    zip_path = resolve_takeout_path(settings, path)
    backend = parser_backend()
    tracemalloc.start()
    t0 = _time.monotonic()
    with open_archive(zip_path) as archive:
        archive_kind = archive.archive_kind()
        if kind == "all":
            agg = {"scanned": 0, "imported_count": 0, "skipped_duplicate_count": 0,
                   "updated_count": 0, "failed_count": 0}
            for fn in (import_watch_history, import_search_history, import_liked_videos):
                r = fn(session, archive, limit=limit, dry_run=dry_run)
                for k in agg:
                    agg[k] += int(r.get(k, 0) or 0)
            result = agg
        else:
            fn = _BENCH_KINDS.get(kind)
            if fn is None:
                raise TakeoutError(f"unknown benchmark kind: {kind!r}")
            result = fn(session, archive, limit=limit, dry_run=dry_run)
    dur = _time.monotonic() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    scanned = int(result.get("scanned", 0) or 0)
    return {
        "kind": kind,
        "scanned": scanned,
        "imported": int(result.get("imported_count", 0) or 0),
        "skipped_duplicate": int(result.get("skipped_duplicate_count", 0) or 0),
        "updated": int(result.get("updated_count", 0) or 0),
        "failed": int(result.get("failed_count", 0) or 0),
        "duration_seconds": round(dur, 3),
        "entries_per_second": round(scanned / dur, 1) if dur > 0 else None,
        "peak_memory_mb": round(peak / 1024 / 1024, 2),
        "parser_backend": backend,
        "dry_run": dry_run,
        "source_kind": result.get("source_kind") or archive_kind,
    }


def benchmark_large(
    session: Session, settings: Settings, path: str, *, include_search: bool = False,
) -> dict:
    """Full-scan dry-run benchmark for liked + watch (+ optional search).

    Returns per-kind throughput + a recommended batch size and an estimated
    full-import time (the dry-run scan time + a write-overhead factor). Safe:
    always dry-run; no raw_json / personal content returned.
    """
    kinds = ["liked_videos", "watch_history"] + (["search_history"] if include_search else [])
    results: dict[str, dict] = {}
    for kind in kinds:
        b = benchmark(session, settings, path, kind=kind, limit=None, dry_run=True)
        eps = b.get("entries_per_second") or 0
        # full import does DB writes too -> estimate scan time * 1.6 as a rough upper bound
        b["estimated_full_import_time_seconds"] = round(b["duration_seconds"] * 1.6, 1)
        # ~5s of work per batch, clamped to a safe range
        b["recommended_batch_size"] = int(min(5000, max(500, round(eps * 5)))) if eps else 1000
        results[kind] = b
    return {
        "results": results,
        "parser_backend": parser_backend(),
        "recommended_batch_size": max((r["recommended_batch_size"] for r in results.values()), default=1000),
        "dry_run": True,
    }


def cleanup_import_sessions(
    session: Session, *, keep_last: int = 0, older_than_days: int = 0, dry_run: bool = True,
    now: datetime | None = None,
) -> dict:
    """Prune old takeout_import_sessions. Deletes ONLY session rows — never jobs
    and never imported liked/watch/search data. Running sessions are kept.

    With both bounds 0, nothing is deleted (safety). A session is deletable when
    it is OLDER than ``older_than_days`` (if > 0) AND not among the most-recent
    ``keep_last`` (if > 0).
    """
    now = now or utcnow()
    all_rows = list(session.scalars(select(TakeoutImportSession).order_by(TakeoutImportSession.id.desc())))
    total = len(all_rows)
    keep_ids = {r.id for r in all_rows[:keep_last]} if keep_last and keep_last > 0 else set()
    cutoff = now - timedelta(days=older_than_days) if older_than_days and older_than_days > 0 else None

    deletable = []
    for r in all_rows:
        if r.id in keep_ids:
            continue
        if r.status == "running":  # never delete an in-flight session
            continue
        if cutoff is not None and (r.started_at or now) >= cutoff:
            continue
        if not keep_last and not older_than_days:
            continue  # no bounds -> delete nothing
        deletable.append(r)

    jobs_referenced = {r.job_id for r in deletable if r.job_id is not None}
    if not dry_run:
        for r in deletable:
            session.delete(r)  # ONLY the session row
        session.flush()
    return {
        "total": total,
        "matched": len(deletable),
        "deleted": 0 if dry_run else len(deletable),
        "kept": total - len(deletable),
        "jobs_preserved": len(jobs_referenced),
        "dry_run": dry_run,
        "keep_last": keep_last,
        "older_than_days": older_than_days,
    }


# --------------------------------------------------------------------------- #
# Phase 6F: scheduled auto-cleanup of import sessions (sessions only)
# --------------------------------------------------------------------------- #
def _cleanup_status_path(settings: Settings) -> Path:
    return settings.config_root / "takeout_session_cleanup_status.json"


def _read_cleanup_status_file(settings: Settings) -> dict:
    try:
        p = _cleanup_status_path(settings)
        if p.is_file():
            return json.loads(p.read_text("utf-8")) or {}
    except (OSError, ValueError):
        pass
    return {}


def cleanup_status(settings: Settings) -> dict:
    """Auto-cleanup configuration + last run result (Phase 6F)."""
    st = _read_cleanup_status_file(settings)
    enabled = settings.takeout_import_session_cleanup_enabled
    interval = max(1, settings.takeout_import_session_cleanup_interval_hours)
    keep_last = settings.takeout_import_session_keep_last
    retention = settings.takeout_import_session_retention_days
    last_run_at = st.get("last_run_at")
    next_due_at = None
    if enabled and (keep_last or retention):
        if last_run_at:
            try:
                nxt = datetime.fromisoformat(last_run_at) + timedelta(hours=interval)
                next_due_at = nxt.isoformat()
            except ValueError:
                next_due_at = None
        # no last run -> due now (next_due_at stays None == "now")
    return {
        "enabled": enabled,
        "interval_hours": interval,
        "keep_last": keep_last,
        "retention_days": retention,
        "last_run_at": last_run_at,
        "last_result": st.get("last_result"),
        "next_due_at": next_due_at,
    }


def auto_cleanup_import_sessions(
    session: Session, settings: Settings, *, now: datetime | None = None, force: bool = False,
) -> dict:
    """Scheduler-driven session prune (Phase 6F).

    Runs at most every ``CLEANUP_INTERVAL_HOURS`` and ONLY when enabled + at
    least one retention bound is set. Deletes ONLY import-session rows (never
    jobs / imported data; ``cleanup_import_sessions`` enforces this). The last
    result + timestamp are persisted to a status file so CLI/API can report it.
    ``force`` bypasses the enabled/interval gates (used by ``cleanup-auto``).
    """
    now = now or utcnow()
    keep_last = settings.takeout_import_session_keep_last
    retention = settings.takeout_import_session_retention_days
    interval = max(1, settings.takeout_import_session_cleanup_interval_hours)

    if not force and not settings.takeout_import_session_cleanup_enabled:
        return {"ran": False, "reason": "auto cleanup disabled", **cleanup_status(settings)}
    if not keep_last and not retention:
        return {"ran": False, "reason": "no retention bounds set (keep_last/retention_days)",
                **cleanup_status(settings)}

    if not force:
        st = _read_cleanup_status_file(settings)
        last = st.get("last_run_at")
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(hours=interval):
                    return {"ran": False, "reason": "not due yet", **cleanup_status(settings)}
            except ValueError:
                pass

    res = cleanup_import_sessions(
        session, keep_last=keep_last, older_than_days=retention, dry_run=False, now=now,
    )
    # persist status (fail-safe; never break the scheduler)
    try:
        settings.config_root.mkdir(parents=True, exist_ok=True)
        _cleanup_status_path(settings).write_text(
            json.dumps({"last_run_at": now.isoformat(), "last_result": res}, ensure_ascii=False),
            "utf-8",
        )
    except OSError:
        logger.warning("takeout: could not persist cleanup status file")
    logger.info("takeout auto-cleanup: %s", res)
    return {"ran": True, "reason": "applied", "result": res, **cleanup_status(settings)}


# --------------------------------------------------------------------------- #
# Phase 6F: large-import preflight / runner / post-import verify
# --------------------------------------------------------------------------- #
_LARGE_KINDS = ("liked_videos", "watch_history", "search_history")
_KIND_DBSTAT = {
    "liked_videos": "liked_videos",
    "watch_history": "watch_history_events",
    "search_history": "search_history_events",
}
_KIND_BLOB_MODEL = {
    "liked_videos": LikedVideo,
    "watch_history": WatchHistoryEvent,
    "search_history": SearchHistoryEvent,
}
# Forbidden substrings for the import leak check (no raw blobs / secrets / host
# absolute paths in session or job metadata).
_LEAK_PATTERNS = (
    '"raw_json":', "po_token", "visitor_data", "cookie", "secret",
    "BEGIN ", "/Users/", "/home/", "/takeout_imports/",
)


def _pf(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _resolve_large_kinds(kind: str) -> list[str]:
    if kind == "all":
        return ["liked_videos", "watch_history"]
    if kind in _LARGE_KINDS:
        return [kind]
    raise TakeoutError(f"unknown kind: {kind!r} (liked_videos|watch_history|search_history|all)")


def _first_fail(checks: list[dict]) -> str:
    for c in checks:
        if c["status"] == "fail":
            return c["detail"]
    return "preflight failed"


def preflight_large(
    session: Session, settings: Settings, path: str, *, kind: str = "all", sample_limit: int = 5000,
) -> dict:
    """Quick go/no-go for a large import: ZIP present, ijson parser, a bounded
    sample benchmark per kind, current DB counts, and a recommended command.

    Bounded (``sample_limit``) so it stays fast; run ``benchmark-large`` for the
    full-scan numbers. No raw_json / content / host path is returned.
    """
    from app.services import db_stats as dbs

    kinds = _resolve_large_kinds(kind)
    checks: list[dict] = []
    try:
        zip_path = resolve_takeout_path(settings, path)
        basename = zip_path.name
        checks.append(_pf("zip_exists", "ok", f"found {basename}"))
    except TakeoutError as exc:
        # Surface the reason WITHOUT the absolute path.
        reason = "zip not found / not under takeout root"
        if "not a .zip" in str(exc):
            reason = "not a .zip file"
        checks.append(_pf("zip_exists", "fail", reason))
        return {
            "ok": False, "path_basename": Path(path).name, "parser_backend": None,
            "checks": checks, "results": {}, "recommended_command": None,
        }

    parser = parser_backend()
    checks.append(_pf(
        "parser_backend", "ok" if parser == "ijson" else "warn",
        f"parser={parser}" + ("" if parser == "ijson" else " (install ijson for huge files)"),
    ))

    stats = dbs.db_stats(session)
    results: dict[str, dict] = {}
    for k in kinds:
        try:
            b = benchmark(session, settings, path, kind=k, limit=sample_limit, dry_run=True)
            results[k] = {
                "sample_scanned": b["scanned"],
                "entries_per_second": b.get("entries_per_second"),
                "peak_memory_mb": b.get("peak_memory_mb"),
                "parser_backend": b.get("parser_backend"),
                "current_db_count": stats.get(_KIND_DBSTAT[k], 0),
                "source_kind": b.get("source_kind"),
            }
            checks.append(_pf(
                f"benchmark:{k}", "ok",
                f"sample scanned={b['scanned']} eps={b.get('entries_per_second')} "
                f"peak={b.get('peak_memory_mb')}MB; current DB rows={stats.get(_KIND_DBSTAT[k], 0)}",
            ))
        except TakeoutError as exc:
            checks.append(_pf(f"benchmark:{k}", "fail", str(exc)))

    checks.append(_pf(
        "db_stats", "ok",
        f"dialect={stats['dialect']} raw_json_stored_total={stats['raw_json_stored_total']}",
    ))
    checks.append(_pf(
        "raw_json_policy", "ok",
        "recommended: import-large keeps --no-raw-json by default (drops raw blobs, "
        "keeps normalized fields)",
    ))

    ok = not any(c["status"] == "fail" for c in checks)
    rec = (
        f"archiver takeout import-large {basename} --kind {kind} --limit 1000 --apply"
        if ok else None
    )
    return {
        "ok": ok, "path_basename": basename, "parser_backend": parser,
        "checks": checks, "results": results, "recommended_command": rec,
    }


def import_large(
    session: Session, settings: Settings, path: str, *, kind: str = "all",
    limit: int | None = None, apply: bool = False, store_raw_json: bool = False,
    as_job: bool = True, skip_preflight: bool = False,
) -> dict:
    """Safe large-import runner (Phase 6F).

    Safe defaults: ``dry_run`` (``--apply`` required to write), ``no-raw-json``,
    and background ``job``. Runs preflight first; an ``--apply`` job is BLOCKED
    if system preflight fails (e.g. a stale worker) or the ZIP/parser preflight
    fails. ``skip_preflight`` bypasses the gate (NOT recommended).
    """
    from app.services import jobs as jobs_svc
    from app.services import preflight as pf

    dry_run = not apply
    kinds = _resolve_large_kinds(kind)
    base = {
        "ok": False, "kind": kind, "dry_run": dry_run, "store_raw_json": store_raw_json,
        "as_job": as_job, "preflight_ok": None, "items": [],
        "recommended_progress_command": None, "recommended_db_stats_command": None,
        "message": None,
    }

    preflight_ok: bool | None = None
    if not skip_preflight:
        large = preflight_large(session, settings, path, kind=kind)
        if not large["ok"]:
            return {**base, "preflight_ok": False,
                    "message": f"preflight-large failed: {_first_fail(large['checks'])}"}
        # An apply-via-job needs a healthy, build-matched worker; dry-run does not.
        sysrep = pf.system_preflight(session, settings)
        if apply and as_job and not sysrep["ok"]:
            return {**base, "preflight_ok": False,
                    "message": f"system preflight failed (no import): {_first_fail(sysrep['checks'])}"}
        preflight_ok = large["ok"] and (sysrep["ok"] if (apply and as_job) else True)

    runners = {
        "liked_videos": run_import_liked_videos,
        "watch_history": run_import,
        "search_history": run_import_search,
    }
    items: list[dict] = []
    for k in kinds:
        if dry_run:
            res = runners[k](
                session, settings, path, limit=limit, dry_run=True, store_raw_json=store_raw_json,
            )
            session.commit()
            items.append({
                "kind": k, "session_id": res.get("session_id"), "job_id": res.get("job_id"),
                "dry_run": True, "store_raw_json": store_raw_json,
                "scanned": res.get("scanned"), "would_import": res.get("imported_count"),
                "rq_submitted": False,
            })
        elif as_job:
            job, row = create_import_job(
                session, settings, import_kind=k, path=path, limit=limit,
                dry_run=False, store_raw_json=store_raw_json,
            )
            session.commit()
            rq_id = None
            try:
                rq_id = jobs_svc.submit_job(job.id)
                if rq_id:
                    j = session.get(Job, job.id)
                    j.rq_job_id = rq_id
                    session.commit()
            except Exception:  # noqa: BLE001 - Redis down; job stays queued
                pass
            items.append({
                "kind": k, "session_id": row.session_id, "job_id": job.id,
                "dry_run": False, "store_raw_json": store_raw_json, "rq_submitted": bool(rq_id),
            })
        else:  # synchronous apply (no background job) — small/explicit use
            res = runners[k](
                session, settings, path, limit=limit, dry_run=False, store_raw_json=store_raw_json,
            )
            session.commit()
            items.append({
                "kind": k, "session_id": res.get("session_id"), "job_id": res.get("job_id"),
                "dry_run": False, "store_raw_json": store_raw_json,
                "imported": res.get("imported_count"), "rq_submitted": False,
            })

    return {
        **base, "ok": True, "preflight_ok": preflight_ok, "items": items,
        "recommended_progress_command": "archiver takeout verify-import --latest"
        + (f" --kind {kind}" if kind != "all" else ""),
        "recommended_db_stats_command": "archiver storage db-stats",
        "message": "dry-run (no writes) — re-run with --apply to import"
        if dry_run else "import submitted",
    }


def verify_import(
    session: Session, settings: Settings, *, session_id: str | None = None,
    latest: bool = False, kind: str | None = None,
) -> dict:
    """Post-import inspection (Phase 6F): session outcome + DB stats + real
    raw_json blob counts + a leak grep (no secrets / host paths in metadata) +
    job status. Read-only."""
    from app.services import db_stats as dbs

    row = None
    if session_id:
        row = get_import_session(session, session_id)
    elif latest:
        rows = list_import_sessions(session, import_kind=kind, limit=1)
        row = rows[0] if rows else None
    if row is None:
        # session_id=None signals "not found" to the API (404) / CLI.
        return {"ok": False, "session_id": None,
                "checks": [_pf("session_found", "fail", "import session not found")]}

    meta = row.meta or {}
    checks: list[dict] = [_pf("session_found", "ok", f"session {row.session_id}")]

    # status
    if row.status == "success":
        checks.append(_pf("status", "ok", "import succeeded"))
    elif row.status in ("running",):
        checks.append(_pf("status", "warn", "import still running"))
    else:
        checks.append(_pf("status", "fail", f"import status={row.status}"))

    # job status / worker error
    job_status = None
    worker_error = None
    if row.job_id:
        job = session.get(Job, row.job_id)
        if job is not None:
            job_status = job.status
            worker_error = job.error_message
            checks.append(_pf(
                "job_status", "ok" if job.status in ("success", None) else "fail",
                f"job #{row.job_id} status={job.status}",
            ))

    # DB stats + real raw_json blobs (db_stats already excludes JSON-null)
    stats = dbs.db_stats(session)
    real_blobs = stats.get("raw_json_stored", {})

    # no-raw-json consistency: a no-raw-json run should have skipped == imported
    store_raw = meta.get("store_raw_json")
    if store_raw is False:
        skipped = meta.get("raw_json_skipped_count")
        stored = meta.get("raw_json_stored_count")
        if stored in (0, None):
            checks.append(_pf("raw_json_policy", "ok",
                              f"no-raw-json honored (skipped={skipped}, stored={stored or 0})"))
        else:
            checks.append(_pf("raw_json_policy", "fail",
                              f"store_raw_json=False but stored={stored}"))
    elif store_raw is True:
        checks.append(_pf("raw_json_policy", "warn",
                          "raw_json ON for this session (DB growth — consider --no-raw-json)"))

    # leak grep over session + job metadata (never content)
    import json as _json

    blob = _json.dumps({"session_meta": meta, "path_basename": row.path_basename,
                        "source_kind": row.source_kind}, ensure_ascii=False)
    if row.job_id:
        job = session.get(Job, row.job_id)
        blob += _json.dumps(getattr(job, "meta", {}) or {}, ensure_ascii=False)
    findings = [p for p in _LEAK_PATTERNS if p in blob]
    leak_ok = not findings
    checks.append(_pf("leak_check", "ok" if leak_ok else "fail",
                      "no secrets / raw blob / host path in metadata" if leak_ok
                      else f"found: {findings}"))

    ok = leak_ok and row.status == "success" and (job_status in ("success", None)) \
        and not any(c["status"] == "fail" for c in checks)
    return {
        "ok": ok,
        "session_id": row.session_id,
        "import_kind": row.import_kind,
        "status": row.status,
        "scanned": row.scanned,
        "imported": row.imported,
        "skipped_duplicate": row.skipped_duplicate,
        "updated": row.updated,
        "failed": row.failed,
        "parser_backend": row.parser_backend,
        "entries_per_second": row.entries_per_second,
        "peak_memory_mb": row.peak_memory_mb,
        "store_raw_json": meta.get("store_raw_json"),
        "raw_json_stored_count": meta.get("raw_json_stored_count"),
        "raw_json_skipped_count": meta.get("raw_json_skipped_count"),
        "job_id": row.job_id,
        "job_status": job_status,
        "worker_error": worker_error,
        "db_stats": {
            "dialect": stats["dialect"], "total_size_mb": stats["total_size_mb"],
            "videos": stats["videos"], "liked_videos": stats["liked_videos"],
            "watch_history_events": stats["watch_history_events"],
            "search_history_events": stats["search_history_events"],
            "raw_json_stored_total": stats["raw_json_stored_total"],
        },
        "raw_json_real_blobs": real_blobs,
        "leak_check_ok": leak_ok,
        "leak_findings": findings,
        "checks": checks,
    }


# --------------------------------------------------------------------------- #
# Phase 6G: staged production import + resume info + operation report
# --------------------------------------------------------------------------- #
# Cumulative per-stage limits. The final ``None`` = full import (gated behind
# allow_full so a 90k import is never started accidentally).
_STAGE_LIMITS = {
    "liked_videos": [100, 1000, 5000, None],
    "watch_history": [1000, 10000, 50000, None],
    "search_history": [1000, 10000, 50000, None],
}


def _wait_for_job(job_id: int, *, timeout: float = 1800.0, interval: float = 2.0) -> str:
    """Poll a job to a terminal state in fresh sessions (used by staged apply
    with background jobs). Returns the final status or 'timeout'."""
    from app.db import session_scope

    waited = 0.0
    while waited < timeout:
        with session_scope() as s:
            j = s.get(Job, job_id)
            if j is not None and j.status in ("success", "failed", "canceled"):
                return j.status
        time.sleep(interval)
        waited += interval
    return "timeout"


def recent_sessions(
    session: Session, *, import_kind: str | None = None, path_basename: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Compact recent-session list for resume/rerun decisions (counts only)."""
    stmt = select(TakeoutImportSession).order_by(TakeoutImportSession.id.desc())
    if import_kind:
        stmt = stmt.where(TakeoutImportSession.import_kind == import_kind)
    if path_basename:
        stmt = stmt.where(TakeoutImportSession.path_basename == path_basename)
    rows = list(session.scalars(stmt.limit(limit)))
    return [
        {
            "session_id": r.session_id, "import_kind": r.import_kind, "status": r.status,
            "dry_run": r.dry_run, "scanned": r.scanned, "imported": r.imported,
            "skipped_duplicate": r.skipped_duplicate, "started_at": r.started_at.isoformat() if r.started_at else None,
            "job_id": r.job_id,
        }
        for r in rows
    ]


def import_staged(
    settings: Settings, path: str, *, kind: str = "all", apply: bool = False,
    store_raw_json: bool = False, as_job: bool = True, skip_preflight: bool = False,
    allow_full: bool = False, max_stage: int | None = None, wait_timeout: float = 1800.0,
) -> dict:
    """Staged production import (Phase 6G). Manages its own DB sessions.

    dry-run (default): preflight + per-kind full benchmark + the staged PLAN — no
    writes. apply: runs each stage (cumulative limit) with verify + db-stats
    between stages; the final FULL stage runs only with ``allow_full``. Safe
    defaults: no-raw-json, background job, preflight-gated (an --apply job is
    blocked when system preflight fails, e.g. a stale worker).
    """
    from app.db import session_scope
    from app.services import db_stats as dbs
    from app.services import jobs as jobs_svc
    from app.services import preflight as pf

    dry_run = not apply
    try:
        kinds = _resolve_large_kinds(kind)
    except TakeoutError as exc:
        return {"ok": False, "kind": kind, "dry_run": dry_run, "message": str(exc),
                "stages": [], "plan": {}, "prior_sessions": [], "preflight_ok": None}

    out: dict = {
        "ok": False, "kind": kind, "dry_run": dry_run, "store_raw_json": store_raw_json,
        "as_job": as_job, "allow_full": allow_full, "preflight_ok": None,
        "plan": {k: ["full" if v is None else v for v in _STAGE_LIMITS[k]] for k in kinds},
        "prior_sessions": [], "stages": [], "message": None, "recommended_next": None,
    }

    base_name = Path(path).name

    # ---- resume/rerun info: prior sessions for these kinds + this file ----
    with session_scope() as s:
        prior: list[dict] = []
        for k in kinds:
            prior += recent_sessions(s, import_kind=k, path_basename=base_name, limit=3)
        out["prior_sessions"] = prior

    # ---- preflight (system + large) ----
    if not skip_preflight:
        with session_scope() as s:
            large = preflight_large(s, settings, path, kind=kind)
            sysrep = pf.system_preflight(s, settings)
        if not large["ok"]:
            out["message"] = f"preflight-large failed: {_first_fail(large['checks'])}"
            out["preflight_ok"] = False
            return out
        if apply and as_job and not sysrep["ok"]:
            out["message"] = f"system preflight failed (no import): {_first_fail(sysrep['checks'])}"
            out["preflight_ok"] = False
            return out
        out["preflight_ok"] = large["ok"] and (sysrep["ok"] if (apply and as_job) else True)

    # ---- dry-run: benchmark per kind + plan only ----
    if dry_run:
        for k in kinds:
            with session_scope() as s:
                b = benchmark(s, settings, path, kind=k, limit=None, dry_run=True)
            out["stages"].append({
                "kind": k, "stage": "benchmark", "limit": None, "status": "dry_run",
                "scanned": b["scanned"], "would_import": b["imported"],
                "eps": b.get("entries_per_second"), "peak_memory_mb": b.get("peak_memory_mb"),
            })
        out["ok"] = True
        out["message"] = "dry-run plan (no writes) — re-run with --apply to execute stages"
        out["recommended_next"] = f"archiver takeout import-staged {base_name} --kind {kind} --apply"
        return out

    # ---- apply: execute stages with verify + db-stats between ----
    runners = {
        "liked_videos": run_import_liked_videos,
        "watch_history": run_import,
        "search_history": run_import_search,
    }
    for k in kinds:
        for i, lim in enumerate(_STAGE_LIMITS[k]):
            is_full = lim is None
            if is_full and not allow_full:
                out["stages"].append({"kind": k, "stage": i + 1, "limit": "full",
                                       "status": "skipped_needs_allow_full"})
                continue
            if max_stage is not None and (i + 1) > max_stage:
                break

            with session_scope() as s:
                size_before = dbs.db_stats(s)["total_size_mb"]

            if as_job:
                with session_scope() as s:
                    job, row = create_import_job(
                        s, settings, import_kind=k, path=path, limit=lim,
                        dry_run=False, store_raw_json=store_raw_json,
                    )
                    s.commit()
                    jid, sid = job.id, row.session_id
                try:
                    rq_id = jobs_svc.submit_job(jid)
                    if rq_id:
                        with session_scope() as s:
                            jj = s.get(Job, jid)
                            jj.rq_job_id = rq_id
                            s.commit()
                except Exception:  # noqa: BLE001 - Redis down; job stays queued
                    pass
                status = _wait_for_job(jid, timeout=wait_timeout)
            else:
                with session_scope() as s:
                    res = runners[k](s, settings, path, limit=lim, dry_run=False,
                                     store_raw_json=store_raw_json)
                    s.commit()
                    jid, sid = res.get("job_id"), res.get("session_id")
                status = "success"

            with session_scope() as s:
                v = verify_import(s, settings, session_id=sid)
                size_after = dbs.db_stats(s)["total_size_mb"]

            out["stages"].append({
                "kind": k, "stage": i + 1, "limit": "full" if is_full else lim,
                "session_id": sid, "job_id": jid, "status": status,
                "scanned": v["scanned"], "imported": v["imported"],
                "skipped": v["skipped_duplicate"], "updated": v["updated"], "failed": v["failed"],
                "eps": v["entries_per_second"], "peak_memory_mb": v["peak_memory_mb"],
                "raw_json_stored": v["raw_json_stored_count"], "raw_json_skipped": v["raw_json_skipped_count"],
                "db_size_mb_before": size_before, "db_size_mb_after": size_after,
                "verify_ok": v["ok"],
            })
            if status != "success":
                out["message"] = f"stage {i + 1} ({k}) status={status}; stopping (re-run is dedup-safe)"
                out["ok"] = False
                return out

    out["ok"] = True
    if not allow_full:
        out["message"] = "staged apply complete (bounded stages). Re-run with --allow-full for the full import."
        out["recommended_next"] = f"archiver takeout import-staged {base_name} --kind {kind} --apply --allow-full"
    else:
        out["message"] = "staged apply complete (through full import)."
        out["recommended_next"] = "archiver takeout import-report --latest"
    return out


def import_report(
    session: Session, settings: Settings, *, session_id: str | None = None,
    latest: bool = False, kind: str | None = None, recent: int | None = None,
) -> dict:
    """Operation report (Phase 6G). Wraps ``verify_import`` and adds a
    recommended next action. With ``recent`` returns a list of compact reports.
    No raw_json / history body / secrets / host paths."""
    if recent:
        rows = list_import_sessions(session, import_kind=kind, limit=recent)
        reports = []
        for r in rows:
            v = verify_import(session, settings, session_id=r.session_id)
            reports.append({
                "session_id": v["session_id"], "import_kind": v["import_kind"],
                "status": v["status"], "imported": v["imported"], "scanned": v["scanned"],
                "skipped_duplicate": v["skipped_duplicate"], "store_raw_json": v["store_raw_json"],
                "job_status": v["job_status"], "leak_check_ok": v["leak_check_ok"],
                "ok": v["ok"], "recommended_next_action": _report_next_action(v),
            })
        return {"reports": reports, "count": len(reports)}

    v = verify_import(session, settings, session_id=session_id, latest=latest, kind=kind)
    if not v.get("session_id"):
        return {"ok": False, "session_id": None, "recommended_next_action": "session not found",
                **v}
    row = get_import_session(session, v["session_id"])
    v["path_basename"] = row.path_basename if row else None
    v["started_at"] = row.started_at.isoformat() if row and row.started_at else None
    v["finished_at"] = row.finished_at.isoformat() if row and row.finished_at else None
    v["recommended_next_action"] = _report_next_action(v)
    return v


def _report_next_action(v: dict) -> str:
    if not v.get("leak_check_ok", True):
        return "ALERT: leak check failed — investigate session/job metadata before continuing."
    status = v.get("status")
    if status == "failed" or v.get("job_status") == "failed":
        return ("import failed — check worker_error, then re-run import-staged "
                "(re-import is dedup-safe; already-imported rows are skipped).")
    if status == "running":
        return "import still running — monitor with `verify-import --latest`."
    if v.get("store_raw_json") is True:
        return ("success, but raw_json is ON (DB growth) — confirm db-stats; "
                "consider --no-raw-json for the remaining stages.")
    return "success — proceed to the next stage (or full import) and re-run verify/db-stats."


def list_import_sessions(session: Session, *, import_kind: str | None = None, limit: int = 50) -> list:
    from app.models import TakeoutImportSession

    stmt = select(TakeoutImportSession).order_by(TakeoutImportSession.id.desc())
    if import_kind:
        stmt = stmt.where(TakeoutImportSession.import_kind == import_kind)
    return list(session.scalars(stmt.limit(limit)))


def get_import_session(session: Session, session_id: str):
    from app.models import TakeoutImportSession

    return session.scalar(
        select(TakeoutImportSession).where(TakeoutImportSession.session_id == session_id)
    )


def run_import(
    session: Session, settings: Settings, path: str, *, limit: int | None = None,
    dry_run: bool = False, record_session: bool = True, store_raw_json: bool = True,
) -> dict:
    """Import watch history (Phase 3A)."""
    return _run_with_job(
        session, settings, path, "watch_history",
        lambda a: import_watch_history(session, a, limit=limit, dry_run=dry_run, store_raw_json=store_raw_json),
        dry_run, record_session=record_session,
    )


def run_import_search(
    session: Session, settings: Settings, path: str, *, limit: int | None = None,
    dry_run: bool = False, record_session: bool = True, store_raw_json: bool = True,
) -> dict:
    return _run_with_job(
        session, settings, path, "search_history",
        lambda a: import_search_history(session, a, limit=limit, dry_run=dry_run, store_raw_json=store_raw_json),
        dry_run, record_session=record_session,
    )


def run_import_subscriptions(
    session: Session, settings: Settings, path: str, *, limit: int | None = None,
    dry_run: bool = False, record_session: bool = True,
) -> dict:
    return _run_with_job(
        session, settings, path, "subscriptions",
        lambda a: import_subscriptions(session, a, limit=limit, dry_run=dry_run), dry_run,
        record_session=record_session,
    )


def run_import_playlists(
    session: Session,
    settings: Settings,
    path: str,
    *,
    limit_playlists: int | None = None,
    limit_items: int | None = None,
    dry_run: bool = False,
    record_session: bool = True,
) -> dict:
    return _run_with_job(
        session, settings, path, "playlists",
        lambda a: import_playlists(
            session, a, limit_playlists=limit_playlists, limit_items=limit_items, dry_run=dry_run
        ),
        dry_run,
        record_session=record_session,
    )


def run_import_liked_videos(
    session: Session, settings: Settings, path: str, *, limit: int | None = None,
    dry_run: bool = False, record_session: bool = True, store_raw_json: bool = True,
) -> dict:
    return _run_with_job(
        session, settings, path, "liked_videos",
        lambda a: import_liked_videos(session, a, limit=limit, dry_run=dry_run, store_raw_json=store_raw_json),
        dry_run, record_session=record_session,
    )


def run_import_all(
    session: Session,
    settings: Settings,
    path: str,
    *,
    limit_watch: int | None = None,
    limit_search: int | None = None,
    limit_subscriptions: int | None = None,
    limit_playlists: int | None = None,
    limit_items: int | None = None,
    limit_liked: int | None = None,
    dry_run: bool = False,
    store_raw_json: bool = True,
) -> dict:
    """Run all Takeout imports in order, returning per-kind results + ONE combined session."""
    started = utcnow()
    parts = {
        "watch_history": run_import(session, settings, path, limit=limit_watch, dry_run=dry_run, record_session=False, store_raw_json=store_raw_json),
        "search_history": run_import_search(session, settings, path, limit=limit_search, dry_run=dry_run, record_session=False, store_raw_json=store_raw_json),
        "subscriptions": run_import_subscriptions(session, settings, path, limit=limit_subscriptions, dry_run=dry_run, record_session=False),
        "playlists": run_import_playlists(
            session, settings, path, limit_playlists=limit_playlists, limit_items=limit_items, dry_run=dry_run, record_session=False
        ),
        "liked_videos": run_import_liked_videos(session, settings, path, limit=limit_liked, dry_run=dry_run, record_session=False, store_raw_json=store_raw_json),
        "dry_run": dry_run,
    }
    # one combined "all" session aggregating the per-kind counts
    agg = {
        "scanned": sum(int(p.get("scanned", 0) or 0) for p in parts.values() if isinstance(p, dict)),
        "imported_count": sum(int(p.get("imported_count", 0) or 0) for p in parts.values() if isinstance(p, dict)),
        "skipped_duplicate_count": sum(int(p.get("skipped_duplicate_count", 0) or 0) for p in parts.values() if isinstance(p, dict)),
        "updated_count": sum(int(p.get("updated_count", 0) or 0) for p in parts.values() if isinstance(p, dict)),
        "failed_count": sum(int(p.get("failed_count", 0) or 0) for p in parts.values() if isinstance(p, dict)),
        "raw_json_stored_count": sum(int(p.get("raw_json_stored_count", 0) or 0) for p in parts.values() if isinstance(p, dict)),
        "raw_json_skipped_count": sum(int(p.get("raw_json_skipped_count", 0) or 0) for p in parts.values() if isinstance(p, dict)),
        "store_raw_json": store_raw_json,
    }
    zip_name = resolve_takeout_path(settings, path).name
    parts["session_id"] = record_import_session(
        session, import_kind="all", path_basename=zip_name,
        source_kind=parts["liked_videos"].get("source_kind") if isinstance(parts.get("liked_videos"), dict) else None,
        result=agg, dry_run=dry_run, started_at=started,
    )
    return parts
