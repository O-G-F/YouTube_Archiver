import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { AuditPanel } from "../components/AuditPanel";

vi.mock("../api/endpoints", () => ({
  api: { auditVerify: vi.fn(), auditStats: vi.fn(), auditEvents: vi.fn() },
}));
import { api } from "../api/endpoints";

beforeEach(() => {
  vi.clearAllMocks();
  (api.auditVerify as any).mockResolvedValue({ valid: true, valid_with_warnings: false, checked_count: 12, segment_count: 1, checkpoint_count: 0, current_signing_key_id: "a", unsigned_event_count: 0, missing_verification_keys: [], first_invalid_event_id: null, failure_reason_code: null, signed: true });
  (api.auditStats as any).mockResolvedValue({ total: 12, window_days: 30, by_category: { auth: 8 }, by_severity: { info: 10, warning: 2 }, by_outcome: { success: 9 } });
  (api.auditEvents as any).mockResolvedValue({
    limit: 25, offset: 0,
    events: [{
      id: 5, occurred_at: "2026-07-05T10:00:00", event_type: "login_success", category: "auth",
      severity: "info", outcome: "success", actor_kind: "admin", actor_id_hash: "abc", client_id_hash: "def",
      request_id: "r1", correlation_id: "r1", resource_type: null, resource_id: null, action: null,
      reason_code: null, metadata: null, event_hash: "h",
    }],
  });
});

describe("Phase 9E audit panel", () => {
  it("shows chain status, severity counts and events, with no edit/delete controls", async () => {
    render(<AuditPanel />);
    expect(await screen.findByText(/VALID · 12 ev · 1 seg · key a/i)).toBeInTheDocument();
    expect(screen.getByText(/warning: 2/i)).toBeInTheDocument();
    expect(await screen.findByText("login_success")).toBeInTheDocument();
    // read-only: no destructive controls
    expect(screen.queryByRole("button", { name: /delete|edit|remove/i })).not.toBeInTheDocument();
  });
});
