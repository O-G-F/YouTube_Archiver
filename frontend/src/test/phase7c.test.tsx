import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { LikedVideo } from "../api/types";

const likedVideosMock = vi.fn();
const likedStatsMock = vi.fn();
const planMock = vi.fn();
const enqueueArchiveMock = vi.fn();

vi.mock("../api/endpoints", () => ({
  api: {
    likedVideos: () => likedVideosMock(),
    likedVideosStats: () => likedStatsMock(),
    likedArchivePlan: (b: unknown) => planMock(b),
    enqueueLikedMetadataV2: vi.fn(),
    enqueueLikedArchive: (b: unknown) => enqueueArchiveMock(b),
    likedRetryFailed: vi.fn(),
  },
  thumbnailUrl: (id: number) => `/api/videos/${id}/thumbnail`,
  mediaUrl: (v: number, m: number) => `/api/videos/${v}/media/${m}`,
}));

import LikedVideos from "../pages/LikedVideos";

function liked(over: Partial<LikedVideo> = {}): LikedVideo {
  return {
    id: 1,
    source: "takeout_my_activity",
    youtube_video_id: "vidAAA11111",
    title: "Test video",
    channel_title: "Chan",
    url: "https://youtu.be/vidAAA11111",
    liked_at: "2025-01-01T00:00:00",
    video_id: 5,
    created_at: "2025-01-01T00:00:00",
    metadata_fetched: true,
    has_metadata: true,
    has_body: false,
    body_media_count: 0,
    metadata_file_count: 1,
    latest_archive_job_id: 9,
    latest_archive_job_status: "failed",
    latest_archive_classification: "HTTP 429",
    raw_json: null,
    ...over,
  };
}

describe("Phase 7C liked-videos archive UI", () => {
  beforeEach(() => {
    likedVideosMock.mockReset();
    likedStatsMock.mockReset();
    planMock.mockReset();
    enqueueArchiveMock.mockReset();
    likedStatsMock.mockResolvedValue({ total: 1, with_video_id: 1, linked_videos: 1, metadata_fetched: 1, earliest: null, latest: null });
  });

  it("shows metadata/body state + last-job badge", async () => {
    likedVideosMock.mockResolvedValue([liked()]);
    render(
      <MemoryRouter>
        <LikedVideos />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("Test video")).toBeInTheDocument());
    expect(screen.getByText("fetched")).toBeInTheDocument(); // has_metadata
    expect(screen.getByText("未保存")).toBeInTheDocument(); // has_body=false
    expect(screen.getByText("failed")).toBeInTheDocument(); // last job status
  });

  it("archive modal warns that the video BODY will be downloaded", async () => {
    likedVideosMock.mockResolvedValue([liked()]);
    render(
      <MemoryRouter>
        <LikedVideos />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("Test video")).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Enqueue archive/));
    // body-DL warning is shown in the modal
    await waitFor(() =>
      expect(screen.getByText(/動画本体（video body）をダウンロード/)).toBeInTheDocument()
    );
    // dry-run + download buttons exist
    expect(screen.getByText(/Dry-run/)).toBeInTheDocument();
    expect(screen.getByText(/Download bodies/)).toBeInTheDocument();
  });

  it("runs a plan (no jobs created)", async () => {
    likedVideosMock.mockResolvedValue([liked()]);
    planMock.mockResolvedValue({
      total_candidates: 5, missing_metadata: 2, missing_body: 4, has_body: 1,
      existing_active_jobs: 0, existing_retryable: 1, recommended_limit: 4,
      recommended_delay_seconds: 30, recommended_profile: "video_compressed_1080p",
      profile: "video_compressed_1080p", notes: ["Start small."],
    });
    render(
      <MemoryRouter>
        <LikedVideos />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("Test video")).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Plan archive/));
    await waitFor(() => expect(screen.getByText(/preview — no jobs created/)).toBeInTheDocument());
    expect(planMock).toHaveBeenCalled();
  });
});
