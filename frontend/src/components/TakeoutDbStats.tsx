import { useState } from "react";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "./ui";
import type { TakeoutBenchmarkLarge, TakeoutSessionCleanup } from "../api/types";

/** DB size card + benchmark-large + session cleanup (Phase 6E). */
export function TakeoutDbStats({ path, onChanged }: { path: string; onChanged?: () => void }) {
  const stats = useFetch(() => api.dbStats(), []);
  const [bench, setBench] = useState<TakeoutBenchmarkLarge | null>(null);
  const [cleanup, setCleanup] = useState<TakeoutSessionCleanup | null>(null);
  const [keepLast, setKeepLast] = useState(50);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function runBenchLarge() {
    if (!path) {
      setErr("Enter a ZIP path above first.");
      return;
    }
    setBusy("bench");
    setErr(null);
    try {
      setBench(await api.takeoutBenchmarkLarge({ path }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function runCleanup(dryRun: boolean) {
    setBusy("cleanup");
    setErr(null);
    try {
      const r = await api.takeoutSessionsCleanup({ keep_last: keepLast, dry_run: dryRun });
      setCleanup(r);
      if (!dryRun) {
        stats.reload();
        onChanged?.();
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const s = stats.data;
  return (
    <div className="panel">
      <div className="spread">
        <h2>DB size & large-import tools</h2>
        <button onClick={stats.reload}>↻ {stats.loading && <span className="spin" />}</button>
      </div>
      <ErrorBox error={err} />
      <ErrorBox error={stats.error} />
      {stats.loading && !s ? (
        <Loading />
      ) : s ? (
        <div className="cards">
          <div className="card"><div className="label">DB size</div><div className="value sm">{s.total_size_mb ?? "—"} MB <span className="muted">({s.dialect})</span></div></div>
          <div className="card"><div className="label">Videos</div><div className="value sm">{s.videos}</div></div>
          <div className="card"><div className="label">Liked</div><div className="value sm">{s.liked_videos}</div></div>
          <div className="card"><div className="label">Watch</div><div className="value sm">{s.watch_history_events}</div></div>
          <div className="card"><div className="label">Search</div><div className="value sm">{s.search_history_events}</div></div>
          <div className="card"><div className="label">raw_json rows</div><div className="value sm">{s.raw_json_stored_total}</div></div>
          <div className="card"><div className="label">Import sessions</div><div className="value sm">{s.takeout_import_sessions}</div></div>
        </div>
      ) : null}

      <h3>Large benchmark (dry-run, full scan)</h3>
      <div className="row">
        <button disabled={busy === "bench"} onClick={runBenchLarge}>
          {busy === "bench" ? <span className="spin" /> : "⏱"} Benchmark-large {path ? `(${path})` : ""}
        </button>
      </div>
      {bench && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Kind</th><th>Scanned</th><th>eps</th><th>peak MB</th><th>est. full import</th><th>batch</th><th>parser</th></tr></thead>
            <tbody>
              {Object.entries(bench.results).map(([k, b]) => (
                <tr key={k}>
                  <td className="mono small">{k}</td>
                  <td className="small">{b.scanned}</td>
                  <td className="small">{b.entries_per_second ?? "—"}</td>
                  <td className="small">{b.peak_memory_mb ?? "—"}</td>
                  <td className="small">{b.estimated_full_import_time_seconds ?? "—"}s</td>
                  <td className="small">{b.recommended_batch_size ?? "—"}</td>
                  <td className="small">{b.parser_backend}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3>Session cleanup</h3>
      <p className="muted small">
        古い import session 行のみ削除します。<strong>job と取り込み済みデータ（liked/watch/search）は削除しません</strong>。
      </p>
      <div className="row">
        <label className="small">keep last
          <input type="number" min={1} value={keepLast} onChange={(e) => setKeepLast(Number(e.target.value))} style={{ width: 70, marginLeft: 6 }} />
        </label>
        <button disabled={busy === "cleanup"} onClick={() => runCleanup(true)}>
          {busy === "cleanup" ? <span className="spin" /> : "🔍"} Dry-run
        </button>
        <button className="danger" disabled={busy === "cleanup"} onClick={() => runCleanup(false)}>Apply</button>
      </div>
      {cleanup && (
        <div className="flash">
          {cleanup.dry_run ? "Dry-run: would delete" : "Deleted"} {cleanup.dry_run ? cleanup.matched : cleanup.deleted} of {cleanup.total} sessions
          (kept {cleanup.kept}, jobs preserved {cleanup.jobs_preserved}). Jobs & imported data are NOT deleted.
        </div>
      )}
    </div>
  );
}
