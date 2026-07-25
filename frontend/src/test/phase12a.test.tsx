import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("../api/endpoints", () => ({ api: { firstRun: vi.fn() } }));
import { api } from "../api/endpoints";
import { FirstRunChecklist } from "../components/FirstRunChecklist";

function status(over: Record<string, unknown> = {}) {
  return {
    is_fresh: true, video_count: 0, liked_count: 0, job_count: 0,
    auth_mode: "disabled", web_bind_host: "127.0.0.1", web_bind_all_interfaces: false,
    exposure_warning: true, exposure_level: "warn",
    exposure_note: "Local single-user: bind to 127.0.0.1 and keep auth off only on a trusted host.",
    items: [{ key: "auth", label: "Authentication", done: false, warn: true, detail: "auth disabled", link: "/settings", optional: false }],
    done_count: 0, total_count: 1, ...over,
  };
}

describe("phase 12a — first-run exposure levels", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows a mild 'auth disabled' warning for loopback + auth off", async () => {
    (api.firstRun as any).mockResolvedValue(status());
    render(<MemoryRouter><FirstRunChecklist /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("auth disabled")).toBeInTheDocument());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("escalates to a DANGER alert for 0.0.0.0 + auth off", async () => {
    (api.firstRun as any).mockResolvedValue(status({
      web_bind_host: "0.0.0.0", web_bind_all_interfaces: true, exposure_level: "danger",
      exposure_note: "DANGER: auth is disabled AND the web port is bound to all interfaces (0.0.0.0).",
      items: [{ key: "auth", label: "Authentication", done: false, warn: true, danger: true, detail: "exposed", link: "/settings", optional: false }],
    }));
    render(<MemoryRouter><FirstRunChecklist /></MemoryRouter>);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/exposed: auth disabled \+ 0\.0\.0\.0/i);
  });
});
