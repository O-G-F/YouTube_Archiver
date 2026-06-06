import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { YouTubeDoctor } from "../api/types";

// Mock the API module before importing the component.
const doctorMock = vi.fn();
vi.mock("../api/endpoints", () => ({
  api: {
    doctorYoutube: () => doctorMock(),
    youtubeDiagnosticsRun: vi.fn(),
  },
}));

import { YouTubeDiagnostics } from "../components/YouTubeDiagnostics";

function doctor(over: Partial<YouTubeDoctor> = {}): YouTubeDoctor {
  return {
    ok: true,
    ytdlp_version: "2025.12.08",
    deno_available: true,
    remote_components: "ejs:github",
    curl_cffi_installed: true,
    curl_cffi_version: "0.7.0",
    impersonate_targets: 0,
    impersonation_available: false,
    cookies: { configured: false, file_configured: false, file_exists: false, readable: false, last_modified: null },
    browser_cookies_configured: false,
    po_token_configured: false,
    visitor_data_configured: false,
    checks: [
      { name: "yt-dlp", status: "ok", detail: "version 2025.12.08" },
      { name: "cookies", status: "warning", detail: "not configured (no cookies.txt / browser cookies)" },
      { name: "PO token", status: "warning", detail: "not set (optional)" },
    ],
    recommendations: ["Configure COOKIES_FILE (e.g. /config/cookies.txt) or COOKIES_FROM_BROWSER to reduce 429."],
    ...over,
  };
}

describe("Phase 7B YouTube diagnostics UI", () => {
  beforeEach(() => doctorMock.mockReset());

  it("renders configured yes/no status and recommendations (no secrets)", async () => {
    doctorMock.mockResolvedValue(doctor());
    const { container } = render(
      <MemoryRouter>
        <YouTubeDiagnostics />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("2025.12.08")).toBeInTheDocument());
    // curl_cffi installed yes/no
    expect(screen.getByText("installed")).toBeInTheDocument();
    // cookies not configured -> "no"
    expect(screen.getAllByText("no").length).toBeGreaterThan(0);
    // recommendation surfaces
    expect(
      screen.getByText(/Configure COOKIES_FILE .* to reduce 429/)
    ).toBeInTheDocument();
    // never renders an actual secret/token/path value
    expect(container.textContent).not.toContain("SENTINEL");
    expect(container.textContent).not.toMatch(/po_token=[A-Za-z0-9]/);
  });

  it("shows cookies configured when present", async () => {
    doctorMock.mockResolvedValue(
      doctor({
        cookies: { configured: true, file_configured: true, file_exists: true, readable: true, last_modified: "2026-06-06T00:00:00" },
        po_token_configured: true,
      })
    );
    render(
      <MemoryRouter>
        <YouTubeDiagnostics />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByText("configured")).toBeInTheDocument());
    expect(screen.getByText("set")).toBeInTheDocument(); // PO token set
  });
});
