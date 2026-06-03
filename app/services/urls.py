"""YouTube URL normalization and classification (requirement 5.1.3).

Pure functions, no I/O, fully unit-tested. Recognizes:
  - video URLs (watch, youtu.be, /shorts/, /live/, /embed/)
  - playlist URLs (/playlist?list=, watch?...&list=)
  - channel URLs (/channel/UC..., /@handle, /c/name, /user/name) and their tabs
    (/videos /shorts /streams /playlists /community)
  - YouTube Music URLs (music.youtube.com)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
_CHANNEL_TABS = {"videos", "shorts", "streams", "playlists", "community", "featured"}


class UrlError(ValueError):
    """Raised when a URL cannot be recognized as a supported YouTube URL."""


@dataclass(frozen=True)
class ParsedUrl:
    kind: str  # "video" | "playlist" | "channel" | "unknown"
    canonical_url: str
    raw_url: str
    video_id: str | None = None
    playlist_id: str | None = None
    channel_id: str | None = None
    channel_handle: str | None = None
    channel_name: str | None = None
    channel_tab: str | None = None
    is_music: bool = False
    is_short: bool = False
    is_live: bool = False
    extra: dict = field(default_factory=dict)


def is_video_id(value: str | None) -> bool:
    return bool(value and _VIDEO_ID_RE.match(value))


def canonical_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def canonical_playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _host(netloc: str) -> str:
    return netloc.split("@")[-1].split(":")[0].lower()


def _is_youtube_host(host: str) -> bool:
    return host in _YOUTUBE_HOSTS or host.endswith(".youtube.com")


def extract_video_id(raw_url: str) -> str | None:
    """Best-effort extraction of a video id from any YouTube URL form."""
    try:
        parsed = normalize_url(raw_url)
    except UrlError:
        return None
    return parsed.video_id


def normalize_url(raw_url: str) -> ParsedUrl:
    """Classify and normalize a YouTube URL into a :class:`ParsedUrl`."""
    if not raw_url or not raw_url.strip():
        raise UrlError("empty URL")
    raw = raw_url.strip()
    candidate = raw if "://" in raw else f"https://{raw}"
    p = urlparse(candidate)
    host = _host(p.netloc)
    if not _is_youtube_host(host):
        raise UrlError(f"not a YouTube URL: {raw_url!r}")

    is_music = host == "music.youtube.com"
    path = p.path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    query = parse_qs(p.query)

    # ----- youtu.be/<id> -----
    if host == "youtu.be":
        if segments and _VIDEO_ID_RE.match(segments[0]):
            vid = segments[0]
            return ParsedUrl(
                kind="video",
                canonical_url=canonical_video_url(vid),
                raw_url=raw,
                video_id=vid,
                playlist_id=_first(query.get("list")),
                is_music=is_music,
            )
        raise UrlError(f"unrecognized youtu.be URL: {raw_url!r}")

    # ----- /watch?v=<id> -----
    if segments[:1] == ["watch"] or path == "/watch":
        vid = _first(query.get("v"))
        if vid and _VIDEO_ID_RE.match(vid):
            return ParsedUrl(
                kind="video",
                canonical_url=canonical_video_url(vid),
                raw_url=raw,
                video_id=vid,
                playlist_id=_first(query.get("list")),
                is_music=is_music,
            )
        # watch with only a list -> treat as playlist
        plist = _first(query.get("list"))
        if plist:
            return ParsedUrl(
                kind="playlist",
                canonical_url=canonical_playlist_url(plist),
                raw_url=raw,
                playlist_id=plist,
                is_music=is_music,
            )
        raise UrlError(f"watch URL without a valid video id: {raw_url!r}")

    # ----- /shorts/<id>, /live/<id>, /embed/<id> -----
    if len(segments) >= 2 and segments[0] in {"shorts", "live", "embed", "v"}:
        vid = segments[1]
        if _VIDEO_ID_RE.match(vid):
            return ParsedUrl(
                kind="video",
                canonical_url=canonical_video_url(vid),
                raw_url=raw,
                video_id=vid,
                playlist_id=_first(query.get("list")),
                is_short=segments[0] == "shorts",
                is_live=segments[0] == "live",
                is_music=is_music,
            )
        raise UrlError(f"unrecognized {segments[0]} URL: {raw_url!r}")

    # ----- /playlist?list=<id> -----
    if segments[:1] == ["playlist"]:
        plist = _first(query.get("list"))
        if plist:
            return ParsedUrl(
                kind="playlist",
                canonical_url=canonical_playlist_url(plist),
                raw_url=raw,
                playlist_id=plist,
                is_music=is_music,
            )
        raise UrlError(f"playlist URL without list id: {raw_url!r}")

    # ----- channels -----
    if segments:
        first = segments[0]
        tab = _channel_tab(segments)

        if first == "channel" and len(segments) >= 2:
            cid = segments[1]
            base = f"https://www.youtube.com/channel/{cid}"
            return ParsedUrl(
                kind="channel",
                canonical_url=_with_tab(base, tab),
                raw_url=raw,
                channel_id=cid if _CHANNEL_ID_RE.match(cid) else None,
                channel_name=None if _CHANNEL_ID_RE.match(cid) else cid,
                channel_tab=tab,
                is_music=is_music,
            )

        if first.startswith("@"):
            base = f"https://www.youtube.com/{first}"
            return ParsedUrl(
                kind="channel",
                canonical_url=_with_tab(base, tab),
                raw_url=raw,
                channel_handle=first,
                channel_tab=tab,
                is_music=is_music,
            )

        if first in {"c", "user"} and len(segments) >= 2:
            name = segments[1]
            base = f"https://www.youtube.com/{first}/{name}"
            return ParsedUrl(
                kind="channel",
                canonical_url=_with_tab(base, tab),
                raw_url=raw,
                channel_name=name,
                channel_tab=tab,
                is_music=is_music,
            )

    raise UrlError(f"unsupported YouTube URL: {raw_url!r}")


def _channel_tab(segments: list[str]) -> str | None:
    for seg in segments:
        if seg in _CHANNEL_TABS:
            return None if seg == "featured" else seg
    return None


def _with_tab(base: str, tab: str | None) -> str:
    return f"{base}/{tab}" if tab else base


# Channel tab -> collection type (DB ``collections.type``).
_TAB_TO_COLLECTION = {
    "videos": "channel_videos",
    "shorts": "channel_shorts",
    "streams": "channel_streams",
}


def classify(raw_url: str) -> str:
    """Fine-grained classification string.

    One of: ``video``, ``playlist``, ``channel``, ``channel_videos``,
    ``channel_shorts``, ``channel_streams``, ``unknown``. ``channel`` is the
    root/ambiguous channel page; the tab-specific forms come from /videos,
    /shorts, /streams.
    """
    try:
        parsed = normalize_url(raw_url)
    except UrlError:
        return "unknown"
    if parsed.kind == "channel":
        return _TAB_TO_COLLECTION.get(parsed.channel_tab or "", "channel")
    return parsed.kind


def collection_type_for(parsed: ParsedUrl) -> str:
    """Collection type to store in the DB for an expandable URL.

    A bare channel root is treated as ``channel_videos`` (its uploads tab).
    """
    if parsed.kind == "playlist":
        return "playlist"
    if parsed.kind == "channel":
        return _TAB_TO_COLLECTION.get(parsed.channel_tab or "", "channel_videos")
    return parsed.kind


def channel_tab_url(parsed: ParsedUrl, tab: str) -> str:
    """Build the URL for a specific tab of a channel (drops any existing tab)."""
    base = parsed.canonical_url
    if parsed.channel_tab:
        base = base.rsplit("/", 1)[0]
    return f"{base}/{tab}"


_CRAWLABLE_TABS = ("videos", "shorts", "streams")


def resolve_channel_tabs(
    parsed: ParsedUrl, videos: bool, shorts: bool, streams: bool
) -> list[str]:
    """Decide which channel tabs to crawl.

    - Explicit flags win.
    - No flags but the URL already targets a crawlable tab -> just that tab.
    - No flags on a channel *root* URL -> error (avoid accidental full crawl).
    """
    tabs = [
        t
        for t, on in (("videos", videos), ("shorts", shorts), ("streams", streams))
        if on
    ]
    if tabs:
        return tabs
    if parsed.channel_tab in _CRAWLABLE_TABS:
        return [parsed.channel_tab]
    raise UrlError(
        "channel root URL: specify at least one of --videos / --shorts / --streams"
    )


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0] or None
