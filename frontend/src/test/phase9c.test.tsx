import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AuthGate } from "../components/AuthGate";
import type { AuthSession } from "../api/types";

vi.mock("../api/endpoints", () => ({
  api: { authSession: vi.fn(), authLogin: vi.fn(), authLogout: vi.fn() },
}));
import { api } from "../api/endpoints";

const S = (over: Partial<AuthSession>): AuthSession => ({
  authenticated: false,
  auth_mode: "local",
  app_env: "production",
  identity: null,
  login_required: true,
  ...over,
});

const APP = <div>ADMIN_CONTENT</div>;

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Phase 9C auth gate / login", () => {
  it("disabled mode renders the app unchanged (no login, no logout)", async () => {
    (api.authSession as any).mockResolvedValue(S({ auth_mode: "disabled", authenticated: true }));
    render(<AuthGate>{APP}</AuthGate>);
    expect(await screen.findByText("ADMIN_CONTENT")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /logout/i })).not.toBeInTheDocument();
  });

  it("local mode unauthenticated shows login and hides admin content", async () => {
    (api.authSession as any).mockResolvedValue(S({ authenticated: false }));
    render(<AuthGate>{APP}</AuthGate>);
    expect(await screen.findByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.queryByText("ADMIN_CONTENT")).not.toBeInTheDocument();
  });

  it("successful login reveals the app", async () => {
    (api.authSession as any)
      .mockResolvedValueOnce(S({ authenticated: false }))
      .mockResolvedValueOnce(S({ authenticated: true, identity: "admin" }));
    (api.authLogin as any).mockResolvedValue(S({ authenticated: true, identity: "admin" }));
    render(<AuthGate>{APP}</AuthGate>);
    fireEvent.change(await screen.findByLabelText(/password/i), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: /login/i }));
    expect(await screen.findByText("ADMIN_CONTENT")).toBeInTheDocument();
    expect(api.authLogin).toHaveBeenCalledWith("pw");
  });

  it("failed login shows a generic error and no admin content", async () => {
    (api.authSession as any).mockResolvedValue(S({ authenticated: false }));
    (api.authLogin as any).mockRejectedValue(new Error("HTTP 401"));
    render(<AuthGate>{APP}</AuthGate>);
    fireEvent.change(await screen.findByLabelText(/password/i), { target: { value: "bad" } });
    fireEvent.click(screen.getByRole("button", { name: /login/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("ログインに失敗しました");
    expect(screen.queryByText("ADMIN_CONTENT")).not.toBeInTheDocument();
  });

  it("authenticated local mode shows identity + a logout control", async () => {
    (api.authSession as any).mockResolvedValue(S({ authenticated: true, identity: "admin" }));
    render(<AuthGate>{APP}</AuthGate>);
    expect(await screen.findByText("ADMIN_CONTENT")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /logout/i })).toBeInTheDocument();
  });

  it("logout returns to the login screen", async () => {
    (api.authSession as any)
      .mockResolvedValueOnce(S({ authenticated: true, identity: "admin" }))
      .mockResolvedValueOnce(S({ authenticated: false }));
    (api.authLogout as any).mockResolvedValue({ ok: true });
    render(<AuthGate>{APP}</AuthGate>);
    fireEvent.click(await screen.findByRole("button", { name: /logout/i }));
    await waitFor(() => expect(api.authLogout).toHaveBeenCalled());
    expect(await screen.findByLabelText(/password/i)).toBeInTheDocument();
  });
});
