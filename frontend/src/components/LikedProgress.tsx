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

const PERMANENT_REASONS = new Set(["private", "deleted", "unavailable"]);

export function LikedProgressDashboard({ onChanged }: { onChanged?: () => void }) {
  const progress = useFetch(() => api.likedProgress(), []);
  const queue = useFetch(() => api.queueStatus(), []);
  const failures = useFetch(() => api.likedFailureBreakdown(), []);
  const secrets = useFetch(() => api.secretsStatus(), []);
  const [tab, setTab] = useState<"now" | "history">("now");
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirmArchive, setConfirmArchive] = useState(false);

  function reloadAll() {
    progress.reload();
    queue.reload();
    failures.reload();
    secrets.reload();
    onChanged?.();
  }

  async function retryRetryable() {
    setBusy("retry-metadata");
    setErr(null);
    setFlash(null);
    try {
      const r = await api.likedRetryFailed({ reason: "rate_limited" });
      setFlash(`retry: re-queued ${r.retried} rate_limited metadata/archive job(s) (permanent excluded)`);
      reloadAll();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
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
            <div className="card"><div className="label">Metadata (broad)</div><div className="value sm">{pct(p.metadata_fetched, p.total_liked)} <span className="muted">({p.metadata_fetched}/{p.total_liked})</span></div></div>
            <div className="card" title="Videos with a real info_json — use this for full-metadata decisions, not the broad count"><div className="label">info_json complete</div><div className="value sm">{p.info_json_complete_count ?? "—"}<span className="muted"> / {p.metadata_fetched} broad</span></div></div>
            <div className="card" title="Has .description but no info_json; retryable partials are upgradeable via retry-metadata"><div className="label">desc-only (retryable)</div><div className="value sm">{p.description_only_count ?? "—"}<span className="muted"> ({p.retryable_partial_count ?? 0} retry)</span></div></div>
            <div className="card"><div className="label">Eligible missing</div><div className="value sm">{p.eligible_metadata_missing}<span className="muted"> / {p.metadata_missing} missing</span></div></div>
            <div className="card"><div className="label">Permanent (kept)</div><div className="value sm"><span className={`badge ${p.permanent_unique_videos ? "err" : "muted"}`}>{p.permanent_unique_videos}</span></div></div>
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

          {secrets.data && (
            <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 6 }}>
              <span className="muted small">fetch auth:</span>
              <span className={`badge ${secrets.data.cookies_configured ? "ok" : "warn"}`}
                title={secrets.data.cookies_file_configured && !secrets.data.cookies_file_readable ? "cookies file not readable" : ""}>
                cookies {secrets.data.cookies_configured ? (secrets.data.cookies_file_readable || secrets.data.cookies_from_browser_configured ? "readable ✓" : "set") : "off"}
              </span>
              <span className={`badge ${secrets.data.po_token_configured ? "ok" : "warn"}`}>
                PO-token {secrets.data.po_token_configured ? "set ✓" : "off"}
              </span>
              {!secrets.data.cookies_configured && (
                <span className="muted small">未設定だと 429 が増えます（COOKIES_FILE / YOUTUBE_PO_TOKEN を .env に設定）。実値は表示しません。</span>
              )}
            </div>
          )}

          {failures.data && (failures.data.total_failed > 0 || failures.data.total_partial > 0) && (
            <div className="row" style={{ flexWrap: "wrap", gap: 8, marginTop: 6 }}>
              <span className="muted small">failures by reason (unique videos / attempts):</span>
              {Object.entries(failures.data.unique_videos_by_reason).map(([reason, n]) => (
                <span key={reason} className={`badge ${PERMANENT_REASONS.has(reason) ? "err" : "warn"}`}
                  title={PERMANENT_REASONS.has(reason) ? "permanent — kept, NOT retried, excluded from metadata selection" : "retryable / transient"}>
                  {reason}: {n}/{failures.data.attempts_by_reason[reason] ?? n}
                </span>
              ))}
              <span className="muted small">
                (permanent unique {failures.data.permanent_unique_videos} は再試行せず保持・選定から除外 / retryable は再投入可。動画は削除しません)
              </span>
            </div>
          )}

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
            <button disabled={busy === "retry-metadata"} onClick={retryRetryable}
              title="Re-queue rate_limited jobs only — permanent (private/deleted/unavailable) are never retried">
              {busy === "retry-metadata" ? <span className="spin" /> : "↻"} Retry rate_limited
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
