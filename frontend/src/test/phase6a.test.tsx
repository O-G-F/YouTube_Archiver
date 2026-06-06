import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import LikedVideos from "../pages/LikedVideos";
import type { LikedVideo, LikedVideoStats } from "../api/types";

// Mock the endpoints module so the page renders deterministically.
vi.mock("../api/endpoints", () => {
  const liked: LikedVideo[] = [
    {
      id: 1,
      source: "takeout",
      youtube_video_id: "dQw4w9WgXcQ",
      title: null, // metadata not fetched
      channel_title: null,
      url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      liked_at: "2023-05-01T12:00:00",
      video_id: 10,
      created_at: "2026-01-01T00:00:00",
      metadata_fetched: false,
      has_metadata: false,
      has_body: false,
      body_media_count: 0,
      metadata_file_count: 0,
      latest_archive_job_id: null,
      latest_archive_job_status: null,
      latest_archive_classification: null,
      raw_json: null,
    },
    {
      id: 2,
      source: "takeout",
      youtube_video_id: "abc12345678",
      title: "Fetched One",
      channel_title: "Chan",
      url: "https://www.youtube.com/watch?v=abc12345678",
      liked_at: "2022-01-02T08:30:00",
      video_id: 11,
      created_at: "2026-01-01T00:00:00",
      metadata_fetched: true,
      has_metadata: true,
      has_body: true,
      body_media_count: 1,
      metadata_file_count: 2,
      latest_archive_job_id: 12,
      latest_archive_job_status: "success",
      latest_archive_classification: "Success",
      raw_json: null,
    },
  ];
  const stats: LikedVideoStats = {
    total: 2,
    with_video_id: 2,
    linked_videos: 2,
    metadata_fetched: 1,
    earliest: "2022-01-02T08:30:00",
    latest: "2023-05-01T12:00:00",
  };
  return {
    api: {
      likedVideos: vi.fn(async () => liked),
      likedVideosStats: vi.fn(async () => stats),
      archiveUrl: vi.fn(async () => ({ id: 99 })),
      enqueueLikedMetadata: vi.fn(async () => ({ videos_selected: 1, jobs_created: 1, job_ids: [99] })),
      enqueueLikedMetadataV2: vi.fn(),
      enqueueLikedArchive: vi.fn(),
      likedArchivePlan: vi.fn(),
      likedRetryFailed: vi.fn(),
    },
    thumbnailUrl: (id: number) => `/api/videos/${id}/thumbnail`,
    mediaUrl: (v: number, m: number) => `/api/videos/${v}/media/${m}`,
  };
});

describe("LikedVideos page", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders liked rows and flags metadata-not-fetched", async () => {
    render(
      <MemoryRouter>
        <LikedVideos />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("(metadata not fetched)")).toBeInTheDocument());
    expect(screen.getByText("Fetched One")).toBeInTheDocument();
    // the unfetched row shows the 未取得 (metadata) + 未保存 (body) badges
    expect(screen.getByText("未取得")).toBeInTheDocument();
    expect(screen.getByText("fetched")).toBeInTheDocument();
    // bulk archive actions are present in the toolbar
    expect(screen.getByText(/Enqueue metadata/)).toBeInTheDocument();
    expect(screen.getByText(/Enqueue archive/)).toBeInTheDocument();
  });
});
