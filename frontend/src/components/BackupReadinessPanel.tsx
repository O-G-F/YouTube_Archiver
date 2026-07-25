import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "./ui";

const ST_BADGE: Record<string, string> = { pass: "ok", warn: "warn", fail: "err" };

function age(h?: number | null, unit: "h" | "d" = "h"): string {
  if (h === null || h === undefined) return "未記録";
  return unit === "d" ? `~${h.toFixed(1)}日前` : `~${h.toFixed(1)}時間前`;
}

/**
 * Phase 9F: read-only backup / disaster-recovery readiness panel. Shows backup
 * freshness, manifest integrity summary, last verification, and last isolated
 * restore rehearsal. Everything is basenames / counts / ages — no host paths,
 * no secret values, and there are NO mutating controls (backups, verification
 * and rehearsals run via the operator scripts, never from the UI).
 */
export function BackupReadinessPanel() {
  const readiness = useFetch(() => api.backupReadiness(), []);
  const d = readiness.data;

  return (
    <div className="panel">
      <div className="spread">
        <h2>Backup &amp; recovery readiness (Phase 9F)</h2>
        <button onClick={() => readiness.reload()}>
          ↻ {readiness.loading && <span className="spin" />}
        </button>
      </div>
      <ErrorBox error={readiness.error} />
      {!d && readiness.loading ? (
        <Loading />
      ) : d ? (
        <>
          <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 8 }}>
            <span className={`badge ${ST_BADGE[d.overall] ?? "muted"}`}>overall: {d.overall}</span>
            <span className="muted small">最終バックアップ: {age(d.backup_age_hours)}</span>
            <span className="muted small">最終整合性検証: {age(d.backup_verified_age_hours)}</span>
            <span className="muted small">最終restore rehearsal: {age(d.restore_rehearsal_age_days, "d")}</span>
          </div>
          {d.manifest?.artifact && (
            <p className="muted small">
              manifest: <code>{d.manifest.artifact}</code>
              {d.manifest.backup_id ? <> · id <code>{d.manifest.backup_id}</code></> : null}
              {" · "}{d.manifest.size_bytes != null ? `${d.manifest.size_bytes} bytes` : "size不明"}
              {" · sha256 "}<code>{(d.manifest.sha256 ?? "").slice(0, 12) || "—"}…</code>
              {d.manifest.schema_head ? <> · schema <code>{d.manifest.schema_head}</code></> : null}
              {d.manifest.audit_head_event_id != null ? <> · audit head #{d.manifest.audit_head_event_id}</> : null}
              {d.manifest.archive_manifest_artifact ? <> · archive <code>{d.manifest.archive_manifest_artifact}</code></> : null}
              {d.manifest.completed === false ? " · 未完了!" : null}
            </p>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>check</th><th>status</th><th>detail</th></tr>
              </thead>
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
          <p className="muted small">
            読み取り専用パネルです。バックアップ/検証/rehearsalは <code>scripts/backup.sh</code>・
            <code>scripts/verify-backup.sh</code>・<code>scripts/restore-rehearsal.sh</code> で実行します
            （ホストパス・secretは表示しません）。
          </p>
        </>
      ) : null}
    </div>
  );
}
