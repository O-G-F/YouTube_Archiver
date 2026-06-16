import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import type { DbStats, TakeoutBenchmarkLarge, TakeoutSessionCleanup } from "../api/types";

const dbStatsMock = vi.fn();
const benchLargeMock = vi.fn();
const cleanupMock = vi.fn();

vi.mock("../api/endpoints", () => ({
  api: {
    dbStats: () => dbStatsMock(),
    takeoutBenchmarkLarge: (b: unknown) => benchLargeMock(b),
    takeoutSessionsCleanup: (b: unknown) => cleanupMock(b),
  },
}));

import { TakeoutDbStats } from "../components/TakeoutDbStats";

const stats: DbStats = {
  dialect: "sqlite",
  total_size_bytes: 1048576,
  total_size_mb: 1.0,
  table_counts: {},
  table_sizes_bytes: {},
  raw_json_stored: { liked_videos: 8 },
  raw_json_stored_total: 8,
  videos: 10,
  liked_videos: 8,
  watch_history_events: 90,
  search_history_events: 5,
  takeout_import_sessions: 3,
};

describe("Phase 6E DB stats + cleanup", () => {
  beforeEach(() => {
    dbStatsMock.mockReset();
    benchLargeMock.mockReset();
    cleanupMock.mockReset();
    dbStatsMock.mockResolvedValue(stats);
  });

  it("shows DB size + entity counts + raw_json rows", async () => {
    render(<TakeoutDbStats path="ma.zip" />);
    await waitFor(() => expect(screen.getByText("DB size & large-import tools")).toBeInTheDocument());
    expect(screen.getByText("raw_json rows")).toBeInTheDocument();
    expect(screen.getByText("90")).toBeInTheDocument(); // watch count (unique)
    expect(screen.getByText("10")).toBeInTheDocument(); // videos (unique)
    expect(screen.getAllByText("8").length).toBeGreaterThanOrEqual(1); // liked + raw_json_total
  });

  it("benchmark-large renders per-kind eps/peak/estimate", async () => {
    const bl: TakeoutBenchmarkLarge = {
      results: {
        liked_videos: { kind: "liked_videos", scanned: 11000, imported: 11000, skipped_duplicate: 0, updated: 0, failed: 0, duration_seconds: 2.0, entries_per_second: 5500, peak_memory_mb: 4.2, parser_backend: "ijson", dry_run: true, source_kind: "takeout_my_activity", estimated_full_import_time_seconds: 3.2, recommended_batch_size: 5000 },
      },
      parser_backend: "ijson",
      recommended_batch_size: 5000,
      dry_run: true,
    };
    benchLargeMock.mockResolvedValue(bl);
    render(<TakeoutDbStats path="ma.zip" />);
    await waitFor(() => expect(screen.getByText("DB size & large-import tools")).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Benchmark-large/));
    await waitFor(() => expect(benchLargeMock).toHaveBeenCalledWith({ path: "ma.zip" }));
    expect(screen.getByText("5500")).toBeInTheDocument(); // eps
    expect(screen.getByText("4.2")).toBeInTheDocument(); // peak MB
  });

  it("cleanup dry-run states that jobs + data are NOT deleted", async () => {
    const cl: TakeoutSessionCleanup = { total: 10, matched: 7, deleted: 0, kept: 3, jobs_preserved: 5, dry_run: true, keep_last: 3, older_than_days: 0 };
    cleanupMock.mockResolvedValue(cl);
    render(<TakeoutDbStats path="ma.zip" />);
    await waitFor(() => expect(screen.getByText("Session cleanup")).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Dry-run/));
    await waitFor(() => expect(cleanupMock).toHaveBeenCalled());
    expect(screen.getByText(/Jobs & imported data are NOT deleted/)).toBeInTheDocument();
  });
});
