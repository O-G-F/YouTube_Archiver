import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { ErrorBox, Loading } from "./ui";
import { Sparkline } from "./Sparkline";
import type { RecommendExport, SchedulerRun } from "../api/types";

const RUN_TYPES = ["", "liked_metadata", "liked_archive", "liked_retry", "collections", "comments", "all"];
const STATUSES = ["", "success", "partial_success", "failed"];

function StatusBadge({ s }: { s: string }) {
  const cls = s === "success" ? "ok" : s === "failed" ? "err" : "warn";
  return <span className={`badge ${cls}`}>{s}</span>;
}

async function copy(text: string, done: () => void) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    /* clipboard may be unavailable; selection fallback omitted */
  }
  done();
}

export function SchedulerHistory() {
  const [runType, setRunType] = useState("");
  const [status, setStatus] = useState("");
  const runs = useFetch(
    () => api.schedulerRuns({ run_type: runType || undefined, status: status || undefined, limit: 30 }),
    [runType, status]
  );
  const history = useFetch(() => api.likedProgressHistory(50), []);

  const [exp, setExp] = useState<RecommendExport | null>(null);
  const [expFmt, setExpFmt] = useState<"env" | "json">("env");
  const [busy, setBusy] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [detail, setDetail] = useState<SchedulerRun | null>(null);

  // cleanup
  const [keepLast, setKeepLast] = useState(20);
  const [cleanupMsg, setCleanupMsg] = useState<string | null>(null);

  async function loadExport(fmt: "env" | "json") {
    setBusy("export");
    setErr(null);
    setExpFmt(fmt);
    try {
      setExp(await api.schedulerRecommendExport(fmt));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function openDetail(runId: string) {
    setErr(null);
    try {
      setDetail(await api.schedulerRun(runId));
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  async function cleanupDryRun() {
    setBusy("cleanup");
    setCleanupMsg(null);
    try {
      const r = await api.schedulerRunsCleanup({ keep_last: keepLast, dry_run: true });
      setCleanupMsg(
        `Dry-run: would delete ${r.matched} of ${r.total_runs} runs (keep last ${keepLast}). ` +
          `Jobs are NOT deleted. Apply via CLI: archiver scheduler runs cleanup --keep-last ${keepLast} --apply`
      );
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const pts = history.data?.points ?? [];

  return (
    <>
      <div className="panel">
        <div className="spread">
          <h3>Recommended settings (suggestion only — not applied)</h3>
          <div className="row">
            <button disabled={busy === "export"} onClick={() => loadExport("env")}>Copy .env…</button>
            <button disabled={busy === "export"} onClick={() => loadExport("json")}>Copy JSON…</button>
          </div>
        </div>
        <ErrorBox error={err} />
        {exp ? (
          <>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="muted small">format: {expFmt} · review before applying · no secrets</span>
              <button onClick={() => copy(exp.content, () => setCopied(expFmt))}>
                {copied === expFmt ? "✓ copied" : "Copy to clipboard"}
              </button>
            </div>
            <pre className="log" aria-label="recommendation export">{exp.content}</pre>
            <p className="muted small">{exp.note}</p>
          </>
        ) : (
          <p className="muted small">
            過去の liked_archive 結果から安全寄りの値を提案し、<strong>.env 貼り付け用</strong>/JSON で出力します（**自動適用はしません**）。
          </p>
        )}
      </div>

      <div className="panel">
        <h3>Liked progress history</h3>
        <ErrorBox error={history.error} />
        {history.loading && !history.data ? (
          <Loading />
        ) : pts.length > 0 ? (
          <>
            <Sparkline
              series={[
                { label: "metadata_fetched", color: "#4f9cf9", values: pts.map((p) => p.metadata_fetched) },
                { label: "body_saved", color: "#3fb950", values: pts.map((p) => p.body_saved) },
                { label: "retryable", color: "#d29922", values: pts.map((p) => p.retryable_liked_jobs) },
                { label: "total_liked", color: "#8b949e", values: pts.map((p) => p.total_liked) },
              ]}
            />
            <div className="table-wrap">
              <table>
                <thead><tr><th>At</th><th>Run type</th><th>Metadata</th><th>Body</th><th>Retryable</th><th>Failed/partial</th></tr></thead>
                <tbody>
                  {pts.slice().reverse().map((p, i) => (
                    <tr key={i}>
                      <td className="small">{fmtDate(p.at)}</td>
                      <td className="mono small">{p.run_type}</td>
                      <td className="small">{p.metadata_fetched}/{p.total_liked}</td>
                      <td className="small">{p.body_saved}/{p.total_liked}</td>
                      <td className="small">{p.retryable_liked_jobs}</td>
                      <td className="small">{p.failed_liked_jobs}/{p.partial_liked_jobs}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <p className="muted small">No history yet — run a scheduler pass.</p>
        )}
      </div>

      <div className="panel">
        <div className="spread">
          <h3>Scheduler run history</h3>
          <div className="row">
            <select value={runType} onChange={(e) => setRunType(e.target.value)}>
              {RUN_TYPES.map((t) => <option key={t} value={t}>{t || "all types"}</option>)}
            </select>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUSES.map((t) => <option key={t} value={t}>{t || "any status"}</option>)}
            </select>
            <button onClick={runs.reload}>↻</button>
          </div>
        </div>
        <ErrorBox error={runs.error} />
        {runs.loading && !runs.data ? (
          <Loading />
        ) : runs.data && runs.data.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Run</th><th>Type</th><th>Status</th><th>Created</th><th>Skipped (a/d/b)</th><th>Body</th><th></th></tr>
              </thead>
              <tbody>
                {runs.data.map((r) => (
                  <tr key={r.run_id}>
                    <td className="mono small">
                      <button className="linklike" onClick={() => openDetail(r.run_id)}>{r.run_id.slice(0, 10)}</button>
                      <div className="muted small">{fmtDate(r.started_at)}</div>
                    </td>
                    <td className="mono small">{r.run_type}</td>
                    <td><StatusBadge s={r.status} /></td>
                    <td className="small">{r.jobs_created}</td>
                    <td className="small">{r.skipped_active_jobs}/{r.skipped_duplicates}/{r.skipped_backoff}</td>
                    <td className="small">{r.body_count_before}→{r.body_count_after}</td>
                    <td className="small"><Link to={`/jobs?scheduler_run_id=${r.run_id}`}>jobs →</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted small">No scheduler runs recorded yet.</p>
        )}

        <h4>Retention (manual)</h4>
        <div className="row">
          <label className="small">keep last
            <input type="number" min={1} value={keepLast} onChange={(e) => setKeepLast(Number(e.target.value))} style={{ width: 70, marginLeft: 6 }} />
          </label>
          <button disabled={busy === "cleanup"} onClick={cleanupDryRun}>
            {busy === "cleanup" ? <span className="spin" /> : "🧹"} Cleanup (dry-run)
          </button>
        </div>
        {cleanupMsg && <div className="flash">{cleanupMsg}</div>}
        <p className="muted small">cleanup は scheduler_runs のみ削除し、<strong>ジョブは消しません</strong>。実削除は CLI（<code>--apply</code>）で。</p>
      </div>

      {detail && (
        <div className="modal-overlay" role="dialog" aria-modal="true" onClick={() => setDetail(null)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <h2>Run {detail.run_id.slice(0, 12)}</h2>
              <button onClick={() => setDetail(null)}>✕</button>
            </div>
            <p className="muted small">
              {detail.run_type} · <StatusBadge s={detail.status} /> · {fmtDate(detail.started_at)} → {fmtDate(detail.finished_at)}
            </p>
            <div className="cards">
              <div className="card"><div className="label">selected</div><div className="value sm">{detail.selected_count}</div></div>
              <div className="card"><div className="label">created</div><div className="value sm">{detail.jobs_created}</div></div>
              <div className="card"><div className="label">submitted</div><div className="value sm">{detail.jobs_submitted}</div></div>
              <div className="card"><div className="label">skipped a/d/b</div><div className="value sm">{detail.skipped_active_jobs}/{detail.skipped_duplicates}/{detail.skipped_backoff}</div></div>
              <div className="card"><div className="label">body</div><div className="value sm">{detail.body_count_before}→{detail.body_count_after}</div></div>
              <div className="card"><div className="label">retryable</div><div className="value sm">{detail.retryable_count}</div></div>
            </div>
            <Link to={`/jobs?scheduler_run_id=${detail.run_id}`}>View related jobs →</Link>
          </div>
        </div>
      )}
    </>
  );
}
