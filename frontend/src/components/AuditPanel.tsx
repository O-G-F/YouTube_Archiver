import { useState } from "react";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "./ui";

const SEV_BADGE: Record<string, string> = { info: "muted", warning: "warn", critical: "err" };

/**
 * Phase 9E: read-only Audit / Observability panel. Shows the tamper-evident chain
 * status, recent security events, and counts. There are NO edit/delete controls;
 * identifiers are shown only as pseudonym hashes (never raw email/IP).
 */
export function AuditPanel() {
  const [rid, setRid] = useState("");
  const [severity, setSeverity] = useState("");
  const verify = useFetch(() => api.auditVerify(), []);
  const stats = useFetch(() => api.auditStats(30), []);
  const events = useFetch(
    () => api.auditEvents({ limit: 25, severity: severity || undefined, request_id: rid || undefined }),
    [severity, rid]
  );
  const v = verify.data;

  return (
    <div className="panel">
      <div className="spread">
        <h2>Audit &amp; observability (Phase 9E)</h2>
        <button onClick={() => { verify.reload(); stats.reload(); events.reload(); }}>
          ↻ {(verify.loading || events.loading) && <span className="spin" />}
        </button>
      </div>
      <ErrorBox error={verify.error} />
      <ErrorBox error={events.error} />

      <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 8 }}>
        <span className="muted small">audit chain:</span>
        {v ? (
          <span
            className={`badge ${v.valid ? (v.valid_with_warnings ? "warn" : "ok") : "err"}`}
            title={v.valid ? "" : `${v.failure_reason_code ?? ""} @ #${v.first_invalid_event_id ?? "?"}`}
          >
            {v.valid ? (v.valid_with_warnings ? "VALID (warnings)" : "VALID") : "INVALID"} · {v.checked_count} ev · {v.segment_count} seg ·{" "}
            {v.signed ? `key ${v.current_signing_key_id}` : "unsigned"}
            {v.unsigned_event_count > 0 ? ` · ${v.unsigned_event_count} unsigned` : ""}
            {v.missing_verification_keys?.length ? ` · missing:${v.missing_verification_keys.join(",")}` : ""}
          </span>
        ) : <span className="muted small">…</span>}
        {stats.data && (
          <>
            <span className="muted small">30d total {stats.data.total}</span>
            {Object.entries(stats.data.by_severity).map(([s, n]) => (
              <span key={s} className={`badge ${SEV_BADGE[s] ?? "muted"}`}>{s}: {n}</span>
            ))}
          </>
        )}
      </div>

      <div className="toolbar" style={{ gap: 8 }}>
        <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="">all severities</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="critical">critical</option>
        </select>
        <input value={rid} onChange={(e) => setRid(e.target.value)} placeholder="request/correlation id" style={{ width: 200 }} />
        {rid && <button onClick={() => setRid("")}>clear</button>}
      </div>

      {events.loading && !events.data ? (
        <Loading />
      ) : events.data && events.data.events.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>id</th><th>time</th><th>sev</th><th>category</th><th>event</th><th>outcome</th><th>actor</th><th>reason</th></tr>
            </thead>
            <tbody>
              {events.data.events.map((e) => (
                <tr key={e.id}>
                  <td>{e.id}</td>
                  <td className="muted small">{e.occurred_at?.replace("T", " ").slice(0, 19)}</td>
                  <td><span className={`badge ${SEV_BADGE[e.severity] ?? "muted"}`}>{e.severity}</span></td>
                  <td>{e.category}</td>
                  <td>{e.event_type}</td>
                  <td>{e.outcome}</td>
                  <td className="muted small">{e.actor_kind}</td>
                  <td className="muted small">{e.reason_code || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted small">No audit events.</p>
      )}
      <p className="muted small">
        識別子は擬似ID（HMAC）のみ・secret/IP/email/path は非表示。監査イベントは追記専用で編集/削除できません。
      </p>
    </div>
  );
}
