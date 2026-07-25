import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReleaseInfoPanel } from "../components/ReleaseInfoPanel";

vi.mock("../api/endpoints", () => ({ api: { releaseReadiness: vi.fn() } }));
import { api } from "../api/endpoints";

function base(runtime_release: any, manifest: any) {
  return {
    overall: "warn",
    counts: { pass: 60, warn: 6, fail: 0 },
    checks: [{ name: "critical_vulnerabilities", status: "warn", detail: "no completed scan" }],
    version: {
      app_version: "0.1.0", git_commit: "3d4fca8fde02", git_tree_clean: null,
      build_id: "src:RUNTIME", build_timestamp: null, schema_head: "e5f6a7b8c9d0",
      frontend_build_id: "ui:x", image_digest: null,
    },
    manifest,
    security_posture: {
      operating_mode: "local_single_user_dev", known_critical_accepted: 7,
      exception_candidates: 4, active_vulnerability_exceptions: 0, reachability_assessed: true,
      production_ready: false, release_check_passes: true,
      risk_acceptance_doc: "docs/decisions/x.md", decision_dossier_doc: "docs/vulnerability-decision-dossier.md",
      note: "accepted local risk; not production-ready.",
    },
    runtime_release,
  };
}

beforeEach(() => vi.clearAllMocks());

describe("Phase 11B runtime vs scanned-release separation", () => {
  it("shows MISMATCH when the running build differs from the scanned release", async () => {
    (api.releaseReadiness as any).mockResolvedValue(base(
      { verdict: "mismatch", message: "Running development build differs from the last scanned release.",
        status_source: "release_manifest", manifest_matches_runtime: false,
        runtime_build_id: "src:RUNTIME", manifest_build_id: "src:RELEASE",
        manifest_release_id: "rel-x", manifest_app_version: "v0.10.0-rc1",
        manifest_age_seconds: 3600 * 30, scan_age_seconds: 3600 * 40 },
      { release_id: "rel-x", app_version: "v0.10.0-rc1", build_id: "src:RELEASE",
        vulnerability_status: "unavailable", sbom_present: true, sbom_sha256: "abc", integrity_scheme: "sha256" },
    ));
    render(<ReleaseInfoPanel />);
    await screen.findByText(/Running runtime/i);
    expect(screen.getByText(/runtime ≠ scanned release/i)).toBeInTheDocument();
    // both build ids are visible and distinct
    expect(screen.getByText("src:RUNTIME")).toBeInTheDocument();
    expect(screen.getByText("src:RELEASE")).toBeInTheDocument();
    // security posture still surfaces the 7 CRITICAL, not hidden
    expect(screen.getByText(/7 known CRITICAL/i)).toBeInTheDocument();
  });

  it("shows MATCH when runtime equals the scanned release", async () => {
    (api.releaseReadiness as any).mockResolvedValue(base(
      { verdict: "match", message: "Current runtime matches the scanned release.",
        status_source: "release_manifest", manifest_matches_runtime: true,
        runtime_build_id: "src:SAME", manifest_build_id: "src:SAME",
        manifest_release_id: "rel-y", manifest_age_seconds: 60, scan_age_seconds: 120 },
      { release_id: "rel-y", app_version: "v1.0.0", build_id: "src:SAME",
        vulnerability_status: "pass", sbom_present: true, integrity_scheme: "hmac_sha256" },
    ));
    render(<ReleaseInfoPanel />);
    await screen.findByText(/runtime = scanned release/i);
  });

  it("shows NO scanned release when there is no manifest", async () => {
    (api.releaseReadiness as any).mockResolvedValue(base(
      { verdict: "no_scanned_release", message: "No scanned release information is available for this runtime.",
        status_source: "none", manifest_matches_runtime: false,
        runtime_build_id: "src:RUNTIME", manifest_build_id: null },
      null,
    ));
    render(<ReleaseInfoPanel />);
    expect((await screen.findAllByText(/no scanned release/i)).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/No scanned release recorded/i)).toBeInTheDocument();
  });
});
