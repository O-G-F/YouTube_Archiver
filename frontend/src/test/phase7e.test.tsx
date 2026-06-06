import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { SchedulerRun, RecommendSettings, LikedProgressHistoryPoint } from "../api/types";

const runsMock = vi.fn();
const historyMock = vi.fn();
const recommendMock = vi.fn();

vi.mock("../api/endpoints", () => ({
  api: {
    schedulerRuns: () => runsMock(),
    likedProgressHistory: () => historyMock(),
    schedulerRecommend: () => recommendMock(),
  },
}));

import { SchedulerHistory } from "../components/SchedulerHistory";

function run(over: Partial<SchedulerRun> = {}): SchedulerRun {
  return {
    id: 1,
    run_id: "abc123def456",
    run_type: "liked_archive",
    reason: "manual",
    started_at: "2026-06-07T00:00:00",
    finished_at: "2026-06-07T00:01:00",
    status: "partial_success",
    selected_count: 2,
    jobs_created: 2,
    jobs_submitted: 2,
    skipped_active_jobs: 0,
    skipped_duplicates: 0,
    skipped_backoff: 1,
    retryable_count: 1,
    failed_count: 0,
    partial_count: 1,
    success_count: 0,
    body_count_before: 5,
    body_count_after: 5,
    ...over,
  };
}

const point: LikedProgressHistoryPoint = {
  run_id: "abc123def456",
  run_type: "liked_archive",
  at: "2026-06-07T00:01:00",
  total_liked: 10,
  metadata_fetched: 6,
  metadata_missing: 4,
  body_saved: 2,
  body_missing: 8,
  retryable_liked_jobs: 1,
  failed_liked_jobs: 0,
  partial_liked_jobs: 1,
  active_archive_jobs: 0,
};

describe("Phase 7E scheduler history + recommend", () => {
  beforeEach(() => {
    runsMock.mockReset();
    historyMock.mockReset();
    recommendMock.mockReset();
    runsMock.mockResolvedValue([run()]);
    historyMock.mockResolvedValue({ points: [point] });
  });

  it("renders run history + progress history tables", async () => {
    render(
      <MemoryRouter>
        <SchedulerHistory />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("Scheduler run history")).toBeInTheDocument());
    expect(screen.getByText("Liked progress history")).toBeInTheDocument();
    // run row shows the run type + a partial_success badge + skipped backoff
    expect(screen.getAllByText("liked_archive").length).toBeGreaterThan(0);
    expect(screen.getByText("partial_success")).toBeInTheDocument();
  });

  it("computes recommended settings on demand (suggestion only)", async () => {
    const rec: RecommendSettings = {
      based_on: { finished_archive_jobs: 3, retryable: 1, active_body_jobs: 0 },
      rates: { success_rate: 0.33, throttle_rate: 0.66 },
      current: { scheduler_liked_archive_limit_per_run: 2 },
      recommended: { scheduler_liked_archive_limit_per_run: 1 },
      reasons: ["Throttle rate 66% is high → archive limit 1, longer delay."],
      note: "Recommendation only — settings are NOT changed automatically.",
    };
    recommendMock.mockResolvedValue(rec);
    render(
      <MemoryRouter>
        <SchedulerHistory />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText(/Recommended settings/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Compute/));
    await waitFor(() => expect(recommendMock).toHaveBeenCalled());
    expect(screen.getByText(/NOT changed automatically/)).toBeInTheDocument();
    expect(screen.getByText(/Throttle rate 66%/)).toBeInTheDocument();
  });
});
