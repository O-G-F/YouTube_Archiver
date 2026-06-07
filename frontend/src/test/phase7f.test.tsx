import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { SchedulerRun, RecommendExport, LikedProgressHistoryPoint } from "../api/types";
import { Sparkline } from "../components/Sparkline";

const runsMock = vi.fn();
const historyMock = vi.fn();
const exportMock = vi.fn();
const cleanupMock = vi.fn();

vi.mock("../api/endpoints", () => ({
  api: {
    schedulerRuns: () => runsMock(),
    likedProgressHistory: () => historyMock(),
    schedulerRecommendExport: (f: string) => exportMock(f),
    schedulerRunsCleanup: (b: unknown) => cleanupMock(b),
    schedulerRun: vi.fn(),
  },
}));

import { SchedulerHistory } from "../components/SchedulerHistory";

function run(over: Partial<SchedulerRun> = {}): SchedulerRun {
  return {
    id: 1, run_id: "abc123def456", run_type: "liked_archive", reason: "manual",
    started_at: "2026-06-07T00:00:00", finished_at: "2026-06-07T00:01:00", status: "partial_success",
    selected_count: 2, jobs_created: 2, jobs_submitted: 2, skipped_active_jobs: 0,
    skipped_duplicates: 0, skipped_backoff: 1, retryable_count: 1, failed_count: 0,
    partial_count: 1, success_count: 0, body_count_before: 5, body_count_after: 5, ...over,
  };
}
function pt(i: number): LikedProgressHistoryPoint {
  return {
    run_id: `r${i}`, run_type: "liked_archive", at: `2026-06-0${i + 1}T00:00:00`,
    total_liked: 10, metadata_fetched: i + 1, metadata_missing: 9 - i, body_saved: i,
    body_missing: 10 - i, retryable_liked_jobs: 0, failed_liked_jobs: 0,
    partial_liked_jobs: 0, active_archive_jobs: 0,
  };
}

describe("Phase 7F Sparkline", () => {
  it("renders an SVG line chart with >=2 points", () => {
    render(<Sparkline series={[{ label: "body_saved", color: "#3fb950", values: [1, 2, 3] }]} />);
    expect(screen.getByLabelText("progress history chart")).toBeInTheDocument();
    expect(screen.getByText(/body_saved/)).toBeInTheDocument();
  });
  it("shows a hint with <2 points", () => {
    render(<Sparkline series={[{ label: "x", color: "#000", values: [1] }]} />);
    expect(screen.getByText(/Not enough data points/)).toBeInTheDocument();
  });
});

describe("Phase 7F scheduler history graph + export", () => {
  beforeEach(() => {
    runsMock.mockReset(); historyMock.mockReset(); exportMock.mockReset(); cleanupMock.mockReset();
    runsMock.mockResolvedValue([run()]);
    historyMock.mockResolvedValue({ points: [pt(0), pt(1), pt(2)] });
  });

  it("renders the progress graph from history", async () => {
    render(<MemoryRouter><SchedulerHistory /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Liked progress history")).toBeInTheDocument());
    expect(screen.getByLabelText("progress history chart")).toBeInTheDocument();
  });

  it("exports a copy-paste .env snippet (suggestion only, no auto-apply)", async () => {
    const ex: RecommendExport = {
      format: "env",
      content: "SCHEDULER_LIKED_ARCHIVE_LIMIT_PER_RUN=1   # was 2",
      recommended: { scheduler_liked_archive_limit_per_run: 1 },
      current: { scheduler_liked_archive_limit_per_run: 2 },
      reasons: ["throttle high"],
      note: "Recommendation only — settings are NOT changed automatically.",
    };
    exportMock.mockResolvedValue(ex);
    render(<MemoryRouter><SchedulerHistory /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/Recommended settings/)).toBeInTheDocument());
    fireEvent.click(screen.getByText("Copy .env…"));
    await waitFor(() => expect(exportMock).toHaveBeenCalledWith("env"));
    expect(screen.getByText(/SCHEDULER_LIKED_ARCHIVE_LIMIT_PER_RUN=1/)).toBeInTheDocument();
    expect(screen.getByText(/NOT changed automatically/)).toBeInTheDocument();
  });

  it("cleanup dry-run reports it does not delete jobs", async () => {
    cleanupMock.mockResolvedValue({ total_runs: 30, matched: 10, deleted: 0, kept: 20, dry_run: true, keep_last: 20, older_than_days: 0, deleted_run_ids: [], matched_run_ids: [] });
    render(<MemoryRouter><SchedulerHistory /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/Retention/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Cleanup \(dry-run\)/));
    await waitFor(() => expect(cleanupMock).toHaveBeenCalled());
    expect(screen.getByText(/Jobs are NOT deleted/)).toBeInTheDocument();
  });
});
