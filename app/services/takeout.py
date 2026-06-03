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
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Collection,
    CollectionItem,
    Job,
    SearchHistoryEvent,
    Source,
    Video,
    WatchHistoryEvent,
    utcnow,
)
from app.services.urls import canonical_video_url, extract_video_id, is_video_id


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

    def list_files(self) -> list[TakeoutFile]:
        files: list[TakeoutFile] = []
        for name, info in sorted(self._members.items()):
            if "youtube" not in name.lower():
                continue
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext == "json":
                head = self._read(name)[:8192].decode("utf-8", errors="replace")
                kind, fmt = _classify_json_peek(head), "json"
            elif ext in ("html", "htm"):
                low = name.lower()
                if "watch" in low or "視聴" in name:
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
        rows = [r for r in csv.reader(io.StringIO(text))]
        return max(0, len(rows) - 1)  # minus header

    # ----- watch events -----
    def iter_watch_events(self) -> Iterator[WatchEvent]:
        for f in self.list_files():
            if f.kind != "watch_history":
                continue
            if f.format == "json":
                data = json.loads(self._read_text(f.name))
                if isinstance(data, list):
                    yield from parse_watch_history_json(data)
            elif f.format == "html":
                yield from parse_watch_history_html(self._read_text(f.name))

    def iter_search_events(self) -> Iterator[SearchEvent]:
        for f in self.list_files():
            if f.kind != "search_history" or f.format != "json":
                continue
            data = json.loads(self._read_text(f.name))
            if isinstance(data, list):
                yield from parse_search_history_json(data)

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
                elif f.kind == "likes":
                    counts["likes_count"] += self._count_csv_rows(f)
                elif f.kind == "playlist":
                    counts["playlists_count"] += 1
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{f.name}: {exc}")

        samples: list[dict] = []
        search_samples: list[str] = []
        subscription_samples: list[dict] = []
        playlist_samples: list[dict] = []
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
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"sample parse: {exc}")

        importable = {
            "watch_history": counts["watch_history_count"] > 0,
            "search_history": counts["search_history_count"] > 0,
            "subscriptions": counts["subscriptions_count"] > 0,
            "playlists": counts["playlists_count"] > 0,
        }
        return {
            "files": [
                {"name": f.name, "kind": f.kind, "format": f.format, "size": f.size}
                for f in files
            ],
            **counts,
            "samples": samples,
            "search_samples": search_samples,
            "subscription_samples": subscription_samples,
            "playlist_samples": playlist_samples,
            "importable": importable,
            "warnings": warnings,
        }


def open_archive(path: Path) -> TakeoutArchive:
    return TakeoutArchive(path)


# --------------------------------------------------------------------------- #
# Import (watch_history_events)
# --------------------------------------------------------------------------- #
def _dedup_key(vid: str | None, title: str | None, watched_at: datetime | None) -> tuple:
    wa = watched_at.isoformat() if watched_at else ""
    if vid:
        return ("v", vid, wa)
    return ("t", (title or "")[:200], wa)


def import_watch_history(
    session: Session,
    archive: TakeoutArchive,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Import watch events into ``watch_history_events`` with dedup.

    ``limit`` caps the number of events scanned from the archive. Returns counts.
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

    imported = skipped = failed = scanned = 0
    seen: set[tuple] = set()
    warnings: list[str] = []

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
            continue
        seen.add(key)
        if not dry_run:
            session.add(
                WatchHistoryEvent(
                    source="takeout",
                    youtube_video_id=ev.youtube_video_id,
                    title=ev.title,
                    channel_title=ev.channel_title,
                    watched_at=ev.watched_at,
                    raw_json=ev.raw,
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
        "warnings": warnings,
    }


def import_search_history(
    session: Session, archive: TakeoutArchive, *, limit: int | None = None, dry_run: bool = False
) -> dict:
    """Import search events into ``search_history_events`` with dedup."""
    existing: set[tuple] = set()
    for q, sa in session.execute(
        select(SearchHistoryEvent.query, SearchHistoryEvent.searched_at).where(
            SearchHistoryEvent.source == "takeout"
        )
    ):
        existing.add(((q or "")[:512], sa.isoformat() if sa else ""))

    imported = skipped = failed = scanned = 0
    seen: set[tuple] = set()
    for ev in archive.iter_search_events():
        if limit is not None and scanned >= limit:
            break
        scanned += 1
        if not ev.query:
            failed += 1
            continue
        key = (ev.query[:512], ev.searched_at.isoformat() if ev.searched_at else "")
        if key in existing or key in seen:
            skipped += 1
            continue
        seen.add(key)
        if not dry_run:
            session.add(
                SearchHistoryEvent(
                    source="takeout",
                    query=ev.query,
                    searched_at=ev.searched_at,
                    raw_json=ev.raw,
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


# --------------------------------------------------------------------------- #
# Job-wrapped runners
# --------------------------------------------------------------------------- #
def _run_with_job(
    session: Session,
    settings: Settings,
    path: str,
    kind: str,
    importer: Callable[[TakeoutArchive], dict],
    dry_run: bool,
) -> dict:
    zip_path = resolve_takeout_path(settings, path)
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
    try:
        with open_archive(zip_path) as archive:
            result = importer(archive)
    except TakeoutError:
        if job is not None:
            job.status = "failed"
            job.finished_at = utcnow()
            session.flush()
        raise
    result["dry_run"] = dry_run
    if job is not None:
        job.status = "success"
        job.finished_at = utcnow()
        job.progress = 100.0
        job.meta = {
            **(job.meta or {}),
            **{k: v for k, v in result.items() if isinstance(v, int)},
        }
        session.flush()
        result["job_id"] = job.id
    else:
        result["job_id"] = None
    return result


def run_import(
    session: Session, settings: Settings, path: str, *, limit: int | None = None, dry_run: bool = False
) -> dict:
    """Import watch history (Phase 3A)."""
    return _run_with_job(
        session, settings, path, "watch_history",
        lambda a: import_watch_history(session, a, limit=limit, dry_run=dry_run), dry_run,
    )


def run_import_search(
    session: Session, settings: Settings, path: str, *, limit: int | None = None, dry_run: bool = False
) -> dict:
    return _run_with_job(
        session, settings, path, "search_history",
        lambda a: import_search_history(session, a, limit=limit, dry_run=dry_run), dry_run,
    )


def run_import_subscriptions(
    session: Session, settings: Settings, path: str, *, limit: int | None = None, dry_run: bool = False
) -> dict:
    return _run_with_job(
        session, settings, path, "subscriptions",
        lambda a: import_subscriptions(session, a, limit=limit, dry_run=dry_run), dry_run,
    )


def run_import_playlists(
    session: Session,
    settings: Settings,
    path: str,
    *,
    limit_playlists: int | None = None,
    limit_items: int | None = None,
    dry_run: bool = False,
) -> dict:
    return _run_with_job(
        session, settings, path, "playlists",
        lambda a: import_playlists(
            session, a, limit_playlists=limit_playlists, limit_items=limit_items, dry_run=dry_run
        ),
        dry_run,
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
    dry_run: bool = False,
) -> dict:
    """Run all Takeout imports in order, returning per-kind results."""
    return {
        "watch_history": run_import(session, settings, path, limit=limit_watch, dry_run=dry_run),
        "search_history": run_import_search(session, settings, path, limit=limit_search, dry_run=dry_run),
        "subscriptions": run_import_subscriptions(session, settings, path, limit=limit_subscriptions, dry_run=dry_run),
        "playlists": run_import_playlists(
            session, settings, path, limit_playlists=limit_playlists, limit_items=limit_items, dry_run=dry_run
        ),
        "dry_run": dry_run,
    }
