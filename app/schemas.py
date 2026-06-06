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
    retry_count: int = 0
    retry_of_job_id: int | None = None
    next_retry_at: datetime | None = None
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
    # Phase 4A/4B state (read-only; surfaced for the admin UI).
    comments_state: str | None = None
    comments_refresh_policy: str | None = None
    last_comments_refresh_at: datetime | None = None
    next_comments_refresh_at: datetime | None = None
    live_chat_state: str | None = None
    has_live_chat: bool = False
    last_live_chat_refresh_at: datetime | None = None
    next_live_chat_refresh_at: datetime | None = None


class VideoListItemOut(VideoOut):
    """Video list row with media-body count + thumbnail flag (Phase 5A/5B UI)."""

    media_files_count: int = 0  # video/audio BODY files only (0 -> 未保存)
    has_thumbnail: bool = False


class RelatedVideosOut(BaseModel):
    """Related videos for the player sidebar (Phase 5B)."""

    same_channel: list[VideoListItemOut] = Field(default_factory=list)
    same_collection: list[VideoListItemOut] = Field(default_factory=list)


class ChannelOut(BaseModel):
    """A distinct channel with its video count (Videos filter dropdown)."""

    channel_id: str | None = None
    channel_title: str | None = None
    count: int = 0


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
    description: str | None = None  # public video description (detail view only)
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


class JobClassification(BaseModel):
    """Derived, human-friendly hints about a job's outcome (Phase 5B/6A)."""

    rate_limited: bool = False  # HTTP 429 seen
    partial: bool = False  # finished with usable output despite non-zero exit
    retryable: bool = False
    reasons: list[str] = Field(default_factory=list)  # machine keys (rate_limited, incomplete_data, …)
    warnings: list[str] = Field(default_factory=list)  # human-readable notes
    summary: str | None = None  # short headline for the UI


class JobOutClassified(JobOut):
    """Job list row plus a derived classification (Phase 5B job UI)."""

    classification: JobClassification = Field(default_factory=JobClassification)


class JobDetailOut(JobOut):
    stdout_log_path: str | None = None
    stderr_log_path: str | None = None
    command_log_path: str | None = None
    output_dir: str | None = None
    video: VideoOut | None = None
    profile: ProfileOut | None = None
    classification: JobClassification = Field(default_factory=JobClassification)


# --------------------------------------------------------------------------- #
# Phase 7A: retry + subtitles refresh
# --------------------------------------------------------------------------- #
class JobRetryAllRequest(BaseModel):
    reason: str | None = None  # only retry jobs whose classification has this reason
    type: str | None = None    # only retry jobs of this type
    limit: int = 50


class JobRetryAllOut(BaseModel):
    retried: int
    job_ids: list[int] = Field(default_factory=list)


class SubtitlesRefreshRequest(BaseModel):
    target: str | None = None  # video id / URL (official)
    video: str | None = None   # backward-compatible alias
    profile: str | None = None
    now: bool = False

    def resolved_target(self) -> str | None:
        return self.target if self.target is not None else self.video

    def has_conflict(self) -> bool:
        return self.target is not None and self.video is not None


class SubtitlesRefreshAllOut(BaseModel):
    videos_selected: int
    jobs_created: int
    job_ids: list[int] = Field(default_factory=list)


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
    comments_enabled: bool = False
    comments_limit_per_run: int = 0
    due_comment_videos: int = 0
    frozen_comment_videos: int = 0


class SchedulerRunOnceRequest(BaseModel):
    # Which parts to run for this manual cycle.
    collections: bool = True
    comments: bool = True
    max_items: int | None = None
    # Phase 7D liked passes (default off; opt in explicitly).
    liked_metadata: bool = False
    liked_archive: bool = False
    liked_retry: bool = False


class SchedulerRunOnceOut(BaseModel):
    enabled: bool
    reason: str
    collections_checked: int = 0
    collection_jobs_created: int = 0
    due_comment_videos_checked: int = 0
    comments_jobs_created: int = 0
    retries_requeued: int = 0
    # Phase 7D liked passes
    liked_metadata_selected: int = 0
    liked_metadata_jobs_created: int = 0
    liked_archive_selected: int = 0
    liked_archive_jobs_created: int = 0
    liked_retry_selected: int = 0
    liked_retry_jobs_requeued: int = 0
    skipped_active_jobs: int = 0
    skipped_duplicates: int = 0
    skipped_frozen: int = 0
    skipped_recent: int = 0
    jobs_created: int = 0
    submitted: int = 0
    job_ids: list[int] = Field(default_factory=list)


# ---- Phase 7D: liked-archive progress + queue health ----
class LikedChannelCount(BaseModel):
    channel: str
    count: int


class LikedProgressOut(BaseModel):
    total_liked: int
    metadata_fetched: int
    metadata_missing: int
    body_saved: int
    body_missing: int
    active_archive_jobs: int
    retryable_liked_jobs: int
    failed_liked_jobs: int
    partial_liked_jobs: int
    by_source: dict[str, int] = Field(default_factory=dict)
    by_channel: list[LikedChannelCount] = Field(default_factory=list)
    earliest_liked_at: str | None = None
    latest_liked_at: str | None = None
    last_archive_job_at: str | None = None
    last_successful_archive_at: str | None = None


class QueueStatusOut(BaseModel):
    queued: int
    running: int
    total_active: int
    by_type: dict[str, int] = Field(default_factory=dict)
    by_source_action: dict[str, int] = Field(default_factory=dict)
    oldest_queued_at: str | None = None
    oldest_queued_job_id: int | None = None
    worker_count: int | None = None


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


class SubscriptionSampleOut(BaseModel):
    channel_id: str | None = None
    channel_title: str | None = None


class PlaylistSampleOut(BaseModel):
    title: str
    playlist_id: str | None = None
    item_count: int = 0


class LikedVideoSampleOut(BaseModel):
    youtube_video_id: str | None = None
    title: str | None = None
    liked_at: str | None = None


class TakeoutPreviewOut(BaseModel):
    path: str
    archive_kind: str | None = None  # youtube_takeout | my_activity_takeout | takeout_index | …
    liked_source_kind: str | None = None
    liked_detected_path: str | None = None
    files: list[TakeoutFileOut] = Field(default_factory=list)
    watch_history_count: int = 0
    search_history_count: int = 0
    likes_count: int = 0
    subscriptions_count: int = 0
    playlists_count: int = 0
    samples: list[TakeoutSampleOut] = Field(default_factory=list)
    search_samples: list[str] = Field(default_factory=list)
    subscription_samples: list[SubscriptionSampleOut] = Field(default_factory=list)
    playlist_samples: list[PlaylistSampleOut] = Field(default_factory=list)
    liked_samples: list[LikedVideoSampleOut] = Field(default_factory=list)
    importable: dict = Field(default_factory=dict)
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


# --------------------------------------------------------------------------- #
# Phase 3B: search history / subscriptions / playlists
# --------------------------------------------------------------------------- #
class SearchHistoryEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str | None = None
    query: str | None = None
    searched_at: datetime | None = None
    raw_json: dict | None = None  # only when include_raw=true


class QueryCount(BaseModel):
    query: str | None = None
    count: int


class SearchHistoryStatsOut(BaseModel):
    total: int
    distinct_queries: int
    earliest: datetime | None = None
    latest: datetime | None = None
    top_queries: list[QueryCount] = Field(default_factory=list)


class SubscriptionOut(BaseModel):
    id: int
    channel_id: str | None = None
    channel_title: str | None = None
    url: str | None = None
    enabled: bool = False


class PlaylistsImportOut(BaseModel):
    playlists_imported: int
    items_imported: int
    items_skipped: int
    videos_created: int
    scanned_playlists: int
    dry_run: bool
    job_id: int | None = None
    warnings: list[str] = Field(default_factory=list)


class TakeoutImportPlaylistsRequest(BaseModel):
    path: str
    limit_playlists: int | None = None
    limit_items: int | None = None
    dry_run: bool = False


class LikedVideosImportRequest(BaseModel):
    path: str
    limit: int | None = None
    dry_run: bool = False


class LikedVideosImportOut(BaseModel):
    imported_count: int
    skipped_duplicate_count: int
    failed_count: int
    scanned: int
    videos_created: int = 0
    source_kind: str | None = None  # takeout_my_activity | takeout_youtube | …
    detected_path: str | None = None  # the member parsed (no traversal vector)
    dry_run: bool
    job_id: int | None = None
    warnings: list[str] = Field(default_factory=list)


class TakeoutImportAllRequest(BaseModel):
    path: str
    limit_watch: int | None = None
    limit_search: int | None = None
    limit_subscriptions: int | None = None
    limit_playlists: int | None = None
    limit_items: int | None = None
    limit_liked: int | None = None
    dry_run: bool = False


class TakeoutImportAllOut(BaseModel):
    watch_history: TakeoutImportOut
    search_history: TakeoutImportOut
    subscriptions: TakeoutImportOut
    playlists: PlaylistsImportOut
    liked_videos: LikedVideosImportOut
    dry_run: bool


class LikedVideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str | None = None
    youtube_video_id: str | None = None
    title: str | None = None
    channel_title: str | None = None
    url: str | None = None
    liked_at: datetime | None = None
    video_id: int | None = None
    created_at: datetime
    # metadata-fetched = a linked Video has a title (filled later by metadata_only)
    metadata_fetched: bool = False
    # Phase 7C body/metadata state (distinguishes metadata files from a saved body).
    has_metadata: bool = False
    has_body: bool = False
    body_media_count: int = 0
    metadata_file_count: int = 0
    latest_archive_job_id: int | None = None
    latest_archive_job_status: str | None = None
    latest_archive_classification: str | None = None
    raw_json: dict | None = None  # only when include_raw=true


# ---- Phase 7C: liked-videos bulk archive ----
class LikedArchiveRequest(BaseModel):
    """Shared filters + action params for plan / enqueue."""

    source: str | None = None  # takeout_my_activity | youtube_data_api | all
    channel: str | None = None
    title: str | None = None
    liked_after: datetime | None = None
    liked_before: datetime | None = None
    missing_metadata: bool = False
    missing_body: bool = False
    profile: str | None = None
    limit: int | None = None
    dry_run: bool = False


class LikedArchivePlanOut(BaseModel):
    total_candidates: int
    missing_metadata: int
    missing_body: int
    has_body: int
    existing_active_jobs: int
    existing_retryable: int
    recommended_limit: int
    recommended_delay_seconds: float
    recommended_profile: str
    profile: str
    notes: list[str] = Field(default_factory=list)


class LikedArchiveEnqueueOut(BaseModel):
    selected_count: int
    jobs_created: int
    skipped_existing_job: int
    skipped_already_has_metadata: int
    skipped_already_has_body: int
    job_ids: list[int] = Field(default_factory=list)
    profile: str
    downloads_body: bool
    dry_run: bool


class LikedRetryFailedRequest(BaseModel):
    reason: str | None = None
    limit: int = 20


class LikedRetryFailedOut(BaseModel):
    retried: int
    job_ids: list[int] = Field(default_factory=list)


class LikedVideoStatsOut(BaseModel):
    total: int
    with_video_id: int
    linked_videos: int
    metadata_fetched: int
    earliest: datetime | None = None
    latest: datetime | None = None


class LikedVideosEnqueueRequest(BaseModel):
    profile: str | None = None  # default metadata_only
    limit: int | None = None
    only_missing_metadata: bool = True


class LikedVideosEnqueueOut(BaseModel):
    videos_selected: int
    jobs_created: int
    job_ids: list[int] = Field(default_factory=list)


class SubscriptionEnqueueRequest(BaseModel):
    videos: bool = False
    shorts: bool = False
    streams: bool = False
    profile: str | None = None
    max_items: int | None = None
    limit: int | None = None  # max channels to enqueue (safety)


class SubscriptionEnqueueOut(BaseModel):
    channels: int
    jobs_created: int
    job_ids: list[int] = Field(default_factory=list)


class TakeoutPlaylistsPreviewOut(BaseModel):
    path: str
    playlists: list[PlaylistSampleOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Phase 4A: comments refresh
# --------------------------------------------------------------------------- #
class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_id: int
    comment_id: str
    parent_comment_id: str | None = None
    author_name: str | None = None
    author_channel_id: str | None = None
    text: str | None = None
    like_count: int | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    fetched_at: datetime | None = None
    is_deleted_or_missing: bool = False
    source: str | None = None
    raw_json: dict | None = None  # only when include_raw=true


class AuthorCount(BaseModel):
    author_name: str | None = None
    count: int


class CommentStatsOut(BaseModel):
    video_id: int
    youtube_video_id: str | None = None
    total: int
    active: int
    missing: int
    distinct_authors: int
    last_comments_refresh_at: datetime | None = None
    next_comments_refresh_at: datetime | None = None
    comments_state: str | None = None
    top_authors: list[AuthorCount] = Field(default_factory=list)


class CommentsRefreshRequest(BaseModel):
    # Official field: a YouTube video id, a YouTube URL, or any value that
    # resolves to a video (see services.jobs.resolve_or_create_video).
    target: str | None = None
    # Backward-compatible alias for `target` (deprecated; do not send both).
    video: str | None = None
    profile: str | None = None
    # Accepted for client compatibility; the API always enqueues to the worker.
    now: bool = False

    def resolved_target(self) -> str | None:
        """The effective target, or None if neither field was supplied."""
        return self.target if self.target is not None else self.video

    def has_conflict(self) -> bool:
        """True if both `target` and `video` were supplied (ambiguous)."""
        return self.target is not None and self.video is not None


class CommentsRefreshAllRequest(BaseModel):
    # ``due_only`` (default) selects videos whose next_comments_refresh_at is due;
    # set ``all=true`` to refresh every non-frozen video. A safety limit applies.
    limit_videos: int | None = None
    profile: str | None = None
    due_only: bool = True
    all: bool = False

    def effective_due_only(self) -> bool:
        return False if self.all else self.due_only


class CommentsRefreshAllOut(BaseModel):
    videos_selected: int
    jobs_created: int
    due_only: bool = True
    job_ids: list[int] = Field(default_factory=list)


class CommentsDueVideoOut(BaseModel):
    video_id: int
    youtube_video_id: str
    title: str | None = None
    comments_state: str | None = None
    last_comments_refresh_at: datetime | None = None
    next_comments_refresh_at: datetime | None = None
    due_reason: str = "due"


class CommentsDueOut(BaseModel):
    now: datetime
    count: int
    videos: list[CommentsDueVideoOut] = Field(default_factory=list)


class MetadataSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_id: int | None = None
    source: str | None = None
    snapshot_type: str
    path: str
    checksum: str | None = None
    fetched_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Phase 4B: live chat
# --------------------------------------------------------------------------- #
class LiveChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_id: int
    message_id: str | None = None
    author_name: str | None = None
    author_channel_id: str | None = None
    message: str | None = None
    timestamp_ms: int | None = None
    time_text: str | None = None
    message_type: str | None = None
    amount: float | None = None
    amount_text: str | None = None
    currency: str | None = None
    is_superchat: bool = False
    is_member_message: bool = False
    published_at: datetime | None = None
    fetched_at: datetime | None = None
    is_deleted_or_missing: bool = False
    # raw_json intentionally omitted by default (privacy); only when include_raw=true.
    raw_json: dict | None = None


class LiveChatStatsOut(BaseModel):
    video_id: int
    youtube_video_id: str | None = None
    total: int
    active: int
    missing: int
    superchats: int
    member_messages: int
    distinct_authors: int
    has_live_chat: bool = False
    live_chat_state: str | None = None
    last_live_chat_refresh_at: datetime | None = None
    next_live_chat_refresh_at: datetime | None = None


class LiveChatRefreshRequest(BaseModel):
    # Official field: a video id / URL (see services.jobs.resolve_or_create_video).
    target: str | None = None
    video: str | None = None  # backward-compatible alias
    profile: str | None = None
    now: bool = False

    def resolved_target(self) -> str | None:
        return self.target if self.target is not None else self.video

    def has_conflict(self) -> bool:
        return self.target is not None and self.video is not None


class LiveChatRefreshAllRequest(BaseModel):
    limit_videos: int | None = None
    profile: str | None = None


class LiveChatRefreshAllOut(BaseModel):
    videos_selected: int
    jobs_created: int
    job_ids: list[int] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Phase 5A: admin UI support (dashboard / stats / takeout files / settings)
# --------------------------------------------------------------------------- #
class JobStatsOut(BaseModel):
    total: int
    by_status: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)


class DashboardCounts(BaseModel):
    videos: int = 0
    collections: int = 0
    crawlable_collections: int = 0
    watch_history: int = 0
    search_history: int = 0
    subscriptions: int = 0
    comments: int = 0
    comments_due: int = 0
    comments_frozen: int = 0
    live_chat_messages: int = 0
    live_chat_due: int = 0
    metadata_snapshots: int = 0


class DashboardOut(BaseModel):
    health: HealthOut
    job_stats: JobStatsOut
    counts: DashboardCounts
    scheduler: SchedulerStatusOut
    latest_jobs: list[JobOut] = Field(default_factory=list)


class TakeoutFileEntryOut(BaseModel):
    name: str  # relative to TAKEOUT_IMPORT_ROOT
    size: int
    modified_at: datetime | None = None
    is_zip: bool = True
    archive_kind: str | None = None  # youtube_takeout | my_activity_takeout | takeout_index | …


class TakeoutFilesOut(BaseModel):
    root: str  # display path of TAKEOUT_IMPORT_ROOT (not a secret)
    files: list[TakeoutFileEntryOut] = Field(default_factory=list)


class TakeoutDiscoverEntry(BaseModel):
    name: str
    size: int = 0
    archive_kind: str = "unknown_takeout"
    has_youtube_takeout: bool = False
    my_activity_youtube_path: str | None = None
    has_index: bool = False
    member_count: int = 0
    liked_source_kind: str | None = None
    liked_detected_path: str | None = None
    liked_count: int | None = None  # only when deep=true
    error: bool = False


class TakeoutDiscoverOut(BaseModel):
    root: str
    archives: list[TakeoutDiscoverEntry] = Field(default_factory=list)


class TakeoutInspectOut(BaseModel):
    path: str
    archive_kind: str
    has_youtube_takeout: bool = False
    my_activity_youtube_path: str | None = None
    has_index: bool = False
    member_count: int = 0
    liked_source_kind: str | None = None
    liked_detected_path: str | None = None


class SettingsItem(BaseModel):
    key: str
    value: str
    note: str | None = None


class SettingsOut(BaseModel):
    """Non-secret, display-only settings. Secrets (cookies/db/redis creds) are
    never included; connection URLs are credential-masked."""

    items: list[SettingsItem] = Field(default_factory=list)
    profiles: list[ProfileOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Phase 5B: search + library
# --------------------------------------------------------------------------- #
class SearchResultOut(BaseModel):
    """One search hit. ``type`` is video | comment | live_chat | collection.

    Privacy: raw_json is never included; snippets are short text excerpts.
    """

    type: str
    title: str | None = None  # video/collection title (or context)
    snippet: str | None = None  # matched text excerpt (comment/live_chat/desc)
    video_id: int | None = None
    youtube_video_id: str | None = None
    collection_id: int | None = None
    author_name: str | None = None  # for comment/live_chat hits
    extra: str | None = None  # e.g. like_count / message_type / channel


class SearchOut(BaseModel):
    query: str
    total: int
    results: list[SearchResultOut] = Field(default_factory=list)


class LibraryCategoryOut(BaseModel):
    """A library section (Phase 5B forward-compat for liked/history/etc.)."""

    key: str  # liked_videos | watch_history | search_history | subscriptions | playlists
    label: str
    count: int = 0
    available: bool = True  # False -> planned (e.g. liked videos sync)
    note: str | None = None


class LibrarySummaryOut(BaseModel):
    categories: list[LibraryCategoryOut] = Field(default_factory=list)
    liked_sources: dict[str, int] = Field(default_factory=dict)  # source -> count (6B)


class LibraryBootstrapRequest(BaseModel):
    youtube_takeout: str | None = None  # path under TAKEOUT_IMPORT_ROOT
    myactivity_takeout: str | None = None
    limit_watch: int | None = None
    limit_search: int | None = None
    limit_subscriptions: int | None = None
    limit_playlists: int | None = None
    limit_items: int | None = None
    limit_liked: int | None = None
    use_api: bool = False
    dry_run: bool = False


class LibraryBootstrapOut(BaseModel):
    dry_run: bool
    youtube_takeout: dict | None = None
    myactivity_takeout: dict | None = None
    api: dict | None = None


# --------------------------------------------------------------------------- #
# Phase 6B: YouTube Data API (OAuth) differential liked sync
# --------------------------------------------------------------------------- #
class YouTubeApiStatusOut(BaseModel):
    """Non-secret OAuth status (never exposes file paths or tokens)."""

    enabled: bool
    client_secret_present: bool
    token_present: bool
    configured: bool
    method: str


# --------------------------------------------------------------------------- #
# Phase 7B: YouTube fetch-stabilization doctor / diagnostics
# --------------------------------------------------------------------------- #
class YouTubeDoctorCheck(BaseModel):
    name: str
    status: str  # ok | warning | failed
    detail: str  # NEVER contains secret values / cookie paths / tokens


class YouTubeCookieStatus(BaseModel):
    configured: bool = False
    file_configured: bool = False
    file_exists: bool = False
    readable: bool = False
    last_modified: str | None = None  # NO path is ever included


class YouTubeDoctorOut(BaseModel):
    ok: bool
    ytdlp_version: str | None = None
    deno_available: bool = False
    remote_components: str | None = None
    curl_cffi_installed: bool = False
    curl_cffi_version: str | None = None
    impersonate_targets: int = 0
    impersonation_available: bool = False
    cookies: YouTubeCookieStatus = Field(default_factory=YouTubeCookieStatus)
    browser_cookies_configured: bool = False
    po_token_configured: bool = False
    visitor_data_configured: bool = False
    checks: list[YouTubeDoctorCheck] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class YouTubeDiagnosticRequest(BaseModel):
    url: str
    profile: str | None = None
    include_video_download: bool = False
    timeout: int | None = None


class YouTubeApiSyncRequest(BaseModel):
    method: str | None = None  # videos | playlist | auto
    stop_on_existing: bool = True
    limit: int | None = None
    dry_run: bool = False


class YouTubeApiSyncOut(BaseModel):
    imported_count: int = 0
    skipped_duplicate_count: int = 0
    failed_count: int = 0
    scanned: int = 0
    videos_created: int = 0
    stopped_on_existing: bool = False
    source: str = "youtube_data_api"
    dry_run: bool = False
    # When OAuth is not configured the endpoint returns 200 with ok=false + a
    # classification (auth_required / quota_exceeded / …) instead of crashing.
    ok: bool = True
    classification: str | None = None
    message: str | None = None
