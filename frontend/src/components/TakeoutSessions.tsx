import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { ErrorBox, Loading } from "./ui";

function KindBadge({ k }: { k: string }) {
  return <span className="badge muted">{k}</span>;
}

/** Takeout import session history (Phase 6C/6D). Counts only — no path/raw_json/PII. */
export function TakeoutSessions({ reloadKey }: { reloadKey?: number }) {
  // poll while any session is running (live progress for job imports)
  const sessions = useFetch(() => api.takeoutImportSessions({ limit: 30 }), [reloadKey]);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const anyRunning = (sessions.data ?? []).some((s) => s.status === "running");

  async function cancel(sessionId: string) {
    setBusy(sessionId);
    setErr(null);
    try {
      await api.takeoutImportCancel(sessionId);
      sessions.reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="panel">
      <div className="spread">
        <h2>Import session history {anyRunning && <span className="badge run">running</span>}</h2>
        <button onClick={sessions.reload}>↻ {sessions.loading && <span className="spin" />}</button>
      </div>
      <p className="muted small">
        差分インポートの履歴（件数のみ・個人情報や raw_json は保存しません）。背景ジョブは job にリンクします。
      </p>
      <ErrorBox error={err} />
      <ErrorBox error={sessions.error} />
      {sessions.loading && !sessions.data ? (
        <Loading />
      ) : sessions.data && sessions.data.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Kind</th>
                <th>Source</th>
                <th>File</th>
                <th>Imported</th>
                <th>Skipped</th>
                <th>Updated</th>
                <th>Scanned</th>
                <th>eps</th>
                <th>Job</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {sessions.data.map((s) => (
                <tr key={s.session_id}>
                  <td className="small">{fmtDate(s.started_at)}</td>
                  <td><KindBadge k={s.import_kind} /></td>
                  <td className="small muted">{s.source_kind ?? "—"}</td>
                  <td className="mono small">{s.path_basename ?? "—"}</td>
                  <td className="small">{s.imported}</td>
                  <td className="small">{s.skipped_duplicate}</td>
                  <td className="small">{s.updated}</td>
                  <td className="small">{s.scanned}</td>
                  <td className="small">{s.entries_per_second ?? "—"}</td>
                  <td className="small">{s.job_id ? <Link to={`/jobs/${s.job_id}`}>#{s.job_id}</Link> : "—"}</td>
                  <td>
                    {s.status === "running" ? (
                      <button className="sm" disabled={busy === s.session_id} onClick={() => cancel(s.session_id)}>
                        {busy === s.session_id ? <span className="spin" /> : "✕"} cancel
                      </button>
                    ) : (
                      <>
                        {s.dry_run && <span className="badge warn">dry-run</span>}
                        {s.status === "cancelled" && <span className="badge warn">cancelled</span>}
                        {s.status === "failed" && <span className="badge err">failed</span>}
                        {s.parser_backend && <span className="badge muted" title="parser">{s.parser_backend}</span>}
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted small">No imports yet.</p>
      )}
    </div>
  );
}
