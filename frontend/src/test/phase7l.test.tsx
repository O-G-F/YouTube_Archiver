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

// Phase 7L: broad metadata_fetched (4629) vs rigorous info_json complete (3050).
const prog: LikedProgress = {
  total_liked: 11066, metadata_fetched: 4629,
  metadata_any_count: 4629, info_json_complete_count: 3050,
  description_only_count: 1579, retryable_partial_count: 1490,
  metadata_missing: 6437,
  eligible_metadata_missing: 6230, skipped_permanent_metadata: 207, permanent_unique_videos: 207,
  body_saved: 0, body_missing: 11066, active_archive_jobs: 0, retryable_liked_jobs: 30,
  failed_liked_jobs: 50, partial_liked_jobs: 200, by_source: { takeout_my_activity: 11066 },
  by_channel: [], earliest_liked_at: null, latest_liked_at: null,
  last_archive_job_at: null, last_successful_archive_at: null,
};

describe("Phase 7L info_json completeness UI", () => {
  beforeEach(() => {
    progressMock.mockReset();
    failuresMock.mockReset();
    progressMock.mockResolvedValue(prog);
    failuresMock.mockResolvedValue({ total_failed: 0, total_partial: 0, retryable: 0, permanent: 0, permanent_unique_videos: 0, by_reason: {}, attempts_by_reason: {}, unique_videos_by_reason: {} });
  });

  it("shows broad metadata vs info_json-complete vs description-only cards", async () => {
    render(<MemoryRouter><LikedProgressDashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("info_json complete")).toBeInTheDocument());
    // broad count card still present
    expect(screen.getByText("Metadata (broad)")).toBeInTheDocument();
    // rigorous info_json complete number
    expect(screen.getByText(/3050/)).toBeInTheDocument();
    // description-only + retryable partial
    expect(screen.getByText("desc-only (retryable)")).toBeInTheDocument();
    expect(screen.getByText(/1579/)).toBeInTheDocument();
    expect(screen.getByText(/1490 retry/)).toBeInTheDocument();
  });
});
