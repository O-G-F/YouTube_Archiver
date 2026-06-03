"""Pydantic request/response models for the Web API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ArchiveUrlRequest(BaseModel):
    url: str
    profile: str | None = None
    priority: int = 0


class ArchiveBatchRequest(BaseModel):
    urls: list[str]
    profile: str | None = None
    priority: int = 0


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    status: str
    url: str | None = None
    profile_name: str | None = None
    video_id: int | None = None
    collection_id: int | None = None
    parent_job_id: int | None = None
    rq_job_id: str | None = None
    priority: int
    progress: float
    error_message: str | None = None
    log_path: str | None = None
    command_path: str | None = None
    meta: dict | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class BatchItemResult(BaseModel):
    url: str
    job_id: int | None = None
    error: str | None = None


class BatchResult(BaseModel):
    created: int
    failed: int
    results: list[BatchItemResult]


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    media_mode: str
    quality_mode: str | None = None
    description: str | None = None
    is_builtin: bool = False


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    youtube_video_id: str
    title: str | None = None
    channel_id: str | None = None
    channel_title: str | None = None
    url: str | None = None
    duration: int | None = None
    upload_date: str | None = None
    is_short: bool = False
    is_live: bool = False
    availability: str | None = None
    thumbnail_path: str | None = None
    first_seen_at: datetime
    last_metadata_refresh_at: datetime | None = None


class MediaFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    media_type: str
    profile: str | None = None
    path: str
    container: str | None = None
    width: int | None = None
    height: int | None = None
    filesize: int | None = None


class VideoDetailOut(VideoOut):
    media_files: list[MediaFileOut] = Field(default_factory=list)
    subtitle_count: int = 0
    comment_count: int = 0


class JobLogOut(BaseModel):
    job_id: int
    command: str | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None


class HealthOut(BaseModel):
    status: str
    version: str
    ytdlp_version: str | None = None
    database: bool
    redis: bool


class JobLogsOut(BaseModel):
    job_id: int
    log_path: str | None = None
    available: bool = False
    command: str | None = None
    stdout: str | None = None
    stderr: str | None = None


class JobDetailOut(JobOut):
    stdout_log_path: str | None = None
    stderr_log_path: str | None = None
    command_log_path: str | None = None
    output_dir: str | None = None
    video: VideoOut | None = None
    profile: ProfileOut | None = None


class BuildCommandRequest(BaseModel):
    url: str


class BuildCommandOut(BaseModel):
    profile: str
    url: str
    kind: str | None = None
    argv: list[str]
    command: str
    note: str | None = None


class DoctorCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class DoctorOut(BaseModel):
    ok: bool
    checks: list[DoctorCheck]


# --------------------------------------------------------------------------- #
# Collections (Phase 2A)
# --------------------------------------------------------------------------- #
class CollectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    title: str | None = None
    url: str | None = None
    youtube_playlist_id: str | None = None
    youtube_channel_id: str | None = None
    download_profile_id: int | None = None
    crawl_policy: str | None = None
    enabled: bool = True
    created_at: datetime
    updated_at: datetime
    item_count: int = 0


class CollectionItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    collection_id: int
    youtube_video_id: str | None = None
    video_id: int | None = None
    position: int | None = None
    discovered_at: datetime
    last_seen_at: datetime | None = None
    removed_at: datetime | None = None


class PlaylistSourceRequest(BaseModel):
    url: str
    profile: str | None = None
    max_items: int | None = None


class ChannelSourceRequest(BaseModel):
    url: str
    profile: str | None = None
    videos: bool = False
    shorts: bool = False
    streams: bool = False
    max_items: int | None = None


class ExpandRequest(BaseModel):
    url: str
    profile: str | None = None
    max_items: int | None = None


class CollectionPatch(BaseModel):
    enabled: bool | None = None
    crawl_policy: str | None = None  # manual | new_only | refresh
    profile: str | None = None       # download profile name


class CollectionRefreshResult(BaseModel):
    collection_id: int
    job_id: int
    status: str
    meta: dict | None = None


class RefreshAllResult(BaseModel):
    collections_checked: int
    jobs_created: int
    job_ids: list[int]


class SchedulerStatusOut(BaseModel):
    enabled: bool
    interval_seconds: int
    enabled_collections: int
    crawlable_collections: int


# --------------------------------------------------------------------------- #
# Takeout (Phase 3A)
# --------------------------------------------------------------------------- #
class TakeoutFileOut(BaseModel):
    name: str
    kind: str
    format: str
    size: int


class TakeoutSampleOut(BaseModel):
    youtube_video_id: str | None = None
    title: str | None = None
    channel_title: str | None = None
    watched_at: str | None = None


class TakeoutPreviewRequest(BaseModel):
    path: str


class TakeoutPreviewOut(BaseModel):
    path: str
    files: list[TakeoutFileOut] = Field(default_factory=list)
    watch_history_count: int = 0
    search_history_count: int = 0
    likes_count: int = 0
    subscriptions_count: int = 0
    playlists_count: int = 0
    samples: list[TakeoutSampleOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TakeoutImportRequest(BaseModel):
    path: str
    limit: int | None = None
    dry_run: bool = False


class TakeoutImportOut(BaseModel):
    imported_count: int
    skipped_duplicate_count: int
    failed_count: int
    scanned: int
    dry_run: bool
    job_id: int | None = None
    warnings: list[str] = Field(default_factory=list)


class WatchHistoryEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str | None = None
    youtube_video_id: str | None = None
    title: str | None = None
    channel_title: str | None = None
    watched_at: datetime | None = None
    raw_json: dict | None = None  # only populated when include_raw=true


class ChannelCount(BaseModel):
    channel_title: str | None = None
    count: int


class WatchHistoryStatsOut(BaseModel):
    total: int
    with_video_id: int
    distinct_videos: int
    distinct_channels: int
    earliest: datetime | None = None
    latest: datetime | None = None
    top_channels: list[ChannelCount] = Field(default_factory=list)
