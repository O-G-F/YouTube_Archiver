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
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Job, WatchHistoryEvent, utcnow
from app.services.urls import extract_video_id


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
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"sample parse: {exc}")

        return {
            "files": [
                {"name": f.name, "kind": f.kind, "format": f.format, "size": f.size}
                for f in files
            ],
            **counts,
            "samples": samples,
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


def run_import(
    session: Session,
    settings: Settings,
    path: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Resolve + open the archive, record a job, and import watch history."""
    zip_path = resolve_takeout_path(settings, path)

    job: Job | None = None
    if not dry_run:
        job = Job(
            type="takeout_import",
            status="running",
            started_at=utcnow(),
            meta={"file": zip_path.name, "limit": limit},
        )
        session.add(job)
        session.flush()

    try:
        with open_archive(zip_path) as archive:
            result = import_watch_history(
                session, archive, limit=limit, dry_run=dry_run
            )
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
            "imported_count": result["imported_count"],
            "skipped_duplicate_count": result["skipped_duplicate_count"],
            "failed_count": result["failed_count"],
            "scanned": result["scanned"],
        }
        session.flush()
        result["job_id"] = job.id
    else:
        result["job_id"] = None
    return result
