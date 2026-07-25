import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api/endpoints";
import { setUnauthorizedHandler } from "../api/client";
import type { AuthSession } from "../api/types";
import { Login } from "./Login";
import { Loading } from "./ui";

/**
 * Phase 9C: gates the whole admin SPA behind auth when AUTH_MODE != disabled.
 * disabled mode (development) renders the app unchanged. Any 401 from the API
 * (expired/absent session) routes back to the login screen.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [sess, setSess] = useState<AuthSession | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    try {
      setSess(await api.authSession());
    } catch {
      setSess(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    setUnauthorizedHandler(() =>
      setSess((s) => (s ? { ...s, authenticated: false } : s))
    );
    return () => setUnauthorizedHandler(null);
  }, []);

  if (loading && !sess) {
    return (
      <div style={{ padding: 48 }}>
        <Loading />
      </div>
    );
  }

  const mode = sess?.auth_mode ?? "disabled";
  const allowed = mode === "disabled" || sess?.authenticated;

  if (allowed) {
    return (
      <>
        {sess && mode !== "disabled" && <AuthBar sess={sess} onLogout={refresh} />}
        {children}
      </>
    );
  }

  if (mode === "local") return <Login onSuccess={refresh} />;
  return <ProxyAuthNotice />;
}

function AuthBar({ sess, onLogout }: { sess: AuthSession; onLogout: () => void }) {
  const [busy, setBusy] = useState(false);
  async function logout() {
    setBusy(true);
    try {
      await api.authLogout();
    } catch {
      /* ignore */
    } finally {
      onLogout();
    }
  }
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "flex-end",
        alignItems: "center",
        gap: 10,
        padding: "6px 14px",
        fontSize: 13,
        borderBottom: "1px solid var(--border, #333)",
      }}
    >
      <span className="muted" title="signed in">🔒 {sess.identity || "admin"}</span>
      <button onClick={logout} disabled={busy}>
        {busy ? <span className="spin" /> : "Logout"}
      </button>
    </div>
  );
}

function ProxyAuthNotice() {
  return (
    <div style={{ maxWidth: 420, margin: "12vh auto", padding: "0 16px" }}>
      <h1 style={{ fontSize: 20 }}>認証が必要です</h1>
      <p className="muted small">
        このデプロイは信頼済みリバースプロキシ（例: Cloudflare Access）経由での認証を要求します。
        プロキシで認証後にアクセスしてください。
      </p>
    </div>
  );
}
