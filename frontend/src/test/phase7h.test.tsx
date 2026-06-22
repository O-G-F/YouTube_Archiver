import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { LikedProgress, QueueStatus, LikedFailureBreakdown } from "../api/types";

const progressMock = vi.fn();
const queueMock = vi.fn();
const failuresMock = vi.fn();

vi.mock("../api/endpoints", () => ({
  api: {
    likedProgress: () => progressMock(),
    queueStatus: () => queueMock(),
    likedFailureBreakdown: () => failuresMock(),
    schedulerRunOnce: vi.fn(),
    secretsStatus: () =>
      Promise.resolve({ cookies_configured: false, cookies_file_configured: false, cookies_file_readable: false, cookies_from_browser_configured: false, po_token_configured: false, visitor_data_configured: false, cookies_last_modified: null, secret_value_exposed: false }),
    likedRetryFailed: () => Promise.resolve({ retried: 0, job_ids: [] }),
  },
}));

import { LikedProgressDashboard } from "../components/LikedProgress";

const prog: LikedProgress = {
  total_liked: 11066, metadata_fetched: 5000, metadata_missing: 6066,
  eligible_metadata_missing: 6000, skipped_permanent_metadata: 66, permanent_unique_videos: 66,
  body_saved: 100, body_missing: 10966,
  active_archive_jobs: 0, retryable_liked_jobs: 4, failed_liked_jobs: 7, partial_liked_jobs: 1,
  by_source: { takeout_my_activity: 11066 }, by_channel: [], earliest_liked_at: null, latest_liked_at: null,
  last_archive_job_at: null, last_successful_archive_at: null,
};
const queue: QueueStatus = {
  queued: 0, running: 0, total_active: 0, by_type: {}, by_source_action: {},
  oldest_queued_at: null, oldest_queued_job_id: null, worker_count: 1,
} as QueueStatus;

describe("Phase 7H liked failure breakdown", () => {
  beforeEach(() => {
    progressMock.mockReset();
    queueMock.mockReset();
    failuresMock.mockReset();
    progressMock.mockResolvedValue(prog);
    queueMock.mockResolvedValue(queue);
  });

  it("renders failures grouped by reason with permanent/retryable counts", async () => {
    const fb: LikedFailureBreakdown = {
      total_failed: 7, total_partial: 1, retryable: 2, permanent: 5,
      permanent_unique_videos: 5,
      by_reason: { deleted: 3, private: 1, unavailable: 1, network: 1, unknown: 1 },
      attempts_by_reason: { deleted: 3, private: 1, unavailable: 1, network: 1, unknown: 1 },
      unique_videos_by_reason: { deleted: 3, private: 1, unavailable: 1, network: 1, unknown: 1 },
    };
    failuresMock.mockResolvedValue(fb);
    render(<MemoryRouter><LikedProgressDashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Liked archive progress")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText(/failures by reason/)).toBeInTheDocument());
    expect(screen.getByText(/deleted: 3\/3/)).toBeInTheDocument();
    expect(screen.getByText(/private: 1\/1/)).toBeInTheDocument();
    expect(screen.getByText(/network: 1\/1/)).toBeInTheDocument();
    expect(screen.getByText(/permanent unique 5/)).toBeInTheDocument();
    expect(screen.getByText(/削除しません/)).toBeInTheDocument();
  });

  it("hides the failure row when there are no failures", async () => {
    failuresMock.mockResolvedValue({ total_failed: 0, total_partial: 0, retryable: 0, permanent: 0, permanent_unique_videos: 0, by_reason: {}, attempts_by_reason: {}, unique_videos_by_reason: {} });
    render(<MemoryRouter><LikedProgressDashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Liked archive progress")).toBeInTheDocument());
    expect(screen.queryByText(/failures by reason/)).toBeNull();
  });
});
