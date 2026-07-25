import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { BackupReadinessPanel } from "../components/BackupReadinessPanel";

vi.mock("../api/endpoints", () => ({
  api: { backupReadiness: vi.fn() },
}));
import { api } from "../api/endpoints";

beforeEach(() => {
  vi.clearAllMocks();
  (api.backupReadiness as any).mockResolvedValue({
    overall: "warn",
    counts: { pass: 2, warn: 2 },
    checks: [
      { name: "backup_freshness", status: "pass", detail: "last backup ~3h ago" },
      { name: "backup_manifest", status: "pass", detail: "manifest for db-20260717.sql.gz (schema_head e5f6a7b8c9d0)" },
      { name: "backup_verified", status: "warn", detail: "backup never verified (run scripts/verify-backup.sh)" },
      { name: "restore_rehearsal", status: "warn", detail: "no restore rehearsal recorded" },
    ],
    manifest: {
      artifact: "db-20260717.sql.gz",
      size_bytes: 123456,
      sha256: "abcdef0123456789abcdef",
      schema_head: "e5f6a7b8c9d0",
      created_at: "2026-07-17T09:00:00",
      manifest_version: 1,
    },
    backup_age_hours: 3.2,
    backup_verified_age_hours: null,
    restore_rehearsal_age_days: 1.5,
  });
});

describe("Phase 9F backup readiness panel", () => {
  it("shows overall status, ages, manifest summary and checks, read-only", async () => {
    render(<BackupReadinessPanel />);
    expect(await screen.findByText(/overall: warn/i)).toBeInTheDocument();
    expect(screen.getByText(/~3.2時間前/)).toBeInTheDocument();
    expect(screen.getByText(/~1.5日前/)).toBeInTheDocument();
    expect(screen.getByText("db-20260717.sql.gz")).toBeInTheDocument();
    expect(screen.getByText("backup_verified")).toBeInTheDocument();
    expect(screen.getByText(/abcdef012345/)).toBeInTheDocument();
    // read-only: reload button only — no run/verify/delete mutations
    expect(screen.queryByRole("button", { name: /run|verify|delete|apply|restore/i })).not.toBeInTheDocument();
  });

  it("never renders host paths", async () => {
    render(<BackupReadinessPanel />);
    await screen.findByText(/overall: warn/i);
    expect(document.body.textContent).not.toMatch(/\/Users\/|\/home\/|\/var\/folders\//);
  });
});
