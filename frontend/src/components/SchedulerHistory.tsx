import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { ErrorBox, Loading } from "./ui";
import type { RecommendSettings } from "../api/types";

function StatusBadge({ s }: { s: string }) {
  const cls = s === "success" ? "ok" : s === "failed" ? "err" : "warn";
  return <span className={`badge ${cls}`}>{s}</span>;
}

export function SchedulerHistory() {
  const runs = useFetch(() => api.schedulerRuns({ limit: 30 }), []);
  const history = useFetch(() => api.likedProgressHistory(30), []);
  const [rec, setRec] = useState<RecommendSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function recommend() {
    setBusy(true);
    setErr(null);
    try {
      setRec(await api.schedulerRecommend(30));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="panel">
        <div className="spread">
          <h3>Recommended settings (suggestion only — not applied)</h3>
          <button disabled={busy} onClick={recommend}>
            {busy ? <span className="spin" /> : "💡"} Compute
          </button>
        </div>
        <ErrorBox error={err} />
        {rec ? (
          <>
            <p className="muted small">
              rates: success={String(rec.rates.success_rate ?? "—")} / throttle(429+incomplete)=
              {String(rec.rates.throttle_rate ?? "—")} · based on {String(rec.based_on.finished_archive_jobs ?? 0)} finished archive jobs
            </p>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Setting</th><th>Current</th><th>Recommended</th></tr></thead>
                <tbody>
                  {Object.keys(rec.recommended).map((k) => {
                    const changed = String(rec.recommended[k]) !== String(rec.current[k]);
                    return (
                      <tr key={k}>
                        <td className="mono small">{k}</td>
                        <td className="small">{String(rec.current[k])}</td>
                        <td className="small">
                          <span className={changed ? "badge warn" : "badge muted"}>{String(rec.recommended[k])}</span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <ul className="muted small" style={{ lineHeight: 1.7 }}>
              {rec.reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
            <p className="muted small">{rec.note}</p>
          </>
        ) : (
          <p className="muted small">過去の liked_archive 実行結果から、安全寄りの archive/retry limit・delay を提案します（自動変更はしません）。</p>
        )}
      </div>

      <div className="panel">
        <h3>Liked progress history</h3>
        <ErrorBox error={history.error} />
        {history.loading && !history.data ? (
          <Loading />
        ) : history.data && history.data.points.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead><tr><th>At</th><th>Run type</th><th>Metadata</th><th>Body</th><th>Retryable</th></tr></thead>
              <tbody>
                {history.data.points.slice().reverse().map((p, i) => (
                  <tr key={i}>
                    <td className="small">{fmtDate(p.at)}</td>
                    <td className="mono small">{p.run_type}</td>
                    <td className="small">{p.metadata_fetched}/{p.total_liked}</td>
                    <td className="small">{p.body_saved}/{p.total_liked}</td>
                    <td className="small">{p.retryable_liked_jobs}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted small">No history yet — run a scheduler pass.</p>
        )}
      </div>

      <div className="panel">
        <div className="spread">
          <h3>Scheduler run history</h3>
          <button onClick={runs.reload}>↻</button>
        </div>
        <ErrorBox error={runs.error} />
        {runs.loading && !runs.data ? (
          <Loading />
        ) : runs.data && runs.data.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Run</th><th>Type</th><th>Status</th><th>Created</th><th>Skipped (a/d/b)</th><th>Body</th><th>Jobs</th></tr>
              </thead>
              <tbody>
                {runs.data.map((r) => (
                  <tr key={r.run_id}>
                    <td className="mono small">{r.run_id.slice(0, 10)}<div className="muted small">{fmtDate(r.started_at)}</div></td>
                    <td className="mono small">{r.run_type}</td>
                    <td><StatusBadge s={r.status} /></td>
                    <td className="small">{r.jobs_created}</td>
                    <td className="small">{r.skipped_active_jobs}/{r.skipped_duplicates}/{r.skipped_backoff}</td>
                    <td className="small">{r.body_count_before}→{r.body_count_after}</td>
                    <td className="small"><Link to={`/jobs?scheduler_run_id=${r.run_id}`}>view →</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted small">No scheduler runs recorded yet.</p>
        )}
      </div>
    </>
  );
}
