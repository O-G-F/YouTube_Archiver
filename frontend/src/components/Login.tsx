import { useState } from "react";
import { api } from "../api/endpoints";

/**
 * Phase 9C: admin login (AUTH_MODE=local). Submits the password to the API which
 * sets an HttpOnly signed-session cookie + a readable CSRF cookie. Failures show a
 * single generic message (no distinction between wrong password / misconfig).
 */
export function Login({ onSuccess }: { onSuccess: () => void }) {
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.authLogin(pw);
      setPw("");
      onSuccess();
    } catch {
      setErr("ログインに失敗しました。"); // generic — never reveals the reason
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ maxWidth: 360, margin: "12vh auto", padding: "0 16px" }}>
      <h1 style={{ fontSize: 20, marginBottom: 4 }}>YouTube Archiver</h1>
      <p className="muted small" style={{ marginBottom: 16 }}>管理者ログインが必要です。</p>
      <form onSubmit={submit} aria-label="login">
        <label style={{ display: "block", marginBottom: 8 }}>
          Password
          <input
            type="password"
            autoFocus
            autoComplete="current-password"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            style={{ width: "100%", marginTop: 4 }}
          />
        </label>
        <button className="primary" type="submit" disabled={busy || !pw} style={{ width: "100%" }}>
          {busy ? <span className="spin" /> : "Login"}
        </button>
      </form>
      {err && <div className="flash err" role="alert" style={{ marginTop: 12 }}>{err}</div>}
    </div>
  );
}
