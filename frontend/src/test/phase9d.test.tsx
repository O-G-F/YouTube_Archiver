import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { LikedOperationsPanel } from "../components/LikedOperations";
import type { LikedOperations, ProductionCheck } from "../api/types";

vi.mock("../api/endpoints", () => ({
  api: { likedOperations: vi.fn(), productionCheck: vi.fn(), releaseCheck: vi.fn() },
}));
import { api } from "../api/endpoints";

const OPS: LikedOperations = {
  default_body_profile: "video_compressed_1080p_light",
  body_saved: 1930,
  remaining_eligible_body: 8420,
  permanent_unique_videos: 716,
  active_archive_jobs: 0,
  queued_jobs: 0,
  running_jobs: 0,
  total_active_jobs: 0,
  worker_count: 1,
  disk: { readable: true, total_gb: 1564, used_gb: 700, free_gb: 800, used_percent: 45 },
  min_free_gb: 500,
  size_estimate: { source: "measured", sample_count: 1930, estimate_mb: 523, avg_mb: 250, median_mb: 240, p90_mb: 523 },
  orphan: { scanned: 0, orphan_found: 0, rq_unreadable: false },
  duplicate_video_media_files: 0,
  comments_table_bytes: 23617536,
  raw_json_stored_total: 0,
};

const PROD: ProductionCheck = {
  overall: "warn",
  counts: { pass: 26, warn: 1, fail: 0 },
  checks: [{ name: "cors_policy", status: "warn", detail: "CORS_ALLOW_ORIGINS=*" }],
  default_body_profile: "video_compressed_1080p_light",
  app_env: "development",
  auth_mode: "local",
  disk_min_free_gb: 500,
  backup_reminder: "Back up Postgres + Redis AOF + archive + secrets.",
};

beforeEach(() => {
  vi.clearAllMocks();
  (api.likedOperations as any).mockResolvedValue(OPS);
  (api.productionCheck as any).mockResolvedValue(PROD);
  (api.releaseCheck as any).mockResolvedValue({ ...PROD, overall: "pass", counts: { pass: 30, warn: 0, fail: 0 }, checks: [] });
});

describe("Phase 9D operations/readiness panel", () => {
  it("shows auth mode and production readiness", async () => {
    render(<LikedOperationsPanel />);
    expect(await screen.findByText(/production readiness:/i)).toBeInTheDocument();
    expect(screen.getByText(/auth: local/i)).toBeInTheDocument();
    expect(screen.getByText(/development/i)).toBeInTheDocument();
  });

  it("runs release-check on demand and shows the summary", async () => {
    render(<LikedOperationsPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /run release-check/i }));
    await waitFor(() => expect(api.releaseCheck).toHaveBeenCalled());
    expect(await screen.findByText(/release: PASS/i)).toBeInTheDocument();
  });
});
