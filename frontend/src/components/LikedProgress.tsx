import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "./ui";
import { SchedulerHistory } from "./SchedulerHistory";
import type { SchedulerRunOnceResult } from "../api/types";

function pct(n: number, d: number): string {
  if (!d) return "0%";
  return `${Math.round((n / d) * 100)}%`;
}

export function LikedProgressDashboard({ onChanged }: { onChanged?: () => void }) {
  const progress = useFetch(() => api.likedProgress(), []);
  const queue = useFetch(() => api.queueStatus(), []);
  const [tab, setTab] = useState<"now" | "history">("now");
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirmArchive, setConfirmArchive] = useState(false);

  function reloadAll() {
    progress.reload();
    queue.reload();
    onChanged?.();
  }

  async function runPass(kind: "metadata" | "archive" | "retry") {
    setBusy(kind);
    setErr(null);
    setFlash(null);
    try {
      const body =
        kind === "metadata"
          ? { liked_metadata: true }
          : kind === "archive"
          ? { liked_archive: true }
          : { liked_retry: true };
      const r: SchedulerRunOnceResult = await api.schedulerRunOnce(body);
      const msg =
        kind === "metadata"
          ? `metadata pass: ${r.liked_metadata_jobs_created ?? 0} job(s) (no body)`
          : kind === "archive"
          ? `archive pass: ${r.liked_archive_jobs_created ?? 0} body job(s), skipped_active=${r.skipped_active_jobs ?? 0}`
          : `retry pass: ${r.liked_retry_jobs_requeued ?? 0} re-queued`;
      setFlash(msg);
      setConfirmArchive(false);
      reloadAll();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const p = progress.data;
  const q = queue.data;
  return (
    <div className="panel">
      <div className="spread">
        <h2>Liked archive progress</h2>
        <div className="tabs">
          <button className={tab === "now" ? "active" : ""} onClick={() => setTab("now")}>Now</button>
          <button className={tab === "history" ? "active" : ""} onClick={() => setTab("history")}>History</button>
          <button className="right" onClick={reloadAll}>↻</button>
        </div>
      </div>
      {tab === "history" && <SchedulerHistory />}
      {tab === "now" && <ErrorBox error={progress.error} />}
      {tab === "now" && (progress.loading && !p ? (
        <Loading />
      ) : p ? (
        <>
          <div className="cards">
            <div className="card"><div className="label">Liked total</div><div className="value">{p.total_liked}</div></div>
            <div className="card"><div className="label">Metadata</div><div className="value sm">{pct(p.metadata_fetched, p.total_liked)} <span className="muted">({p.metadata_fetched}/{p.total_liked})</span></div></div>
            <div className="card"><div className="label">Body saved</div><div className="value sm">{pct(p.body_saved, p.total_liked)} <span className="muted">({p.body_saved}/{p.total_liked})</span></div></div>
            <div className="card"><div className="label">Retryable</div><div className="value sm"><span className={`badge ${p.retryable_liked_jobs ? "warn" : "muted"}`}>{p.retryable_liked_jobs}</span></div></div>
            <div className="card"><div className="label">Active archive</div><div className="value sm">{p.active_archive_jobs}</div></div>
            <div className="card"><div className="label">Failed / partial</div><div className="value sm">{p.failed_liked_jobs} / {p.partial_liked_jobs}</div></div>
          </div>

          <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 4 }}>
            <span className="muted small">by source:</span>
            {Object.entries(p.by_source).map(([s, n]) => (
              <span key={s} className="badge muted">{s}: {n}</span>
            ))}
          </div>

          {q && (
            <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 6 }}>
              <span className="muted small">queue:</span>
              <span className="badge muted">queued {q.queued}</span>
              <span className="badge muted">running {q.running}</span>
              {Object.entries(q.by_source_action).map(([s, n]) => (
                <span key={s} className="badge muted">{s}: {n}</span>
              ))}
              {q.worker_count != null && <span className="badge muted">workers {q.worker_count}</span>}
            </div>
          )}

          <h3>Run a scheduler pass once</h3>
          <p className="muted small">
            手動で1回だけ実行（既定では scheduler は OFF）。metadata は本体非保存。archive は<strong>本体DL</strong>のため少量・確認付き。
          </p>
          <div className="row">
            <button disabled={busy === "metadata"} onClick={() => runPass("metadata")}>
              {busy === "metadata" ? <span className="spin" /> : "⤓"} Run metadata pass
            </button>
            {confirmArchive ? (
              <>
                <span className="badge err">本体DLが発生します</span>
                <button className="danger" disabled={busy === "archive"} onClick={() => runPass("archive")}>
                  {busy === "archive" ? <span className="spin" /> : "⬇"} Confirm archive pass
                </button>
                <button onClick={() => setConfirmArchive(false)}>Cancel</button>
              </>
            ) : (
              <button onClick={() => setConfirmArchive(true)}>⬇ Run archive pass…</button>
            )}
            <button disabled={busy === "retry"} onClick={() => runPass("retry")}>
              {busy === "retry" ? <span className="spin" /> : "↻"} Run retry pass
            </button>
            <Link className="btn-link" to="/jobs?source_action=liked_archive">View liked jobs →</Link>
          </div>
          {flash && <div className="flash">{flash}</div>}
          <ErrorBox error={err} />
        </>
      ) : null)}
    </div>
  );
}
