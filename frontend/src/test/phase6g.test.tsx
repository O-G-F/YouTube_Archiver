import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import type { FullHealth, ImportReport } from "../api/types";

const healthMock = vi.fn();
const reportMock = vi.fn();
const importJobMock = vi.fn();

vi.mock("../api/endpoints", () => ({
  api: {
    systemHealthFull: () => healthMock(),
    takeoutImportReportLatest: () => reportMock(),
    takeoutImportJob: (k: string, b: unknown) => importJobMock(k, b),
    takeoutPreflightLarge: vi.fn(),
    takeoutBenchmarkLarge: vi.fn(),
    dbStats: vi.fn(),
  },
}));

import { ProductionImportWizard } from "../components/ProductionImportWizard";

const health: FullHealth = {
  status: "ok", ok: true, database: true, redis: true,
  build_info: { app_version: "0.1.0", build_id: "src:x", git_commit: null, build_time: null, schema_head: "c3", supported_job_types: ["takeout_import"] },
  workers: [{ worker_id: "h:1", build_id: "src:x", app_version: "0.1.0", age_seconds: 2, stale: false, takeout_import: true }],
  worker_build_match: true, schema_head_match: true,
};

const report: ImportReport = {
  ok: true, session_id: "sess123", import_kind: "watch_history", status: "success",
  path_basename: "ma.zip", started_at: null, finished_at: null,
  scanned: 1000, imported: 1000, skipped_duplicate: 0, updated: 0, failed: 0,
  parser_backend: "ijson", entries_per_second: 64, peak_memory_mb: 94,
  store_raw_json: false, raw_json_stored_count: 0, raw_json_skipped_count: 1000,
  job_id: 1, job_status: "success", worker_error: null,
  db_stats: { raw_json_stored_total: 0, watch_history_events: 1000 },
  raw_json_real_blobs: { watch_history_events: 0 }, leak_check_ok: true, leak_findings: [],
  recommended_next_action: "success — proceed to the next stage", checks: [],
};

describe("Phase 6G production import wizard", () => {
  beforeEach(() => {
    healthMock.mockReset();
    reportMock.mockReset();
    importJobMock.mockReset();
  });

  it("renders 7 wizard steps with no-raw-json default ON", () => {
    render(<ProductionImportWizard path="ma.zip" />);
    expect(screen.getByText("Production import wizard")).toBeInTheDocument();
    expect(screen.getByText("1. System preflight")).toBeInTheDocument();
    expect(screen.getByText("7. Report")).toBeInTheDocument();
    const norawjson = screen.getByRole("checkbox") as HTMLInputElement;
    expect(norawjson.checked).toBe(true); // no-raw-json default ON
  });

  it("preflight step flags healthy build match", async () => {
    healthMock.mockResolvedValue(health);
    render(<ProductionImportWizard path="ma.zip" />);
    fireEvent.click(screen.getAllByText(/run/)[0]);
    await waitFor(() => expect(healthMock).toHaveBeenCalled());
    expect(screen.getByText(/build_match=true/)).toBeInTheDocument();
  });

  it("staged import submits a no-raw-json job and report shows leak-clean", async () => {
    healthMock.mockResolvedValue(health);
    importJobMock.mockResolvedValue({ id: 7 });
    reportMock.mockResolvedValue(report);
    render(<ProductionImportWizard path="ma.zip" />);
    // staged: click the +1000 stage button (watch_history default)
    fireEvent.click(screen.getByText("+1000"));
    // no-raw-json ON (default) -> store_raw_json:false sent to the API
    await waitFor(() => expect(importJobMock).toHaveBeenCalledWith("watch-history", { path: "ma.zip", limit: 1000, dry_run: false, store_raw_json: false }));
    expect(screen.getByText(/submitted job #7/)).toBeInTheDocument();
    // report step
    fireEvent.click(screen.getByText("7. Report").closest("tr")!.querySelector("button")!);
    await waitFor(() => expect(reportMock).toHaveBeenCalled());
    expect(screen.getByText(/leak clean/)).toBeInTheDocument();
    // appears in both the step-detail cell and the report flash
    expect(screen.getAllByText(/proceed to the next stage/).length).toBeGreaterThanOrEqual(1);
  });
});
