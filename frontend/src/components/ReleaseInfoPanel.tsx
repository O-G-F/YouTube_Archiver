import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "./ui";

const ST_BADGE: Record<string, string> = { pass: "ok", warn: "warn", fail: "err" };

function fmtAge(sec: number | null | undefined): string {
  if (sec == null) return "unknown";
  if (sec < 90) return "just now";
  const m = sec / 60;
  if (m < 90) return `${Math.round(m)}m ago`;
  const h = m / 60;
  if (h < 48) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

/**
 * Phase 10A / 11B: read-only runtime & release status. It SEPARATES the running
 * runtime from the last scanned release so a stale manifest is never shown as the
 * current runtime's scan. Only short ids / hashes / counts / statuses / ages —
 * no repository paths, host paths, registry credentials, secrets, or raw scanner
 * commands, and NO deploy / dependency-update controls.
 */
export function ReleaseInfoPanel() {
  const readiness = useFetch(() => api.releaseReadiness(), []);
  const d = readiness.data;
  const v = d?.version;
  const m = d?.manifest;
  const sp = d?.security_posture ?? null;
  const rr = d?.runtime_release ?? null;
  const sev = m?.vulnerability_severities ?? null;

  const verdictBadge =
    rr?.verdict === "match" ? "ok" : rr?.verdict === "mismatch" ? "warn" : "muted";

  return (
    <div className="panel">
      <div className="spread">
        <h2>Runtime &amp; release status</h2>
        <button onClick={() => readiness.reload()} aria-label="Refresh runtime & release status">
          ↻ {readiness.loading && <span className="spin" />}
        </button>
      </div>
      <ErrorBox error={readiness.error} />
      {!d && readiness.loading ? (
        <Loading what="runtime status" />
      ) : d && v ? (
        <>
          {/* runtime-vs-release verdict — the key distinction */}
          {rr ? (
            <p className="row" style={{ gap: 8, alignItems: "center", marginBottom: 10 }}>
              <span className={`badge ${verdictBadge}`}>
                {rr.verdict === "match"
                  ? "runtime = scanned release"
                  : rr.verdict === "mismatch"
                    ? "runtime ≠ scanned release"
                    : "no scanned release"}
              </span>
              <span className="muted small">{rr.message}</span>
            </p>
          ) : null}

          <div className="row" style={{ flexWrap: "wrap", gap: 16, alignItems: "flex-start" }}>
            {/* RUNNING RUNTIME */}
            <div style={{ flex: "1 1 280px", minWidth: 260 }}>
              <h3 className="small" style={{ margin: "0 0 6px" }}>Running runtime</h3>
              <table className="kv"><tbody>
                <tr><td className="muted small">app version</td><td><code>{v.app_version}</code></td></tr>
                <tr><td className="muted small">build id</td><td><code>{v.build_id}</code></td></tr>
                {v.git_commit ? <tr><td className="muted small">commit</td><td><code>{v.git_commit.slice(0, 12)}</code></td></tr> : null}
                <tr><td className="muted small">tree</td><td>{v.git_tree_clean === true ? "clean" : v.git_tree_clean === false ? "DIRTY" : "?"}</td></tr>
                <tr><td className="muted small">schema</td><td><code>{v.schema_head ?? "-"}</code></td></tr>
                {v.frontend_build_id ? <tr><td className="muted small">ui</td><td><code>{v.frontend_build_id}</code></td></tr> : null}
              </tbody></table>
            </div>

            {/* LAST SCANNED RELEASE */}
            <div style={{ flex: "1 1 280px", minWidth: 260 }}>
              <h3 className="small" style={{ margin: "0 0 6px" }}>Last scanned release</h3>
              {m ? (
                <table className="kv"><tbody>
                  <tr><td className="muted small">release</td><td><code>{m.release_id}</code></td></tr>
                  <tr><td className="muted small">version</td><td><code>{m.app_version ?? "-"}</code></td></tr>
                  <tr><td className="muted small">build id</td><td><code>{rr?.manifest_build_id ?? "-"}</code></td></tr>
                  <tr><td className="muted small">recorded</td><td className="muted small">{fmtAge(rr?.manifest_age_seconds)}</td></tr>
                  <tr><td className="muted small">scan DB</td><td className="muted small">{fmtAge(rr?.scan_age_seconds)}</td></tr>
                  <tr><td className="muted small">vuln scan</td><td>
                    <span className={`badge ${m.vulnerability_status === "fail" ? "err" : m.vulnerability_status === "warn" ? "warn" : "muted"}`}>
                      {m.vulnerability_status ?? "none"}
                    </span>
                    {sev ? Object.entries(sev).map(([s, n]) => (
                      <span key={s} className={`badge ${s === "CRITICAL" ? "err" : s === "HIGH" ? "warn" : "muted"}`} style={{ marginLeft: 4 }}>{s}: {n}</span>
                    )) : null}
                  </td></tr>
                  <tr><td className="muted small">SBOM</td><td>{m.sbom_present ? <code>{(m.sbom_sha256 ?? "").slice(0, 12) || "yes"}…</code> : "absent"}</td></tr>
                  <tr><td className="muted small">integrity</td><td className="muted small">{m.integrity_scheme ?? "-"}</td></tr>
                </tbody></table>
              ) : (
                <p className="muted small">No scanned release recorded (run <code>scripts/build-release.sh</code>).</p>
              )}
            </div>
          </div>

          {/* SECURITY POSTURE (Phase 11A) — honest accepted-risk, never hidden */}
          {sp ? (
            <div
              className="row"
              style={{ flexWrap: "wrap", gap: 8, alignItems: "center", margin: "12px 0 8px", padding: "8px 10px", border: "1px solid var(--border)", borderRadius: 6 }}
            >
              <strong className="small">Security posture:</strong>
              <span className={`badge ${sp.operating_mode === "production" ? "muted" : "warn"}`}>
                {sp.operating_mode === "production" ? "production" : "local single-user (dev)"}
              </span>
              {typeof sp.known_critical_accepted === "number" && sp.known_critical_accepted > 0 ? (
                <span className="badge err" title="known CRITICAL OS CVEs accepted as local-use risk (not hidden)">
                  {sp.known_critical_accepted} known CRITICAL — accepted (local)
                </span>
              ) : null}
              <span className={`badge ${sp.production_ready ? "ok" : "warn"}`}>
                {sp.production_ready ? "production-ready" : "not production-ready"}
              </span>
              <span className="muted small">{sp.active_vulnerability_exceptions} active exception(s)</span>
              <span className="muted small">{sp.note} 詳細: <code>{sp.decision_dossier_doc}</code></span>
            </div>
          ) : null}

          <details style={{ marginTop: 6 }}>
            <summary className="small" style={{ cursor: "pointer" }}>Release-check details ({d.counts.fail ?? 0} fail · {d.counts.warn ?? 0} warn · {d.counts.pass ?? 0} pass)</summary>
            <div className="table-wrap" style={{ marginTop: 6 }}>
              <table>
                <thead><tr><th>check</th><th>status</th><th>detail</th></tr></thead>
                <tbody>
                  {d.checks.map((c) => (
                    <tr key={c.name}>
                      <td>{c.name}</td>
                      <td><span className={`badge ${ST_BADGE[c.status] ?? "muted"}`}>{c.status}</span></td>
                      <td className="muted small">{c.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
          <p className="muted small" style={{ marginTop: 6 }}>
            読み取り専用です。release build は <code>scripts/build-release.sh</code>、検証は
            <code>scripts/verify-release.sh</code> で実行します（リポジトリパス・secret・scanner の生コマンドは表示しません）。
          </p>
        </>
      ) : null}
    </div>
  );
}
