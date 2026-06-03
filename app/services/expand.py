"""Playlist / channel expansion (Phase 2A).

Flat-extracts a playlist or channel(-tab) URL into video ids (no media download),
upserts a ``collection`` + ``collection_items`` with diff detection, and creates
child ``download`` jobs only for videos that do not already have an active job.

Extraction (network) and the DB diff are split so the diff logic is unit-testable
without yt-dlp / network: ``flat_extract`` does I/O, ``process_entries`` is pure DB.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Collection, CollectionItem, DownloadProfile, Job, Source, utcnow
from app.services.command_builder import external_ctx
from app.services.jobs import create_download_child_job
from app.services.urls import (
    canonical_video_url,
    collection_type_for,
    is_video_id,
    normalize_url,
)
from app.services.ytdlp import run_ytdlp

# Statuses that mean "this video already has a download job we should not duplicate".
_ACTIVE_DOWNLOAD_STATUSES = ("queued", "running", "success", "partial_success")


@dataclass
class ExpandResult:
    collection_id: int
    discovered_count: int
    created_jobs_count: int
    skipped_existing_count: int
    removed_count: int
    capped: bool
    child_job_ids: list[int] = field(default_factory=list)

    def as_meta(self) -> dict:
        return {
            "collection_id": self.collection_id,
            "discovered_count": self.discovered_count,
            "created_jobs_count": self.created_jobs_count,
            "skipped_existing_count": self.skipped_existing_count,
            "removed_count": self.removed_count,
            "capped": self.capped,
        }


# --------------------------------------------------------------------------- #
# Extraction (network / subprocess)
# --------------------------------------------------------------------------- #
def _flat_extract_argv(settings: Settings, cap: int) -> list[str]:
    args = ["--ignore-config", "--flat-playlist", "--skip-download", "--dump-single-json"]
    if cap and cap > 0:
        args += ["-I", f"1:{cap}"]  # extract at most `cap` items (avoid runaway)
    if settings.ytdlp_retry_backoff_seconds and settings.ytdlp_retry_backoff_seconds > 0:
        args += ["--retry-sleep", str(settings.ytdlp_retry_backoff_seconds)]
    ext = external_ctx(settings)
    if ext["deno_path"]:
        args += ["--js-runtimes", f"deno:{ext['deno_path']}"]
    if ext["remote_components"]:
        args += ["--remote-components", ext["remote_components"]]
    if ext["cookies_file"]:
        args += ["--cookies", ext["cookies_file"]]
    return args


def flat_extract(
    settings: Settings, url: str, log_dir: Path, cap: int
) -> tuple[dict, list[dict], bool]:
    """Run yt-dlp flat extraction; returns (playlist_info, entries, capped).

    Writes command.txt / stdout / stderr into ``log_dir`` (re-runnable, logged).
    Raises RuntimeError on failure (caller records logs + marks the job failed).
    """
    parsed = normalize_url(url)
    argv = _flat_extract_argv(settings, cap)
    run = run_ytdlp(
        argv,
        log_dir,
        url=parsed.canonical_url,
        settings=settings,
        timeout=settings.ytdlp_timeout or None,
    )
    if not run.ok:
        raise RuntimeError(
            f"yt-dlp flat extraction exited {run.returncode}\n{_tail(run.stderr_path)}"
        )
    try:
        info = json.loads(run.stdout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not parse flat extraction JSON: {exc}")
    if not isinstance(info, dict):
        raise RuntimeError("unexpected flat extraction output (not a JSON object)")
    entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
    capped = bool(cap and cap > 0 and len(entries) >= cap)
    return info, entries, capped


# --------------------------------------------------------------------------- #
# Diff + child-job creation (pure DB; unit-testable)
# --------------------------------------------------------------------------- #
def process_entries(
    session: Session,
    settings: Settings,
    *,
    url: str,
    info: dict | None,
    entries: list[dict],
    capped: bool,
    profile_name: str,
    parent_job_id: int | None = None,
    detect_removed: bool = True,
) -> ExpandResult:
    parsed = normalize_url(url)
    ctype = collection_type_for(parsed)
    collection = _upsert_collection(session, parsed, ctype, info or {}, profile_name)

    now = utcnow()
    existing = {
        it.youtube_video_id: it for it in collection.items if it.youtube_video_id
    }
    seen: list[str] = []
    seen_set: set[str] = set()

    for pos, entry in enumerate(entries):
        vid = entry.get("id")
        if not is_video_id(vid) or vid in seen_set:
            continue
        seen_set.add(vid)
        seen.append(vid)
        item = existing.get(vid)
        if item is None:
            item = CollectionItem(
                collection_id=collection.id,
                youtube_video_id=vid,
                position=pos,
                discovered_at=now,
                last_seen_at=now,
                raw_json=_slim(entry),
            )
            session.add(item)
            existing[vid] = item
        else:
            item.last_seen_at = now
            item.position = pos
            item.removed_at = None
            item.raw_json = _slim(entry)

    removed = 0
    # Only mark removals on a refresh that saw the whole list (not capped).
    if detect_removed and not capped:
        for vid, item in existing.items():
            if vid not in seen_set and item.removed_at is None:
                item.removed_at = now
                removed += 1
    session.flush()

    created_ids: list[int] = []
    skipped = 0
    for vid in seen:
        if _has_active_download_job(session, vid):
            skipped += 1
            continue
        child = create_download_child_job(
            session,
            vid,
            profile_name,
            parent_job_id=parent_job_id,
            collection_id=collection.id,
        )
        created_ids.append(child.id)
    session.flush()

    return ExpandResult(
        collection_id=collection.id,
        discovered_count=len(seen),
        created_jobs_count=len(created_ids),
        skipped_existing_count=skipped,
        removed_count=removed,
        capped=capped,
        child_job_ids=created_ids,
    )


def _upsert_collection(
    session: Session,
    parsed,
    ctype: str,
    info: dict,
    profile_name: str,
) -> Collection:
    canon = parsed.canonical_url
    collection = session.scalar(select(Collection).where(Collection.url == canon))
    if collection is None:
        source = Source(
            type=parsed.kind, url=canon, name=info.get("title"), api_source="manual"
        )
        session.add(source)
        session.flush()
        collection = Collection(
            source_id=source.id, url=canon, type=ctype, crawl_policy="new_only"
        )
        session.add(collection)

    collection.type = ctype
    if not collection.crawl_policy:
        collection.crawl_policy = "new_only"
    if parsed.playlist_id:
        collection.youtube_playlist_id = parsed.playlist_id
    channel_id = parsed.channel_id or info.get("channel_id")
    if channel_id:
        collection.youtube_channel_id = channel_id
    if info.get("title"):
        collection.title = info["title"]

    prow = session.scalar(
        select(DownloadProfile).where(DownloadProfile.name == profile_name)
    )
    if prow is not None:
        collection.download_profile_id = prow.id
    session.flush()
    return collection


def _has_active_download_job(session: Session, youtube_video_id: str) -> bool:
    url = canonical_video_url(youtube_video_id)
    found = session.scalar(
        select(Job.id)
        .where(
            Job.type == "download",
            Job.url == url,
            Job.status.in_(_ACTIVE_DOWNLOAD_STATUSES),
        )
        .limit(1)
    )
    return found is not None


def _slim(entry: dict) -> dict:
    keys = ("id", "title", "url", "ie_key", "_type", "duration", "channel_id")
    return {k: entry.get(k) for k in keys if k in entry}


def _tail(path: Path, max_chars: int = 4000) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""
