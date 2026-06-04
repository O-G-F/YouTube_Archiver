"""YouTube Data API OAuth — differential liked-videos sync (Phase 6B).

This is the **incremental update** path, NOT the bulk/first-time path: the API
practically returns only the most recent ~5000 liked videos, so the historical
backfill comes from Google Takeout "My Activity" (see services.takeout). Here we
walk the API newest-first and STOP when we reach a video already in the DB.

Design notes:
  - DEFAULT DISABLED. The module imports cleanly without google libraries; the
    real fetcher lazy-imports them and raises a *classified* error if missing or
    if OAuth is not configured. So the whole app starts safely with no OAuth.
  - Secrets (client_secret / token) are files under /config or /secrets and are
    never returned by the API or logged.
  - ``sync_liked`` takes an injectable ``fetcher`` so the merge/dedup/stop logic
    is unit-testable without real credentials.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.logging_setup import get_logger
from app.models import LikedVideo, Video, utcnow
from app.services.urls import canonical_video_url

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

# error classification keys (surfaced to the UI / job classification)
AUTH_REQUIRED = "auth_required"
QUOTA_EXCEEDED = "quota_exceeded"
FORBIDDEN = "forbidden"
TOKEN_EXPIRED = "token_expired"
RATE_LIMITED = "rate_limited"
API_ERROR = "api_error"

_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}
_AUTH_REASONS = {"authError", "unauthorized", "invalid_grant", "invalidCredentials"}
_FORBIDDEN_REASONS = {"forbidden", "insufficientPermissions", "accessNotConfigured"}


class YouTubeApiError(Exception):
    """API/OAuth failure carrying a UI-friendly classification key."""

    def __init__(self, classification: str, message: str):
        super().__init__(message)
        self.classification = classification
        self.message = message


def classify_api_error(status: int | None, reason: str | None) -> str:
    reason = reason or ""
    low = reason.lower()
    if reason in _QUOTA_REASONS or "quota" in low or status == 429:
        return QUOTA_EXCEEDED if "quota" in low or reason in _QUOTA_REASONS else RATE_LIMITED
    if reason in _AUTH_REASONS or status == 401:
        return TOKEN_EXPIRED if "grant" in low or "expire" in low else AUTH_REQUIRED
    if reason in _FORBIDDEN_REASONS or status == 403:
        return FORBIDDEN
    return API_ERROR


def is_configured(settings: Settings) -> bool:
    return settings.youtube_api_configured


def status_dict(settings: Settings) -> dict:
    """Non-secret status for the Settings/Library UI (no paths leaked)."""
    cs = settings.youtube_client_secret_path
    return {
        "enabled": settings.youtube_api_enabled,
        "client_secret_present": bool(cs and cs.is_file()),
        "token_present": settings.youtube_token_path.is_file(),
        "configured": settings.youtube_api_configured,
        "method": settings.youtube_api_liked_method,
    }


# --------------------------------------------------------------------------- #
# Real fetcher (lazy google imports) — runs only when configured.
# --------------------------------------------------------------------------- #
def _load_credentials(settings: Settings):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise YouTubeApiError(
            AUTH_REQUIRED,
            "google-api-python-client / google-auth not installed; run the authorize flow first",
        ) from exc

    token_path = settings.youtube_token_path
    if not token_path.is_file():
        raise YouTubeApiError(AUTH_REQUIRED, "OAuth token not found; run `youtube-api authorize` first")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                token_path.write_text(creds.to_json(), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                raise YouTubeApiError(TOKEN_EXPIRED, f"OAuth token refresh failed: {exc}") from exc
        else:
            raise YouTubeApiError(TOKEN_EXPIRED, "OAuth token expired and cannot be refreshed")
    return creds


def _build_client(settings: Settings):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise YouTubeApiError(AUTH_REQUIRED, "googleapiclient not installed") from exc
    creds = _load_credentials(settings)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _http_error_reason(error) -> tuple[int | None, str]:
    import json as _json

    resp = getattr(error, "resp", None)
    status = getattr(resp, "status", None)
    reason = ""
    content = getattr(error, "content", b"")
    try:
        body = _json.loads(content.decode("utf-8") if isinstance(content, bytes) else str(content))
        errs = (body.get("error") or {}).get("errors") or []
        if errs and isinstance(errs[0], dict):
            reason = errs[0].get("reason", "")
    except Exception:  # noqa: BLE001
        pass
    return status, reason


def _record(video_id, *, title=None, channel_title=None, channel_id=None, published_at=None,
            thumbnail_url=None, raw=None) -> dict:
    return {
        "video_id": video_id,
        "title": title,
        "channel_title": channel_title,
        "channel_id": channel_id,
        "published_at": published_at,
        "thumbnail_url": thumbnail_url,
        "raw": raw or {},
    }


def real_fetcher(settings: Settings, *, method: str | None = None, page_size: int = 50):
    """Return an iterator factory that yields liked-video records newest-first.

    Method A ("videos"): videos.list(myRating=like). Method B ("playlist"):
    channels.list -> relatedPlaylists.likes -> playlistItems.list. "auto" tries
    videos then falls back to playlist.
    """
    method = method or settings.youtube_api_liked_method or "auto"

    def _iter() -> Iterator[dict]:
        try:
            from googleapiclient.errors import HttpError
        except ImportError as exc:  # pragma: no cover
            raise YouTubeApiError(AUTH_REQUIRED, "googleapiclient not installed") from exc
        client = _build_client(settings)

        def videos_method() -> Iterator[dict]:
            page_token = None
            while True:
                req = client.videos().list(
                    part="snippet,contentDetails", myRating="like",
                    maxResults=page_size, pageToken=page_token,
                )
                resp = req.execute()
                for item in resp.get("items", []):
                    sn = item.get("snippet", {})
                    thumbs = sn.get("thumbnails", {}) or {}
                    thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url")
                    yield _record(
                        item.get("id"), title=sn.get("title"), channel_title=sn.get("channelTitle"),
                        channel_id=sn.get("channelId"), published_at=sn.get("publishedAt"),
                        thumbnail_url=thumb, raw=item,
                    )
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        def playlist_method() -> Iterator[dict]:
            ch = client.channels().list(part="contentDetails", mine=True, maxResults=5).execute()
            likes_id = None
            for it in ch.get("items", []):
                likes_id = (it.get("contentDetails", {}).get("relatedPlaylists", {}) or {}).get("likes")
                if likes_id:
                    break
            if not likes_id:
                raise YouTubeApiError(API_ERROR, "could not resolve the liked-videos playlist")
            page_token = None
            while True:
                resp = client.playlistItems().list(
                    part="snippet,contentDetails", playlistId=likes_id,
                    maxResults=page_size, pageToken=page_token,
                ).execute()
                for item in resp.get("items", []):
                    sn = item.get("snippet", {})
                    cd = item.get("contentDetails", {})
                    vid = cd.get("videoId") or (sn.get("resourceId", {}) or {}).get("videoId")
                    thumbs = sn.get("thumbnails", {}) or {}
                    thumb = (thumbs.get("medium") or thumbs.get("default") or {}).get("url")
                    yield _record(
                        vid, title=sn.get("title"), channel_title=sn.get("videoOwnerChannelTitle"),
                        channel_id=sn.get("videoOwnerChannelId"), published_at=sn.get("publishedAt"),
                        thumbnail_url=thumb, raw=item,
                    )
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        try:
            if method == "playlist":
                yield from playlist_method()
            elif method == "videos":
                yield from videos_method()
            else:  # auto
                try:
                    yield from videos_method()
                except HttpError:
                    yield from playlist_method()
        except HttpError as exc:
            status, reason = _http_error_reason(exc)
            raise YouTubeApiError(classify_api_error(status, reason), f"YouTube API error: {reason or exc}") from exc

    return _iter


def authorize(settings: Settings) -> Path:
    """Run the installed-app OAuth flow (needs a browser) and store the token.

    Only used interactively (CLI ``youtube-api authorize``); the sync itself uses
    the stored token. Returns the token path.
    """
    cs = settings.youtube_client_secret_path
    if not cs or not cs.is_file():
        raise YouTubeApiError(AUTH_REQUIRED, "client secret file not found (set YOUTUBE_OAUTH_CLIENT_SECRET_FILE)")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover
        raise YouTubeApiError(AUTH_REQUIRED, "google-auth-oauthlib not installed") from exc
    flow = InstalledAppFlow.from_client_secrets_file(str(cs), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path: Path = settings.youtube_token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return token_path


# --------------------------------------------------------------------------- #
# Differential sync (testable with an injected fetcher)
# --------------------------------------------------------------------------- #
def _published_to_upload_date(published_at: str | None) -> str | None:
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        return dt.strftime("%Y%m%d")
    except ValueError:
        return None


def sync_liked(
    session: Session,
    settings: Settings,
    *,
    fetcher: Callable[[], Iterable[dict]] | None = None,
    method: str | None = None,
    stop_on_existing: bool = True,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Differentially sync liked videos from the API into ``liked_videos``.

    Walks newest-first; with ``stop_on_existing`` it stops at the first video
    already in the DB (Takeout or a prior sync). New rows get
    ``source=youtube_data_api`` and enrich the linked Video stub
    (title/channel/channel_id/upload_date). Raises :class:`YouTubeApiError`
    (classified) if not configured.
    """
    if fetcher is None:
        if not is_configured(settings):
            raise YouTubeApiError(
                AUTH_REQUIRED,
                "YouTube Data API is not configured (set YOUTUBE_API_ENABLED + client secret + authorize)",
            )
        fetcher = real_fetcher(settings, method=method)

    existing_ids = {
        v for v in session.scalars(select(LikedVideo.youtube_video_id)) if v
    }
    imported = skipped = failed = scanned = videos_created = 0
    stopped_on_existing = False
    seen: set[str] = set()

    for rec in fetcher():
        if limit is not None and scanned >= limit:
            break
        scanned += 1
        vid = rec.get("video_id")
        if not vid:
            failed += 1
            continue
        if vid in existing_ids or vid in seen:
            if stop_on_existing:
                stopped_on_existing = True
                break
            skipped += 1
            continue
        seen.add(vid)
        imported += 1
        if dry_run:
            continue

        video_pk = None
        from app.services.takeout import _find_or_create_video  # local import avoids cycle

        video, created = _find_or_create_video(session, vid)
        if created:
            videos_created += 1
        if rec.get("title") and not video.title:
            video.title = rec["title"]
        if rec.get("channel_title") and not video.channel_title:
            video.channel_title = rec["channel_title"]
        if rec.get("channel_id") and not video.channel_id:
            video.channel_id = rec["channel_id"]
        upload = _published_to_upload_date(rec.get("published_at"))
        if upload and not video.upload_date:
            video.upload_date = upload
        video_pk = video.id
        session.add(
            LikedVideo(
                source="youtube_data_api",
                youtube_video_id=vid,
                title=rec.get("title"),
                channel_title=rec.get("channel_title"),
                url=canonical_video_url(vid),
                liked_at=None,  # the API does not expose the like timestamp
                video_id=video_pk,
                raw_json={"thumbnail_url": rec.get("thumbnail_url"), "published_at": rec.get("published_at")},
            )
        )

    if not dry_run:
        session.flush()
    logger.info(
        "youtube-api sync_liked: scanned=%d imported=%d skipped=%d stopped_on_existing=%s",
        scanned, imported, skipped, stopped_on_existing,
    )
    return {
        "imported_count": imported,
        "skipped_duplicate_count": skipped,
        "failed_count": failed,
        "scanned": scanned,
        "videos_created": videos_created,
        "stopped_on_existing": stopped_on_existing,
        "source": "youtube_data_api",
        "dry_run": dry_run,
    }
