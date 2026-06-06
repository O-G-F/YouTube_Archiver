// TypeScript shapes mirroring the FastAPI response models (app/schemas.py).
// Only the fields the UI consumes are typed; unknown extra fields are ignored.

export interface Health {
  status: string;
  version: string;
  ytdlp_version: string | null;
  database: boolean;
  redis: boolean;
}

export interface JobClassification {
  rate_limited: boolean;
  partial: boolean;
  retryable: boolean;
  reasons: string[];
  warnings: string[];
  summary: string | null;
}

export interface Job {
  id: number;
  type: string;
  status: string;
  url: string | null;
  profile_name: string | null;
  video_id: number | null;
  collection_id: number | null;
  parent_job_id: number | null;
  rq_job_id: string | null;
  priority: number;
  progress: number;
  error_message: string | null;
  log_path: string | null;
  command_path: string | null;
  meta: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  retry_count?: number;
  retry_of_job_id?: number | null;
  next_retry_at?: string | null;
  classification?: JobClassification;
}

export interface Profile {
  name: string;
  media_mode: string;
  quality_mode: string | null;
  description: string | null;
  is_builtin: boolean;
}

export interface JobDetail extends Job {
  stdout_log_path: string | null;
  stderr_log_path: string | null;
  command_log_path: string | null;
  output_dir: string | null;
  video: Video | null;
  profile: Profile | null;
}

export interface JobLogs {
  job_id: number;
  log_path: string | null;
  available: boolean;
  command: string | null;
  stdout: string | null;
  stderr: string | null;
}

export interface Video {
  id: number;
  youtube_video_id: string;
  title: string | null;
  channel_id: string | null;
  channel_title: string | null;
  url: string | null;
  duration: number | null;
  upload_date: string | null;
  is_short: boolean;
  is_live: boolean;
  availability: string | null;
  thumbnail_path: string | null;
  first_seen_at: string;
  last_metadata_refresh_at: string | null;
  comments_state: string | null;
  comments_refresh_policy: string | null;
  last_comments_refresh_at: string | null;
  next_comments_refresh_at: string | null;
  live_chat_state: string | null;
  has_live_chat: boolean;
  last_live_chat_refresh_at: string | null;
  next_live_chat_refresh_at: string | null;
}

export interface VideoListItem extends Video {
  media_files_count: number; // video/audio BODY files only (0 -> 未保存)
  has_thumbnail: boolean;
}

export interface RelatedVideos {
  same_channel: VideoListItem[];
  same_collection: VideoListItem[];
}

export interface Channel {
  channel_id: string | null;
  channel_title: string | null;
  count: number;
}

export interface MediaFile {
  id: number;
  media_type: string;
  profile: string | null;
  path: string;
  container: string | null;
  width: number | null;
  height: number | null;
  filesize: number | null;
}

export interface VideoDetail extends Video {
  description: string | null;
  media_files: MediaFile[];
  subtitle_count: number;
  comment_count: number;
}

export interface Collection {
  id: number;
  type: string;
  title: string | null;
  url: string | null;
  youtube_playlist_id: string | null;
  youtube_channel_id: string | null;
  download_profile_id: number | null;
  crawl_policy: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  item_count: number;
}

export interface CollectionItem {
  id: number;
  collection_id: number;
  youtube_video_id: string | null;
  video_id: number | null;
  position: number | null;
  discovered_at: string;
  last_seen_at: string | null;
  removed_at: string | null;
}

export interface SchedulerStatus {
  enabled: boolean;
  interval_seconds: number;
  enabled_collections: number;
  crawlable_collections: number;
  comments_enabled: boolean;
  comments_limit_per_run: number;
  due_comment_videos: number;
  frozen_comment_videos: number;
}

export interface JobStats {
  total: number;
  by_status: Record<string, number>;
  by_type: Record<string, number>;
}

export interface DashboardCounts {
  videos: number;
  collections: number;
  crawlable_collections: number;
  watch_history: number;
  search_history: number;
  subscriptions: number;
  comments: number;
  comments_due: number;
  comments_frozen: number;
  live_chat_messages: number;
  live_chat_due: number;
  metadata_snapshots: number;
}

export interface Dashboard {
  health: Health;
  job_stats: JobStats;
  counts: DashboardCounts;
  scheduler: SchedulerStatus;
  latest_jobs: Job[];
}

export interface DoctorCheck {
  name: string;
  ok: boolean;
  detail: string;
}
export interface Doctor {
  ok: boolean;
  checks: DoctorCheck[];
}

export interface CommentStats {
  video_id: number;
  youtube_video_id: string | null;
  total: number;
  active: number;
  missing: number;
  distinct_authors: number;
  last_comments_refresh_at: string | null;
  next_comments_refresh_at: string | null;
  comments_state: string | null;
  top_authors: { author_name: string | null; count: number }[];
}

export interface Comment {
  id: number;
  video_id: number;
  comment_id: string;
  parent_comment_id: string | null;
  author_name: string | null;
  author_channel_id?: string | null;
  text: string | null;
  like_count: number | null;
  published_at: string | null;
  is_deleted_or_missing: boolean;
}

export interface LiveChatStats {
  video_id: number;
  youtube_video_id: string | null;
  total: number;
  active: number;
  missing: number;
  superchats: number;
  member_messages: number;
  distinct_authors: number;
  has_live_chat: boolean;
  live_chat_state: string | null;
  last_live_chat_refresh_at: string | null;
  next_live_chat_refresh_at: string | null;
}

export interface LiveChatMessage {
  id: number;
  video_id: number;
  message_id: string | null;
  author_name: string | null;
  message: string | null;
  time_text: string | null;
  message_type: string | null;
  amount: number | null;
  amount_text: string | null;
  currency: string | null;
  is_superchat: boolean;
  is_member_message: boolean;
  is_deleted_or_missing: boolean;
}

export interface MetadataSnapshot {
  id: number;
  video_id: number | null;
  source: string | null;
  snapshot_type: string;
  path: string;
  checksum: string | null;
  fetched_at: string | null;
}

export interface SettingsItem {
  key: string;
  value: string;
  note: string | null;
}
export interface SettingsView {
  items: SettingsItem[];
  profiles: Profile[];
}

export interface TakeoutFileEntry {
  name: string;
  size: number;
  modified_at: string | null;
  is_zip: boolean;
}
export interface TakeoutFiles {
  root: string;
  files: TakeoutFileEntry[];
}

export interface TakeoutPreview {
  path: string;
  archive_kind: string | null;
  liked_source_kind: string | null;
  liked_detected_path: string | null;
  watch_history_count: number;
  search_history_count: number;
  likes_count: number;
  subscriptions_count: number;
  playlists_count: number;
  liked_samples: { youtube_video_id: string | null; title: string | null; liked_at: string | null }[];
  warnings: string[];
}

export interface TakeoutImportResult {
  imported_count: number;
  skipped_duplicate_count: number;
  failed_count: number;
  scanned: number;
  dry_run: boolean;
  warnings: string[];
}
export interface PlaylistsImportResult {
  playlists_imported: number;
  items_imported: number;
  items_skipped: number;
  videos_created: number;
  scanned_playlists: number;
  dry_run: boolean;
  warnings: string[];
}
export interface LikedImportResult {
  imported_count: number;
  skipped_duplicate_count: number;
  failed_count: number;
  scanned: number;
  videos_created: number;
  dry_run: boolean;
}
export interface TakeoutImportAll {
  watch_history: TakeoutImportResult;
  search_history: TakeoutImportResult;
  subscriptions: TakeoutImportResult;
  playlists: PlaylistsImportResult;
  liked_videos: LikedImportResult;
  dry_run: boolean;
}

export interface LikedVideo {
  id: number;
  source: string | null;
  youtube_video_id: string | null;
  title: string | null;
  channel_title: string | null;
  url: string | null;
  liked_at: string | null;
  video_id: number | null;
  created_at: string;
  metadata_fetched: boolean;
  raw_json: Record<string, unknown> | null;
}

export interface LikedVideoStats {
  total: number;
  with_video_id: number;
  linked_videos: number;
  metadata_fetched: number;
  earliest: string | null;
  latest: string | null;
}

export interface LikedVideosEnqueueResult {
  videos_selected: number;
  jobs_created: number;
  job_ids: number[];
}

export interface SearchResult {
  type: "video" | "comment" | "live_chat" | "collection" | "liked_video";
  title: string | null;
  snippet: string | null;
  video_id: number | null;
  youtube_video_id: string | null;
  collection_id: number | null;
  author_name: string | null;
  extra: string | null;
}
export interface SearchResponse {
  query: string;
  total: number;
  results: SearchResult[];
}

export interface LibraryCategory {
  key: string;
  label: string;
  count: number;
  available: boolean;
  note: string | null;
}
export interface LibrarySummary {
  categories: LibraryCategory[];
  liked_sources: Record<string, number>;
}

export interface TakeoutDiscoverEntry {
  name: string;
  size: number;
  archive_kind: string;
  has_youtube_takeout: boolean;
  my_activity_youtube_path: string | null;
  has_index: boolean;
  member_count: number;
  liked_source_kind: string | null;
  liked_detected_path: string | null;
  liked_count: number | null;
  error: boolean;
}
export interface TakeoutDiscover {
  root: string;
  archives: TakeoutDiscoverEntry[];
}

export interface YouTubeApiStatus {
  enabled: boolean;
  client_secret_present: boolean;
  token_present: boolean;
  configured: boolean;
  method: string;
}

export interface YouTubeCookieStatus {
  configured: boolean;
  file_configured: boolean;
  file_exists: boolean;
  readable: boolean;
  last_modified: string | null;
}
export interface YouTubeDoctorCheck {
  name: string;
  status: string; // ok | warning | failed
  detail: string;
}
export interface YouTubeDoctor {
  ok: boolean;
  ytdlp_version: string | null;
  deno_available: boolean;
  remote_components: string | null;
  curl_cffi_installed: boolean;
  curl_cffi_version: string | null;
  impersonate_targets: number;
  impersonation_available: boolean;
  cookies: YouTubeCookieStatus;
  browser_cookies_configured: boolean;
  po_token_configured: boolean;
  visitor_data_configured: boolean;
  checks: YouTubeDoctorCheck[];
  recommendations: string[];
}

export interface YouTubeApiSyncResult {
  ok: boolean;
  classification: string | null;
  message: string | null;
  imported_count: number;
  skipped_duplicate_count: number;
  videos_created: number;
  scanned: number;
  stopped_on_existing: boolean;
  dry_run: boolean;
}

export interface SchedulerRunOnceResult {
  enabled: boolean;
  reason: string;
  collections_checked: number;
  collection_jobs_created: number;
  due_comment_videos_checked: number;
  comments_jobs_created: number;
  skipped_frozen: number;
  skipped_recent: number;
  jobs_created: number;
  submitted: number;
  job_ids: number[];
}
