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
    counts: { pass: 60, warn: 6 },
    checks: [
      { name: "critical_vulnerabilities", status: "warn", detail: "no completed scan — CRITICAL count unknown" },
      { name: "vulnerability_reachability_complete", status: "pass", detail: "all 7 remaining CRITICAL(s) have a reachability judgement" },
    ],
    version: {
      app_version: "0.1.0", git_commit: "2541a6eb3203c4bf", git_tree_clean: null,
      build_id: "src:2541a6eb3203c4bf", build_timestamp: null, schema_head: "e5f6a7b8c9d0",
      frontend_build_id: "ui:cd0460179c253820", image_digest: null,
    },
    manifest: {
      manifest_version: 1, release_id: "rel-x", app_version: "v0.10.0-rc1",
      build_id: "src:x", schema_head: "e5f6a7b8c9d0", completed: true,
      service_count: 4, sbom_present: true, sbom_sha256: "6f0faa7d2805aa",
      vulnerability_status: "unavailable", integrity_scheme: "sha256",
    },
    // Phase 11A: honest accepted-risk summary (surfaces the known 7 CRITICAL)
    security_posture: {
      operating_mode: "local_single_user_dev",
      known_critical_accepted: 7,
      exception_candidates: 4,
      active_vulnerability_exceptions: 0,
      reachability_assessed: true,
      production_ready: false,
      release_check_passes: true,
      risk_acceptance_doc: "docs/decisions/phase-11-local-single-user-risk-acceptance.md",
      decision_dossier_doc: "docs/vulnerability-decision-dossier.md",
      note: "Known CRITICAL OS CVEs (no upstream fix) are accepted as local single-user risk and are NOT hidden. This build is not production-ready; release-check does not pass in production.",
    },
  });
});

describe("Phase 11A security posture surfacing", () => {
  it("surfaces the known accepted CRITICAL count and not-production-ready, without hiding it", async () => {
    render(<ReleaseInfoPanel />);
    await screen.findByText(/Security posture:/i);
    // the 7 known CRITICAL are NOT hidden — shown as accepted local risk
    expect(screen.getByText(/7 known CRITICAL/i)).toBeInTheDocument();
    // local vs production distinction + not-production-ready (badge + note both mention it)
    expect(screen.getByText(/local single-user \(dev\)/i)).toBeInTheDocument();
    expect(screen.getAllByText(/not production-ready/i).length).toBeGreaterThanOrEqual(1);
    // dossier reference for the operator
    expect(screen.getByText(/vulnerability-decision-dossier\.md/i)).toBeInTheDocument();
    // 0 active exceptions surfaced
    expect(screen.getByText(/0 active exception/i)).toBeInTheDocument();
  });

  it("still renders read-only with no deploy/update controls and no host paths", async () => {
    render(<ReleaseInfoPanel />);
    await screen.findByText(/Security posture:/i);
    expect(screen.queryByRole("button", { name: /deploy|update|build|upgrade|rebuild|delete/i }))
      .not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\/Users\/|\/home\/|\/var\/folders\//);
  });
});
