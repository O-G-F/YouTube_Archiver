import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReleaseInfoPanel } from "../components/ReleaseInfoPanel";

vi.mock("../api/endpoints", () => ({
  api: { releaseReadiness: vi.fn() },
}));
import { api } from "../api/endpoints";

beforeEach(() => {
  vi.clearAllMocks();
  (api.releaseReadiness as any).mockResolvedValue({
    overall: "warn",
    counts: { pass: 5, warn: 2 },
    checks: [
      { name: "git_tree_clean", status: "pass", detail: "clean source tree" },
      { name: "application_version", status: "pass", detail: "app_version=v1.0.0" },
      { name: "release_manifest", status: "pass", detail: "release rel-x (integrity sha256)" },
      { name: "sbom_present", status: "pass", detail: "SBOM sha256=abcdef012345…" },
      { name: "vulnerability_scan", status: "warn", detail: "scan warn severities={HIGH:2}" },
    ],
    version: {
      app_version: "v1.0.0",
      git_commit: "abcdef1234567890",
      git_tree_clean: true,
      build_id: "src:eae50accb008ee71",
      build_timestamp: "2026-07-19T05:00:00Z",
      schema_head: "e5f6a7b8c9d0",
      frontend_build_id: "ui:438c39b309f3b78a",
      image_digest: null,
    },
    manifest: {
      manifest_version: 1,
      release_id: "rel-20260719-abc",
      app_version: "v1.0.0",
      git_tree_clean: true,
      build_id: "src:eae50accb008ee71",
      schema_head: "e5f6a7b8c9d0",
      completed: true,
      service_build_ids: ["src:eae50accb008ee71"],
      service_count: 4,
      image_digests_captured: 0,
      sbom_present: true,
      sbom_sha256: "abcdef0123456789abcdef",
      vulnerability_status: "warn",
      vulnerability_severities: { HIGH: 2, MEDIUM: 5 },
      vulnerability_tool: "trivy",
      release_check_overall: "warn",
      integrity_scheme: "sha256",
      backend_test_count: 640,
      frontend_test_count: 68,
    },
  });
});

describe("Phase 10A release info panel", () => {
  it("shows version identity, manifest status and vuln summary, read-only", async () => {
    render(<ReleaseInfoPanel />);
    // Phase 11B: runtime and scanned release are shown as distinct sections
    expect(await screen.findByText(/Running runtime/i)).toBeInTheDocument();
    expect(screen.getByText(/Last scanned release/i)).toBeInTheDocument();
    // runtime + manifest both carry v1.0.0 in this mock -> at least one shown
    expect(screen.getAllByText("v1.0.0").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("rel-20260719-abc")).toBeInTheDocument();
    expect(screen.getByText("src:eae50accb008ee71")).toBeInTheDocument();
    expect(screen.getByText("e5f6a7b8c9d0")).toBeInTheDocument();
    // vulnerability severity summary
    expect(screen.getByText(/HIGH: 2/)).toBeInTheDocument();
    // check rows render
    expect(screen.getByText("sbom_present")).toBeInTheDocument();
    // read-only: only a reload button — no deploy / update / build controls
    expect(screen.queryByRole("button", { name: /deploy|update|build|upgrade|rebuild|delete/i }))
      .not.toBeInTheDocument();
  });

  it("never renders repository/host paths (real leak vector)", async () => {
    render(<ReleaseInfoPanel />);
    await screen.findByText(/Running runtime/i);
    // host / repo path markers must never appear (descriptive "no secrets shown"
    // copy is allowed; actual path/credential VALUES are the leak concern)
    expect(document.body.textContent).not.toMatch(/\/Users\/|\/home\/|\/var\/folders\/|@sha256:.*\/|registry-\d/);
  });
});
