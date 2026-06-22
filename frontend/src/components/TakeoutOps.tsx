import { useState } from "react";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "./ui";
import type { PreflightLarge } from "../api/types";

function StatusBadge({ status }: { status: string }) {
  const cls = status === "ok" ? "ok" : status === "warn" ? "warn" : "err";
  return <span className={`badge ${cls}`}>{status}</span>;
}

/** Phase 6F operational safety: build identity + stale-worker guard,
 *  large-import preflight, and auto session-cleanup status. */
export function TakeoutOps({ path }: { path: string }) {
  const health = useFetch(() => api.systemHealthFull(), []);
  const cleanup = useFetch(() => api.takeoutCleanupStatus(), []);
  const [kind, setKind] = useState("all");
  const [pl, setPl] = useState<PreflightLarge | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function runPreflightLarge() {
    if (!path) {
      setErr("Enter a ZIP path above first.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      setPl(await api.takeoutPreflightLarge({ path, kind }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const h = health.data;
  const c = cleanup.data;
  return (
    <div className="panel">
      <div className="spread">
        <h2>System &amp; large-import preflight</h2>
        <button onClick={health.reload}>↻ {health.loading && <span className="spin" />}</button>
      </div>
      <ErrorBox error={err} />
      <ErrorBox error={health.error} />

      {/* Build identity + stale-worker guard */}
      {health.loading && !h ? (
        <Loading />
      ) : h ? (
        <div className="cards">
          <div className="card">
            <div className="label">web build_id</div>
            <div className="value sm mono">{h.build_info.build_id}</div>
          </div>
          <div className="card">
            <div className="label">worker match</div>
            <div className="value sm">
              {h.workers.length === 0 ? (
                <span className="badge err">no worker</span>
              ) : h.worker_build_match ? (
                <span className="badge ok">match</span>
              ) : (
                <span className="badge err">STALE WORKER</span>
              )}
            </div>
          </div>
          <div className="card">
            <div className="label">schema head</div>
            <div className="value sm">
              {h.schema_head_match === null ? (
                <span className="badge muted">{h.build_info.schema_head ?? "—"}</span>
              ) : h.schema_head_match ? (
                <span className="badge ok">{h.build_info.schema_head}</span>
              ) : (
                <span className="badge err">mismatch</span>
              )}
            </div>
          </div>
          <div className="card">
            <div className="label">db / redis</div>
            <div className="value sm">
              <span className={`badge ${h.database ? "ok" : "err"}`}>db</span>{" "}
              <span className={`badge ${h.redis ? "ok" : "err"}`}>redis</span>
            </div>
          </div>
        </div>
      ) : null}
      {h && h.workers.length > 0 && (
        <p className="muted small">
          workers:{" "}
          {h.workers.map((w, i) => (
            <span key={i} className="badge muted" title={w.worker_id ?? ""}>
              {(w.build_id ?? "?").slice(0, 12)}
              {w.stale ? " (stale)" : ""}
              {w.takeout_import ? "" : " ⚠no-takeout"}
            </span>
          ))}
        </p>
      )}
      {h && !h.worker_build_match && h.workers.length > 0 && (
        <div className="flash" style={{ borderColor: "var(--err)" }}>
          ⚠ Stale worker: web and worker build_id differ. Rebuild ALL images before a large import:
          <code> docker compose build web worker migrate</code> then <code>docker compose up -d</code>.
        </div>
      )}

      {/* Large-import preflight */}
      <h3>Preflight large import</h3>
      <div className="row">
        <label className="small">kind
          <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ marginLeft: 6 }}>
            <option value="all">all</option>
            <option value="liked_videos">liked_videos</option>
            <option value="watch_history">watch_history</option>
            <option value="search_history">search_history</option>
          </select>
        </label>
        <button disabled={busy} onClick={runPreflightLarge}>
          {busy ? <span className="spin" /> : "🚦"} Preflight-large {path ? `(${path})` : ""}
        </button>
      </div>
      {pl && (
        <div style={{ marginTop: 8 }}>
          <div className="row">
            <span className={`badge ${pl.ok ? "ok" : "err"}`}>{pl.ok ? "PASS" : "FAIL"}</span>
            <span className="muted small">zip {pl.path_basename} · parser {pl.parser_backend}</span>
          </div>
          <div className="table-wrap" style={{ marginTop: 6 }}>
            <table>
              <thead><tr><th>kind</th><th>est. (sample)</th><th>eps</th><th>peak MB</th><th>current DB</th></tr></thead>
              <tbody>
                {Object.entries(pl.results).map(([k, r]) => (
                  <tr key={k}>
                    <td className="mono small">{k}</td>
                    <td className="small">{r.sample_scanned}</td>
                    <td className="small">{r.entries_per_second ?? "—"}</td>
                    <td className="small">{r.peak_memory_mb ?? "—"}</td>
                    <td className="small">{r.current_db_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="small" style={{ marginTop: 6 }}>
            {pl.checks.map((ck, i) => (
              <li key={i}><StatusBadge status={ck.status} /> {ck.name}: <span className="muted">{ck.detail}</span></li>
            ))}
          </ul>
          {pl.recommended_command && (
            <p className="small">next: <code>{pl.recommended_command}</code></p>
          )}
          <p className="muted small">
            raw_json policy: large imports default to <strong>--no-raw-json</strong> (drops raw blobs, keeps normalized fields).
          </p>
        </div>
      )}

      {/* Auto cleanup status */}
      <h3>Auto session cleanup</h3>
      <ErrorBox error={cleanup.error} />
      {c ? (
        <div className="cards">
          <div className="card"><div className="label">enabled</div><div className="value sm">
            <span className={`badge ${c.enabled ? "ok" : "muted"}`}>{c.enabled ? "on" : "off"}</span></div></div>
          <div className="card"><div className="label">interval</div><div className="value sm">{c.interval_hours}h</div></div>
          <div className="card"><div className="label">keep_last</div><div className="value sm">{c.keep_last}</div></div>
          <div className="card"><div className="label">retention</div><div className="value sm">{c.retention_days}d</div></div>
          <div className="card"><div className="label">last run</div><div className="value sm">{c.last_run_at ? c.last_run_at.slice(0, 16).replace("T", " ") : "—"}</div></div>
        </div>
      ) : (
        <p className="muted small">No cleanup status yet.</p>
      )}
      <p className="muted small">
        Auto-cleanup prunes <strong>import session rows only</strong> — jobs and imported data are never deleted.
      </p>
    </div>
  );
}
