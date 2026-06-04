import type { ReactNode } from "react";
import { statusKind, stateKind } from "../lib/format";

export function Loading({ what }: { what?: string }) {
  return (
    <div className="loading">
      <span className="spin" /> Loading{what ? ` ${what}` : ""}…
    </div>
  );
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="error-box">⚠ {error}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Card({ label, value, sm }: { label: string; value: ReactNode; sm?: boolean }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={sm ? "value sm" : "value"}>{value}</div>
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge ${statusKind(status)}`}>{status}</span>;
}

export function StateBadge({ state }: { state: string | null | undefined }) {
  return <span className={`badge ${stateKind(state)}`}>{state ?? "—"}</span>;
}

export function Bool({ value }: { value: boolean }) {
  return <span className={`badge ${value ? "ok" : "muted"}`}>{value ? "yes" : "no"}</span>;
}

export function JsonBlock({ value }: { value: unknown }) {
  if (value == null) return <span className="muted">—</span>;
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function KV({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <div className="kv">
      {rows.map(([k, v], i) => (
        <div key={i} style={{ display: "contents" }}>
          <div className="k">{k}</div>
          <div className="v">{v}</div>
        </div>
      ))}
    </div>
  );
}
