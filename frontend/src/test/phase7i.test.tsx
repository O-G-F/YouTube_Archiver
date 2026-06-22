import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { LikedProgress, QueueStatus, SecretsStatus } from "../api/types";

const progressMock = vi.fn();
const secretsMock = vi.fn();
const retryMock = vi.fn();

vi.mock("../api/endpoints", () => ({
  api: {
    likedProgress: () => progressMock(),
    queueStatus: () => Promise.resolve({ queued: 0, running: 0, total_active: 0, by_type: {}, by_source_action: {}, oldest_queued_at: null, oldest_queued_job_id: null, worker_count: 1 }),
    likedFailureBreakdown: () => Promise.resolve({ total_failed: 0, total_partial: 0, retryable: 0, permanent: 0, permanent_unique_videos: 0, by_reason: {}, attempts_by_reason: {}, unique_videos_by_reason: {} }),
    secretsStatus: () => secretsMock(),
    likedRetryFailed: (b: unknown) => retryMock(b),
    schedulerRunOnce: vi.fn(),
  },
}));

import { LikedProgressDashboard } from "../components/LikedProgress";

const prog: LikedProgress = {
  total_liked: 11066, metadata_fetched: 49, metadata_missing: 11017,
  eligible_metadata_missing: 11017, skipped_permanent_metadata: 0, permanent_unique_videos: 0,
  body_saved: 0, body_missing: 11066,
  active_archive_jobs: 0, retryable_liked_jobs: 44, failed_liked_jobs: 1, partial_liked_jobs: 44,
  by_source: { takeout_my_activity: 11066 }, by_channel: [], earliest_liked_at: null, latest_liked_at: null,
  last_archive_job_at: null, last_successful_archive_at: null,
};

const secretsOff: SecretsStatus = {
  cookies_configured: false, cookies_file_configured: false, cookies_file_readable: false,
  cookies_from_browser_configured: false, po_token_configured: false, visitor_data_configured: false,
  cookies_last_modified: null, secret_value_exposed: false,
};

describe("Phase 7I cookie status + retry", () => {
  beforeEach(() => {
    progressMock.mockReset();
    secretsMock.mockReset();
    retryMock.mockReset();
    progressMock.mockResolvedValue(prog);
  });

  it("shows cookies/PO-token OFF warning when not configured (no secret values)", async () => {
    secretsMock.mockResolvedValue(secretsOff);
    const { container } = render(<MemoryRouter><LikedProgressDashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/cookies off/)).toBeInTheDocument());
    expect(screen.getByText(/PO-token off/)).toBeInTheDocument();
    expect(screen.getByText(/429 が増えます/)).toBeInTheDocument();
    // never renders a secret value / absolute path
    expect(container.innerHTML).not.toMatch(/COOKIES_FILE=|\/Users\/|po_token=/);
  });

  it("shows configured status when cookies + PO-token set", async () => {
    secretsMock.mockResolvedValue({ ...secretsOff, cookies_configured: true, cookies_file_configured: true, cookies_file_readable: true, po_token_configured: true });
    render(<MemoryRouter><LikedProgressDashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/cookies readable/)).toBeInTheDocument());
    expect(screen.getByText(/PO-token set/)).toBeInTheDocument();
  });

  it("retry rate_limited button calls retry-failed with reason=rate_limited", async () => {
    secretsMock.mockResolvedValue(secretsOff);
    retryMock.mockResolvedValue({ retried: 3, job_ids: [1, 2, 3] });
    render(<MemoryRouter><LikedProgressDashboard /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Liked archive progress")).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Retry rate_limited/));
    await waitFor(() => expect(retryMock).toHaveBeenCalledWith({ reason: "rate_limited" }));
    expect(await screen.findByText(/re-queued 3 rate_limited/)).toBeInTheDocument();
  });
});
