import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import type { FullHealth, CleanupStatus, PreflightLarge } from "../api/types";

const healthMock = vi.fn();
const cleanupStatusMock = vi.fn();
const preflightLargeMock = vi.fn();

vi.mock("../api/endpoints", () => ({
  api: {
    systemHealthFull: () => healthMock(),
    takeoutCleanupStatus: () => cleanupStatusMock(),
    takeoutPreflightLarge: (b: unknown) => preflightLargeMock(b),
  },
}));

import { TakeoutOps } from "../components/TakeoutOps";

const baseHealth: FullHealth = {
  status: "ok",
  ok: true,
  database: true,
  redis: true,
  build_info: { app_version: "0.1.0", build_id: "src:abcdef123456", git_commit: null, build_time: null, schema_head: "c3d4e5f6a7b8", supported_job_types: ["takeout_import"] },
  workers: [{ worker_id: "h:1", build_id: "src:abcdef123456", app_version: "0.1.0", age_seconds: 3, stale: false, takeout_import: true }],
  worker_build_match: true,
  schema_head_match: true,
};

const cleanup: CleanupStatus = {
  enabled: false, interval_hours: 24, keep_last: 0, retention_days: 0,
  last_run_at: null, last_result: null, next_due_at: null,
};

describe("Phase 6F system ops", () => {
  beforeEach(() => {
    healthMock.mockReset();
    cleanupStatusMock.mockReset();
    preflightLargeMock.mockReset();
    cleanupStatusMock.mockResolvedValue(cleanup);
  });

  it("shows build_id + worker match when web/worker agree", async () => {
    healthMock.mockResolvedValue(baseHealth);
    render(<TakeoutOps path="ma.zip" />);
    await waitFor(() => expect(screen.getByText("System & large-import preflight")).toBeInTheDocument());
    expect(screen.getByText("src:abcdef123456")).toBeInTheDocument();
    expect(screen.getByText("match")).toBeInTheDocument();
  });

  it("flags a STALE WORKER when build_ids differ", async () => {
    healthMock.mockResolvedValue({
      ...baseHealth,
      worker_build_match: false,
      workers: [{ worker_id: "h:1", build_id: "src:OLDOLDOLD", app_version: "0.1.0", age_seconds: 3, stale: false, takeout_import: true }],
    });
    render(<TakeoutOps path="ma.zip" />);
    await waitFor(() => expect(screen.getByText("STALE WORKER")).toBeInTheDocument());
    expect(screen.getByText(/Rebuild ALL images/)).toBeInTheDocument();
  });

  it("preflight-large renders per-kind sample + recommended command", async () => {
    healthMock.mockResolvedValue(baseHealth);
    const pl: PreflightLarge = {
      ok: true, path_basename: "ma.zip", parser_backend: "ijson",
      checks: [{ name: "zip_exists", status: "ok", detail: "found ma.zip" }],
      results: { watch_history: { sample_scanned: 5000, entries_per_second: 4200, peak_memory_mb: 94.1, parser_backend: "ijson", current_db_count: 0, source_kind: "my_activity_takeout" } },
      recommended_command: "archiver takeout import-large ma.zip --kind all --limit 1000 --apply",
    };
    preflightLargeMock.mockResolvedValue(pl);
    render(<TakeoutOps path="ma.zip" />);
    await waitFor(() => expect(screen.getByText("System & large-import preflight")).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Preflight-large/));
    await waitFor(() => expect(preflightLargeMock).toHaveBeenCalledWith({ path: "ma.zip", kind: "all" }));
    expect(screen.getByText("4200")).toBeInTheDocument();
    expect(screen.getByText(/import-large ma.zip/)).toBeInTheDocument();
  });
});
