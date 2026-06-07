import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { TakeoutImportSession } from "../api/types";

const sessionsMock = vi.fn();
vi.mock("../api/endpoints", () => ({
  api: { takeoutImportSessions: () => sessionsMock() },
}));

import { TakeoutSessions } from "../components/TakeoutSessions";

function sess(over: Partial<TakeoutImportSession> = {}): TakeoutImportSession {
  return {
    id: 1,
    session_id: "abcd1234",
    path_basename: "myactivity.zip",
    source_kind: "takeout_my_activity",
    import_kind: "liked_videos",
    started_at: "2026-06-07T00:00:00",
    finished_at: "2026-06-07T00:00:05",
    status: "success",
    dry_run: false,
    scanned: 10,
    imported: 7,
    skipped_duplicate: 3,
    updated: 1,
    failed: 0,
    ...over,
  };
}

describe("Phase 6C Takeout import sessions", () => {
  beforeEach(() => sessionsMock.mockReset());

  it("renders import session counts (no path leak beyond basename)", async () => {
    sessionsMock.mockResolvedValue([sess()]);
    const { container } = render(<TakeoutSessions />);
    await waitFor(() => expect(screen.getByText("Import session history")).toBeInTheDocument());
    expect(screen.getByText("liked_videos")).toBeInTheDocument();
    expect(screen.getByText("myactivity.zip")).toBeInTheDocument();
    // imported / skipped / updated visible
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    // only the basename — never an absolute path
    expect(container.textContent).not.toContain("/tmp/");
    expect(container.textContent).not.toContain("/Users/");
  });

  it("flags dry-run sessions", async () => {
    sessionsMock.mockResolvedValue([sess({ dry_run: true })]);
    render(<TakeoutSessions />);
    await waitFor(() => expect(screen.getByText("dry-run")).toBeInTheDocument());
  });

  it("shows an empty state with no sessions", async () => {
    sessionsMock.mockResolvedValue([]);
    render(<TakeoutSessions />);
    await waitFor(() => expect(screen.getByText("No imports yet.")).toBeInTheDocument());
  });
});
