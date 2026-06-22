import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { LikedProgress } from "../api/types";

const progressMock = vi.fn();
const failuresMock = vi.fn();

vi.mock("../api/endpoints", () => ({
  api: {
    likedProgress: () => progressMock(),
    queueStatus: () => Promise.resolve({ queued: 0, running: 0, total_active: 0, by_type: {}, by_source_action: {}, oldest_queued_at: null, oldest_queued_job_id: null, worker_count: 1 }),
    likedFailureBreakdown: () => failuresMock(),
    secretsStatus: () => Promise.resolve({ cookies_configured: true, cookies_file_configured: true, cookies_file_readable: true, cookies_from_browser_configured: false, po_token_configured: false, visitor_data_configured: false, cookies_last_modified: null, secret_value_exposed: false }),
    likedRetryFailed: () => Promise.resolve({ retried: 0, job_ids: [] }),
    schedulerRunOnce: vi.fn(),
  },
}));

import { LikedProgressDashboard } from "../components/LikedProgress";

const prog: LikedProgress = {
  total_liked: 11066, metadata_fetched: 936, metadata_missing: 10130,
  eligible_metadata_missing: 9085, skipped_permanent_metadata: 1045, permanent_unique_videos: 1045,
  body_saved: 0, body_missing: 11066, active_archive_jobs: 0, retryable_liked_jobs: 30,
  failed_liked_jobs: 50, partial_liked_jobs: 200, by_source: { takeout_my_activity: 11066 },
  by_channel: [], earliest_liked_at: null, latest_liked_at: null,
  last_archive_job_at: null, last_successful_archive_at: null,
};

describe("Phase 7J permanent-skip progress UI", () => {
  beforeEach(() => {
    progressMock.mockReset();
    failuresMock.mockReset();
    progressMock.mockResolvedValue(prog);
  });

  it("shows eligible-missing and permanent-kept cards", async () => {
    failuresMock.mockResolvedValue({ total_failed: 0, total_partial: 0, retryable: 0, permanent: 0, permanent_unique_videos: 0, by_reason: {}, attempts_by_reason: {}, unique_videos_by_reason: {} });
    render(<MemoryRouter><LikedProgressDashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Eligible missing")).toBeInTheDocument());
    expect(screen.getByText(/9085/)).toBeInTheDocument();           // eligible
    expect(screen.getByText("Permanent (kept)")).toBeInTheDocument();
    expect(screen.getByText("1045")).toBeInTheDocument();           // permanent unique
  });

  it("failures row separates unique videos from attempts and says kept/excluded", async () => {
    failuresMock.mockResolvedValue({
      total_failed: 50, total_partial: 200, retryable: 30, permanent: 1100, permanent_unique_videos: 1045,
      by_reason: { private: 1100 }, attempts_by_reason: { private: 1100 },
      unique_videos_by_reason: { private: 1045 },
    });
    render(<MemoryRouter><LikedProgressDashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/failures by reason/)).toBeInTheDocument());
    // unique/attempts format: private appears as 1045/1100
    expect(screen.getByText(/private: 1045\/1100/)).toBeInTheDocument();
    expect(screen.getByText(/再試行せず保持・選定から除外/)).toBeInTheDocument();
  });
});
