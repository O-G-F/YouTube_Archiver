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
  permanent?: boolean; // Phase 7H: private/deleted/unavailable
  primary_reason?: string | null;
  reasons: string[];
  warnings: string[];
  summary: string | null;
}

// Phase 7H/7J: failed/partial liked-archive jobs grouped by reason
export interface LikedFailureBreakdown {
  total_failed: number;
  total_partial: number;
  retryable: number;
  permanent: number;
  permanent_unique_videos: number;
  by_reason: Record<string, number>;
  attempts_by_reason: Record<string, number>;
  unique_videos_by_reason: Record<string, number>;
}

// Phase 7I: cookie / PO-token configuration status (booleans/masked only)
export interface SecretsStatus {
  cookies_configured: boolean;
  cookies_file_configured: boolean;
  cookies_file_readable: boolean;
  cookies_from_browser_configured: boolean;
  po_token_configured: boolean;
  visitor_data_configured: boolean;
  cookies_last_modified: string | null;
  secret_value_exposed: boolean;
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
  session_id?: string | null;
}

export interface TakeoutRegistrySource {
  kind: string;
  member: string;
  format: string;
  import_kinds: string[];
}

export interface TakeoutInspect {
  path: string;
  archive_kind: string;
  has_youtube_takeout: boolean;
  my_activity_youtube_path: string | null;
  has_index: boolean;
  member_count: number;
  liked_source_kind: string | null;
  liked_detected_path: string | null;
  registry: TakeoutRegistrySource[];
}

export interface TakeoutImportSession {
  id: number;
  session_id: string;
  path_basename: string | null;
  source_kind: string | null;
  import_kind: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  dry_run: boolean;
  scanned: number;
  imported: number;
  skipped_duplicate: number;
  updated: number;
  failed: number;
  job_id?: number | null;
  parser_backend?: string | null;
  entries_per_second?: number | null;
  peak_memory_mb?: number | null;
  cancel_requested?: boolean;
  current_phase?: string | null;
  last_update_at?: string | null;
}

export interface TakeoutBenchmark {
  kind: string;
  scanned: number;
  imported: number;
  skipped_duplicate: number;
  updated: number;
  failed: number;
  duration_seconds: number;
  entries_per_second: number | null;
  peak_memory_mb: number | null;
  parser_backend: string;
  dry_run: boolean;
  source_kind: string | null;
  estimated_full_import_time_seconds?: number | null;
  recommended_batch_size?: number | null;
}

export interface DbStats {
  dialect: string;
  total_size_bytes: number | null;
  total_size_mb: number | null;
  table_counts: Record<string, number | null>;
  table_sizes_bytes: Record<string, number>;
  raw_json_stored: Record<string, number>;
  raw_json_stored_total: number;
  videos: number;
  liked_videos: number;
  watch_history_events: number;
  search_history_events: number;
  takeout_import_sessions: number;
}

export interface TakeoutBenchmarkLarge {
  results: Record<string, TakeoutBenchmark>;
  parser_backend: string;
  recommended_batch_size: number;
  dry_run: boolean;
}

export interface TakeoutSessionCleanup {
  total: number;
  matched: number;
  deleted: number;
  kept: number;
  jobs_preserved: number;
  dry_run: boolean;
  keep_last: number;
  older_than_days: number;
}

export interface TakeoutImportProgress {
  session_id: string;
  status: string;
  current_phase: string | null;
  scanned: number;
  imported: number;
  skipped_duplicate: number;
  updated: number;
  failed: number;
  entries_per_second: number | null;
  cancel_requested: boolean;
  job_id: number | null;
  last_update_at: string | null;
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
  // Phase 7C body/metadata state
  has_metadata: boolean;
  has_body: boolean;
  body_media_count: number;
  metadata_file_count: number;
  latest_archive_job_id: number | null;
  latest_archive_job_status: string | null;
  latest_archive_classification: string | null;
  raw_json: Record<string, unknown> | null;
}

export interface LikedArchivePlan {
  total_candidates: number;
  missing_metadata: number;
  missing_body: number;
  has_body: number;
  permanent_excluded?: number;
  eligible_missing_body?: number;
  existing_active_jobs: number;
  existing_retryable: number;
  recommended_limit: number;
  recommended_delay_seconds: number;
  recommended_profile: string;
  profile: string;
  notes: string[];
  // Phase 9A: batch planning + disk capacity guard
  requested_limit?: number;
  cap_per_run?: number;
  selected_count?: number;
  disk_safe_limit?: number | null;
  limiting_factor?: string;
  blocked?: boolean;
  block_reason?: string | null;
  disk_readable?: boolean;
  disk_total_gb?: number | null;
  disk_used_gb?: number | null;
  disk_free_gb?: number | null;
  min_free_gb?: number;
  estimated_size_per_video_mb?: number;
  size_estimate_source?: string;
  size_estimate_sample_count?: number;
  estimated_required_gb?: number;
  estimated_free_after_gb?: number | null;
}

export interface LikedArchiveEnqueueResult {
  selected_count: number;
  jobs_created: number;
  skipped_existing_job: number;
  skipped_already_has_metadata: number;
  skipped_already_has_body: number;
  skipped_permanent?: number;
  job_ids: number[];
  profile: string;
  downloads_body: boolean;
  dry_run: boolean;
  // Phase 9A: disk capacity guard outcome
  blocked?: boolean;
  block_reason?: string | null;
  capacity?: Record<string, unknown>;
}

// Phase 9A: consolidated body-archive operations status
export interface LikedOperations {
  default_body_profile: string;
  body_saved: number;
  remaining_eligible_body: number;
  permanent_unique_videos: number;
  active_archive_jobs: number;
  queued_jobs: number;
  running_jobs: number;
  total_active_jobs: number;
  worker_count: number;
  disk: {
    readable: boolean;
    total_gb: number | null;
    used_gb: number | null;
    free_gb: number | null;
    used_percent: number | null;
  };
  min_free_gb: number;
  size_estimate: {
    source: string;
    sample_count: number;
    estimate_mb: number;
    avg_mb: number | null;
    median_mb: number | null;
    p90_mb: number | null;
  };
  orphan: { scanned: number; orphan_found: number; rq_unreadable: boolean };
  duplicate_video_media_files: number;
  comments_table_bytes: number;
  raw_json_stored_total: number;
}

// Phase 9E: audit trail
export interface AuditEvent {
  id: number;
  occurred_at: string | null;
  event_type: string;
  category: string;
  severity: string;
  outcome: string;
  actor_kind: string;
  actor_id_hash: string | null;
  client_id_hash: string | null;
  request_id: string | null;
  correlation_id: string | null;
  resource_type: string | null;
  resource_id: string | null;
  action: string | null;
  reason_code: string | null;
  metadata: Record<string, unknown> | null;
  event_hash: string;
}
export interface AuditStats {
  total: number;
  window_days: number;
  by_category: Record<string, number>;
  by_severity: Record<string, number>;
  by_outcome: Record<string, number>;
}
export interface AuditVerify {
  valid: boolean;
  valid_with_warnings: boolean;
  checked_count: number;
  segment_count: number;
  checkpoint_count: number;
  current_signing_key_id: string | null;
  unsigned_event_count: number;
  missing_verification_keys: string[];
  first_invalid_event_id: number | null;
  failure_reason_code: string | null;
  signed: boolean;
}

// Phase 9C: auth
export interface AuthSession {
  authenticated: boolean;
  auth_mode: "disabled" | "local" | "trusted_proxy";
  app_env: string;
  identity: string | null;
  login_required: boolean;
}

// Phase 9B: production deployment readiness
export interface ProductionCheckItem {
  name: string;
  status: "pass" | "warn" | "fail";
  detail: string;
}
export interface ProductionCheck {
  overall: "pass" | "warn" | "fail";
  counts: { pass?: number; warn?: number; fail?: number };
  checks: ProductionCheckItem[];
  default_body_profile: string;
  app_env?: string;
  auth_mode?: string;
  disk_min_free_gb: number;
  backup_reminder: string;
}
// Phase 9F: read-only backup / disaster-recovery readiness
export interface BackupManifestSummary {
  artifact?: string | null;
  size_bytes?: number | null;
  sha256?: string | null;
  schema_head?: string | null;
  created_at?: string | null;
  manifest_version?: number | null;
  // v2 backup-set fields (Phase 9F.1)
  backup_id?: string | null;
  completed?: boolean | null;
  app_version?: string | null;
  build_id?: string | null;
  active_jobs_at_backup?: number | null;
  audit_head_event_id?: number | null;
  redis_recovery_mode?: string | null;
  encrypted?: boolean | null;
  archive_manifest_artifact?: string | null;
  archive_manifest_sha256?: string | null;
  integrity_scheme?: string | null;
}
export interface BackupReadiness {
  overall: "pass" | "warn" | "fail";
  counts: { pass?: number; warn?: number; fail?: number };
  checks: ProductionCheckItem[];
  manifest?: BackupManifestSummary | null;
  backup_age_hours?: number | null;
  backup_verified_age_hours?: number | null;
  restore_rehearsal_age_days?: number | null;
}

// Phase 10A: release / provenance readiness (read-only)
export interface VersionInfo {
  app_version: string;
  git_commit?: string | null;
  git_tree_clean?: boolean | null;
  build_id: string;
  build_timestamp?: string | null;
  schema_head?: string | null;
  frontend_build_id?: string | null;
  image_digest?: string | null;
}
export interface ReleaseManifestSummary {
  manifest_version?: number | null;
  release_id?: string | null;
  app_version?: string | null;
  git_commit?: string | null;
  git_tree_clean?: boolean | null;
  build_id?: string | null;
  schema_head?: string | null;
  frontend_build_id?: string | null;
  completed?: boolean | null;
  service_build_ids?: string[];
  service_count?: number | null;
  image_digests_captured?: number | null;
  sbom_present?: boolean | null;
  sbom_sha256?: string | null;
  vulnerability_status?: string | null;
  vulnerability_severities?: Record<string, number> | null;
  vulnerability_tool?: string | null;
  vulnerability_tool_version?: string | null;
  vulnerability_db_updated_at?: string | null;
  release_check_overall?: string | null;
  integrity_scheme?: string | null;
  backend_test_count?: number | null;
  frontend_test_count?: number | null;
  // Phase 10A.1 reproducible-lock / supply-chain gates
  python_lock_exact?: boolean | null;
  python_lock_hashed?: boolean | null;
  python_lock_package_count?: number | null;
  base_python_digest_pinned?: boolean | null;
  base_node_digest_pinned?: boolean | null;
}
export interface SecurityPosture {
  operating_mode: string; // "local_single_user_dev" | "production"
  known_critical_accepted: number | null;
  exception_candidates: number | null;
  active_vulnerability_exceptions: number;
  reachability_assessed: boolean;
  production_ready: boolean;
  release_check_passes: boolean;
  risk_acceptance_doc: string;
  decision_dossier_doc: string;
  note: string;
}
export interface FirstRunItem {
  key: string;
  label: string;
  done: boolean;
  detail: string;
  link: string;
  optional: boolean;
  warn?: boolean;
  danger?: boolean;
}
export interface FirstRunStatus {
  is_fresh: boolean;
  video_count: number;
  liked_count: number;
  job_count: number;
  auth_mode: string;
  web_bind_host: string;
  web_bind_all_interfaces: boolean;
  exposure_warning: boolean;
  exposure_level: "none" | "warn" | "danger";
  exposure_note: string;
  items: FirstRunItem[];
  done_count: number;
  total_count: number;
}
export interface RuntimeReleaseStatus {
  verdict: "match" | "mismatch" | "no_scanned_release";
  message: string;
  status_source: string;
  manifest_matches_runtime: boolean;
  runtime_build_id: string | null;
  manifest_build_id: string | null;
  runtime_app_version: string | null;
  runtime_git_commit: string | null;
  runtime_schema_head: string | null;
  runtime_git_tree_clean: boolean | null;
  manifest_app_version: string | null;
  manifest_release_id: string | null;
  manifest_git_commit: string | null;
  manifest_age_seconds: number | null;
  scan_age_seconds: number | null;
}
export interface ReleaseReadiness {
  overall: "pass" | "warn" | "fail";
  counts: { pass?: number; warn?: number; fail?: number };
  checks: ProductionCheckItem[];
  version: VersionInfo;
  manifest?: ReleaseManifestSummary | null;
  security_posture?: SecurityPosture | null; // Phase 11A
  runtime_release?: RuntimeReleaseStatus | null; // Phase 11B
}
export interface ArchiveMediaCheck {
  db_video_media_files: number;
  checked: number;
  existing: number;
  missing: number;
  missing_youtube_ids: string[];
  duplicate_video_media_files: number;
  disk: { readable: boolean; free_gb: number | null; total_gb: number | null; used_gb: number | null };
  ok: boolean;
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
  retries_requeued?: number;
  liked_metadata_selected?: number;
  liked_metadata_jobs_created?: number;
  liked_archive_selected?: number;
  liked_archive_jobs_created?: number;
  liked_retry_selected?: number;
  liked_retry_jobs_requeued?: number;
  skipped_active_jobs?: number;
  skipped_duplicates?: number;
  skipped_frozen: number;
  skipped_recent: number;
  jobs_created: number;
  submitted: number;
  job_ids: number[];
}

export interface LikedProgress {
  total_liked: number;
  // metadata_fetched is BROAD (>=1 metadata media). Phase 7L adds the rigorous split.
  metadata_fetched: number;
  metadata_any_count?: number;
  info_json_complete_count?: number;
  description_only_count?: number;
  retryable_partial_count?: number;
  metadata_missing: number;
  eligible_metadata_missing: number;
  skipped_permanent_metadata: number;
  permanent_unique_videos: number;
  body_saved: number;
  body_missing: number;
  eligible_missing_body?: number;
  active_archive_jobs: number;
  retryable_liked_jobs: number;
  failed_liked_jobs: number;
  partial_liked_jobs: number;
  by_source: Record<string, number>;
  by_channel: { channel: string; count: number }[];
  earliest_liked_at: string | null;
  latest_liked_at: string | null;
  last_archive_job_at: string | null;
  last_successful_archive_at: string | null;
}

export interface QueueStatus {
  queued: number;
  running: number;
  total_active: number;
  by_type: Record<string, number>;
  by_source_action: Record<string, number>;
  oldest_queued_at: string | null;
  oldest_queued_job_id: number | null;
  worker_count: number | null;
}

export interface SchedulerRun {
  id: number;
  run_id: string;
  run_type: string;
  reason: string | null;
  started_at: string;
  finished_at: string | null;
  status: string;
  selected_count: number;
  jobs_created: number;
  jobs_submitted: number;
  skipped_active_jobs: number;
  skipped_duplicates: number;
  skipped_backoff: number;
  retryable_count: number;
  failed_count: number;
  partial_count: number;
  success_count: number;
  body_count_before: number;
  body_count_after: number;
  meta?: Record<string, unknown> | null;
}

export interface SchedulerStats {
  runs_considered: number;
  by_type: Record<string, number>;
  by_status: Record<string, number>;
  jobs_created: number;
  jobs_submitted: number;
  skipped_active_jobs: number;
  skipped_duplicates: number;
  skipped_backoff: number;
  last_run_id: string | null;
  last_run_type: string | null;
  last_run_status: string | null;
  last_run_at: string | null;
}

export interface LikedProgressHistoryPoint {
  run_id: string;
  run_type: string;
  at: string | null;
  total_liked: number;
  metadata_fetched: number;
  metadata_missing: number;
  body_saved: number;
  body_missing: number;
  retryable_liked_jobs: number;
  failed_liked_jobs: number;
  partial_liked_jobs: number;
  active_archive_jobs: number;
}

export interface RecommendSettings {
  based_on: Record<string, number>;
  rates: Record<string, number | null>;
  current: Record<string, number | boolean>;
  recommended: Record<string, number | boolean>;
  reasons: string[];
  note: string;
}

export interface RecommendExport {
  format: string;
  content: string;
  recommended: Record<string, number | boolean>;
  current: Record<string, number | boolean>;
  reasons: string[];
  note: string;
}

export interface SchedulerRunCleanup {
  total_runs: number;
  matched: number;
  deleted: number;
  kept: number;
  dry_run: boolean;
  keep_last: number;
  older_than_days: number;
  deleted_run_ids: string[];
  matched_run_ids: string[];
}

// ---- Phase 6F: build identity / preflight / verify / cleanup status ----
export interface BuildInfo {
  app_version: string;
  build_id: string;
  git_commit: string | null;
  build_time: string | null;
  schema_head: string | null;
  supported_job_types: string[];
}

export interface WorkerInfo {
  worker_id: string | null;
  build_id: string | null;
  app_version: string | null;
  age_seconds: number | null;
  stale: boolean | null;
  takeout_import: boolean;
}

export interface FullHealth {
  status: string;
  ok: boolean;
  database: boolean;
  redis: boolean;
  build_info: BuildInfo;
  workers: WorkerInfo[];
  worker_build_match: boolean;
  schema_head_match: boolean | null;
}

export interface PreflightCheck {
  name: string;
  status: string; // ok | warn | fail
  detail: string;
}

export interface PreflightLarge {
  ok: boolean;
  path_basename: string | null;
  parser_backend: string | null;
  checks: PreflightCheck[];
  results: Record<string, {
    sample_scanned: number;
    entries_per_second: number | null;
    peak_memory_mb: number | null;
    parser_backend: string | null;
    current_db_count: number;
    source_kind: string | null;
  }>;
  recommended_command: string | null;
}

export interface VerifyImport {
  ok: boolean;
  session_id: string | null;
  import_kind: string | null;
  status: string | null;
  scanned: number;
  imported: number;
  skipped_duplicate: number;
  updated: number;
  failed: number;
  parser_backend: string | null;
  entries_per_second: number | null;
  peak_memory_mb: number | null;
  store_raw_json: boolean | null;
  raw_json_stored_count: number | null;
  raw_json_skipped_count: number | null;
  job_id: number | null;
  job_status: string | null;
  worker_error: string | null;
  db_stats: Record<string, number | string | null>;
  raw_json_real_blobs: Record<string, number>;
  leak_check_ok: boolean;
  leak_findings: string[];
  checks: PreflightCheck[];
}

export interface CleanupStatus {
  enabled: boolean;
  interval_hours: number;
  keep_last: number;
  retention_days: number;
  last_run_at: string | null;
  last_result: Record<string, number | boolean> | null;
  next_due_at: string | null;
}

// ---- Phase 6G: operation report ----
export interface ImportReport extends VerifyImport {
  path_basename: string | null;
  started_at: string | null;
  finished_at: string | null;
  recommended_next_action: string | null;
}
