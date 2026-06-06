import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { LikedProgress, QueueStatus } from "../api/types";

const progressMock = vi.fn();
const queueMock = vi.fn();
const runOnceMock = vi.fn();

vi.mock("../api/endpoints", () => ({
  api: {
    likedProgress: () => progressMock(),
    queueStatus: () => queueMock(),
    schedulerRunOnce: (b: unknown) => runOnceMock(b),
  },
}));

import { LikedProgressDashboard } from "../components/LikedProgress";

function prog(over: Partial<LikedProgress> = {}): LikedProgress {
  return {
    total_liked: 10,
    metadata_fetched: 6,
    metadata_missing: 4,
    body_saved: 2,
    body_missing: 8,
    active_archive_jobs: 1,
    retryable_liked_jobs: 3,
    failed_liked_jobs: 2,
    partial_liked_jobs: 0,
    by_source: { takeout_my_activity: 8, youtube_data_api: 2 },
    by_channel: [{ channel: "Chan A", count: 4 }],
    earliest_liked_at: null,
    latest_liked_at: null,
    last_archive_job_at: null,
    last_successful_archive_at: null,
    ...over,
  };
}

function queue(over: Partial<QueueStatus> = {}): QueueStatus {
  return {
    queued: 2,
    running: 1,
    total_active: 3,
    by_type: { download: 3 },
    by_source_action: { liked_archive: 3 },
    oldest_queued_at: null,
    oldest_queued_job_id: 5,
    worker_count: 1,
    ...over,
  };
}

describe("Phase 7D liked progress dashboard", () => {
  beforeEach(() => {
    progressMock.mockReset();
    queueMock.mockReset();
    runOnceMock.mockReset();
    queueMock.mockResolvedValue(queue());
  });

  it("renders progress %, counts and source breakdown", async () => {
    progressMock.mockResolvedValue(prog());
    render(
      <MemoryRouter>
        <LikedProgressDashboard />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("Liked archive progress")).toBeInTheDocument());
    // metadata 6/10 -> 60%, body 2/10 -> 20%
    expect(screen.getByText(/60%/)).toBeInTheDocument();
    expect(screen.getByText(/20%/)).toBeInTheDocument();
    // retryable badge + source breakdown
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/takeout_my_activity: 8/)).toBeInTheDocument();
  });

  it("archive pass requires a confirm before downloading bodies", async () => {
    progressMock.mockResolvedValue(prog());
    runOnceMock.mockResolvedValue({ liked_archive_jobs_created: 1, skipped_active_jobs: 0 });
    render(
      <MemoryRouter>
        <LikedProgressDashboard />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("Liked archive progress")).toBeInTheDocument());
    // first click reveals the confirm + body-DL warning, does NOT call the API
    fireEvent.click(screen.getByText(/Run archive pass/));
    expect(screen.getByText(/本体DLが発生します/)).toBeInTheDocument();
    expect(runOnceMock).not.toHaveBeenCalled();
    // confirm triggers the run
    fireEvent.click(screen.getByText(/Confirm archive pass/));
    await waitFor(() => expect(runOnceMock).toHaveBeenCalledWith({ liked_archive: true }));
  });

  it("metadata pass runs immediately (no body)", async () => {
    progressMock.mockResolvedValue(prog());
    runOnceMock.mockResolvedValue({ liked_metadata_jobs_created: 3 });
    render(
      <MemoryRouter>
        <LikedProgressDashboard />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("Liked archive progress")).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Run metadata pass/));
    await waitFor(() => expect(runOnceMock).toHaveBeenCalledWith({ liked_metadata: true }));
  });
});
