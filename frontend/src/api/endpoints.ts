import { apiGet, apiPost, apiPatch, apiText } from "./client";
import type {
  Collection,
  CollectionItem,
  Comment,
  CommentStats,
  Dashboard,
  Doctor,
  Job,
  JobDetail,
  JobLogs,
  JobStats,
  LiveChatMessage,
  LiveChatStats,
  MetadataSnapshot,
  Profile,
  SchedulerRunOnceResult,
  SchedulerStatus,
  SettingsView,
  TakeoutFiles,
  TakeoutImportAll,
  TakeoutPreview,
  Video,
  VideoDetail,
  VideoListItem,
} from "./types";

const qs = (params: Record<string, string | number | boolean | undefined>) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "" && v !== null) u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
};

export const api = {
  dashboard: () => apiGet<Dashboard>("/api/dashboard"),
  jobStats: () => apiGet<JobStats>("/api/job-stats"),
  doctor: () => apiGet<Doctor>("/api/doctor"),
  settings: () => apiGet<SettingsView>("/api/settings"),
  profiles: () => apiGet<Profile[]>("/api/profiles"),

  schedulerStatus: () => apiGet<SchedulerStatus>("/api/scheduler/status"),
  schedulerRunOnce: (body: { collections?: boolean; comments?: boolean; max_items?: number }) =>
    apiPost<SchedulerRunOnceResult>("/api/scheduler/run-once", body),

  // Jobs
  jobs: (p: { status?: string; type?: string; limit?: number; offset?: number }) =>
    apiGet<Job[]>(`/api/jobs${qs(p)}`),
  job: (id: number) => apiGet<JobDetail>(`/api/jobs/${id}`),
  jobLogs: (id: number, tail?: number) => apiGet<JobLogs>(`/api/jobs/${id}/logs${qs({ tail })}`),
  jobLogStream: (id: number, stream: string, tail?: number) =>
    apiText(`/api/jobs/${id}/logs/${stream}${qs({ tail })}`),
  retryJob: (id: number) => apiPost<Job>(`/api/jobs/${id}/retry`),
  cancelJob: (id: number) => apiPost<Job>(`/api/jobs/${id}/cancel`),

  // Videos
  videos: (p: {
    q?: string;
    comments_state?: string;
    live_chat_state?: string;
    has_media?: boolean;
    limit?: number;
    offset?: number;
  }) => apiGet<VideoListItem[]>(`/api/videos${qs(p)}`),
  video: (id: number) => apiGet<VideoDetail>(`/api/videos/${id}`),
  videoJobs: (id: number) => apiGet<Job[]>(`/api/videos/${id}/jobs`),
  videoCollections: (id: number) => apiGet<Collection[]>(`/api/videos/${id}/collections`),
  videoComments: (id: number, p: { limit?: number; include_missing?: boolean } = {}) =>
    apiGet<Comment[]>(`/api/videos/${id}/comments${qs(p)}`),
  videoCommentStats: (id: number) => apiGet<CommentStats>(`/api/videos/${id}/comments/stats`),
  videoLiveChat: (id: number, p: { limit?: number; superchats_only?: boolean } = {}) =>
    apiGet<LiveChatMessage[]>(`/api/videos/${id}/live-chat${qs(p)}`),
  videoLiveChatStats: (id: number) => apiGet<LiveChatStats>(`/api/videos/${id}/live-chat/stats`),
  videoSnapshots: (id: number) => apiGet<MetadataSnapshot[]>(`/api/videos/${id}/snapshots`),
  refreshVideoComments: (id: number) => apiPost<Job>(`/api/videos/${id}/comments/refresh`),
  refreshVideoLiveChat: (id: number) => apiPost<Job>(`/api/videos/${id}/live-chat/refresh`),

  // Collections
  collections: () => apiGet<Collection[]>("/api/collections"),
  collection: (id: number) => apiGet<Collection>(`/api/collections/${id}`),
  collectionItems: (id: number, p: { limit?: number; include_removed?: boolean } = {}) =>
    apiGet<CollectionItem[]>(`/api/collections/${id}/items${qs(p)}`),
  enableCollection: (id: number) => apiPost<Collection>(`/api/collections/${id}/enable`),
  disableCollection: (id: number) => apiPost<Collection>(`/api/collections/${id}/disable`),
  patchCollection: (id: number, body: { crawl_policy?: string; enabled?: boolean; profile?: string }) =>
    apiPatch<Collection>(`/api/collections/${id}`, body),
  refreshCollection: (id: number, max_items?: number) =>
    apiPost<Job>(`/api/collections/${id}/refresh${qs({ max_items })}`),

  // Archive
  archiveUrl: (body: { url: string; profile?: string; priority?: number }) =>
    apiPost<Job>("/api/archive/url", body),
  archiveExpand: (body: { url: string; profile?: string; max_items?: number }) =>
    apiPost<Job>("/api/archive/expand", body),
  addChannel: (body: {
    url: string;
    profile?: string;
    videos?: boolean;
    shorts?: boolean;
    streams?: boolean;
    max_items?: number;
  }) => apiPost<Job[]>("/api/sources/channel", body),

  // Takeout
  takeoutFiles: () => apiGet<TakeoutFiles>("/api/takeout/files"),
  takeoutPreview: (path: string) => apiPost<TakeoutPreview>("/api/takeout/preview", { path }),
  takeoutImportAll: (body: Record<string, unknown>) =>
    apiPost<TakeoutImportAll>("/api/takeout/import-all", body),
};
