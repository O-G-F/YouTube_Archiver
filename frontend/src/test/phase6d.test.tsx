import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { TakeoutImportSession } from "../api/types";

const sessionsMock = vi.fn();
const cancelMock = vi.fn();
vi.mock("../api/endpoints", () => ({
  api: {
    takeoutImportSessions: () => sessionsMock(),
    takeoutImportCancel: (id: string) => cancelMock(id),
  },
}));

import { TakeoutSessions } from "../components/TakeoutSessions";

function sess(over: Partial<TakeoutImportSession> = {}): TakeoutImportSession {
  return {
    id: 1, session_id: "abcd1234", path_basename: "myactivity.zip",
    source_kind: "takeout_my_activity", import_kind: "liked_videos",
    started_at: "2026-06-07T00:00:00", finished_at: "2026-06-07T00:00:05",
    status: "success", dry_run: false, scanned: 30, imported: 28,
    skipped_duplicate: 2, updated: 0, failed: 0, job_id: 9,
    parser_backend: "ijson", entries_per_second: 1200, peak_memory_mb: 1.4,
    cancel_requested: false, current_phase: "done", last_update_at: null, ...over,
  };
}

describe("Phase 6D Takeout sessions: job link + progress + cancel", () => {
  beforeEach(() => {
    sessionsMock.mockReset();
    cancelMock.mockReset();
  });

  it("shows source_kind, parser backend, eps and a job link", async () => {
    sessionsMock.mockResolvedValue([sess()]);
    render(<MemoryRouter><TakeoutSessions /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("takeout_my_activity")).toBeInTheDocument());
    expect(screen.getByText("ijson")).toBeInTheDocument();
    expect(screen.getByText("1200")).toBeInTheDocument();
    const link = screen.getByText("#9");
    expect(link.closest("a")?.getAttribute("href")).toBe("/jobs/9");
  });

  it("a running session shows a cancel button that calls the API", async () => {
    sessionsMock.mockResolvedValue([sess({ status: "running", current_phase: "liked_videos", finished_at: null })]);
    cancelMock.mockResolvedValue({ status: "running", cancel_requested: true });
    render(<MemoryRouter><TakeoutSessions /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/cancel/)).toBeInTheDocument());
    // a "running" badge appears in the header
    expect(screen.getAllByText("running").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText(/cancel/));
    await waitFor(() => expect(cancelMock).toHaveBeenCalledWith("abcd1234"));
  });
});
