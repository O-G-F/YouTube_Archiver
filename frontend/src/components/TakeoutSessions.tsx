import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { ErrorBox, Loading } from "./ui";

function KindBadge({ k }: { k: string }) {
  return <span className="badge muted">{k}</span>;
}

/** Takeout import session history (Phase 6C). Counts only — no path/raw_json/PII. */
export function TakeoutSessions({ reloadKey }: { reloadKey?: number }) {
  const sessions = useFetch(() => api.takeoutImportSessions({ limit: 30 }), [reloadKey]);

  return (
    <div className="panel">
      <div className="spread">
        <h2>Import session history</h2>
        <button onClick={sessions.reload}>↻ {sessions.loading && <span className="spin" />}</button>
      </div>
      <p className="muted small">
        差分インポートの履歴（件数のみ・個人情報や raw_json は保存しません）。
      </p>
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
                <th>File</th>
                <th>Imported</th>
                <th>Skipped</th>
                <th>Updated</th>
                <th>Failed</th>
                <th>Scanned</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {sessions.data.map((s) => (
                <tr key={s.session_id}>
                  <td className="small">{fmtDate(s.started_at)}</td>
                  <td><KindBadge k={s.import_kind} /></td>
                  <td className="mono small">{s.path_basename ?? "—"}</td>
                  <td className="small">{s.imported}</td>
                  <td className="small">{s.skipped_duplicate}</td>
                  <td className="small">{s.updated}</td>
                  <td className="small">{s.failed}</td>
                  <td className="small">{s.scanned}</td>
                  <td>
                    {s.dry_run && <span className="badge warn">dry-run</span>}
                    {s.status !== "success" && <span className="badge err">{s.status}</span>}
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
