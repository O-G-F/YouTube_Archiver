import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { ErrorBox, Loading } from "../components/ui";
import { JobBadges } from "../components/JobBadges";
import type { Job } from "../api/types";

const STATUSES = ["", "queued", "running", "success", "partial_success", "failed", "canceled"];
const TYPES = [
  "", "download", "expand", "metadata_refresh", "comments_refresh", "live_chat_refresh", "subtitles_refresh",
  "youtube_diagnostic",
];
const REASONS = [
  "", "rate_limited", "incomplete_data", "fragments_failed", "subtitles_failed", "comments_failed", "impersonation",
];

export default function Jobs() {
  const [status, setStatus] = useState("");
  const [type, setType] = useState("");
  const [retryableOnly, setRetryableOnly] = useState(false);
  const [reason, setReason] = useState("");
  const [auto, setAuto] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const { data, error, loading, reload } = useFetch<Job[]>(
    () =>
      retryableOnly
        ? api.retryableJobs({ reason: reason || undefined, type: type || undefined, limit: 100 })
        : api.jobs({ status: status || undefined, type: type || undefined, limit: 100 }),
    [status, type, retryableOnly, reason],
    auto ? 6000 : undefined
  );

  async function retry(id: number) {
    setBusyId(id);
    setActionErr(null);
    try {
      await api.retryJob(id);
      reload();
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function retryAll() {
    setBusyId(-1);
    setActionErr(null);
    setFlash(null);
    try {
      const r = await api.retryAll({ reason: reason || undefined, type: type || undefined, limit: 100 });
      setFlash(`Re-queued ${r.retried} retryable job(s).`);
      reload();
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div>
      <h1 className="page-title">Jobs</h1>
      <p className="page-sub">All download / expand / refresh jobs (most recent first).</p>

      <div className="toolbar">
        <div className="field inline">
          <label>Status</label>
          <select value={status} disabled={retryableOnly} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s || "all"}
              </option>
            ))}
          </select>
        </div>
        <div className="field inline">
          <label>Type</label>
          <select value={type} onChange={(e) => setType(e.target.value)}>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t || "all"}
              </option>
            ))}
          </select>
        </div>
        <label className="checkbox" style={{ alignSelf: "center" }}>
          <input type="checkbox" checked={retryableOnly} onChange={(e) => setRetryableOnly(e.target.checked)} />
          retryable only
        </label>
        <div className="field inline">
          <label>Reason</label>
          <select value={reason} onChange={(e) => setReason(e.target.value)}>
            {REASONS.map((r) => (
              <option key={r} value={r}>
                {r || "any"}
              </option>
            ))}
          </select>
        </div>
        {retryableOnly && (
          <button disabled={busyId === -1} onClick={retryAll}>
            {busyId === -1 ? <span className="spin" /> : "↻"} Retry all
          </button>
        )}
        <label className="checkbox" style={{ alignSelf: "center" }}>
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
          auto (6s)
        </label>
        <button className="right" onClick={reload}>
          ↻ Refresh {loading && <span className="spin" />}
        </button>
      </div>

      {flash && <div className="flash">{flash}</div>}
      <ErrorBox error={error} />
      <ErrorBox error={actionErr} />

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Status</th>
              <th>Type</th>
              <th>Profile</th>
              <th className="wrap">URL</th>
              <th>Created</th>
              <th>Finished</th>
              <th className="wrap">Error</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data?.map((j) => (
              <tr key={j.id}>
                <td>
                  <Link to={`/jobs/${j.id}`}>#{j.id}</Link>
                </td>
                <td>
                  <JobBadges job={j} />
                </td>
                <td>{j.type}</td>
                <td className="muted">{j.profile_name ?? "—"}</td>
                <td className="wrap mono small">{j.url ?? "—"}</td>
                <td className="muted small">{fmtDate(j.created_at)}</td>
                <td className="muted small">{fmtDate(j.finished_at)}</td>
                <td className="wrap small" style={{ color: "var(--err)" }}>
                  {j.error_message ? j.error_message.split("\n")[0] : ""}
                </td>
                <td>
                  {["failed", "canceled", "partial_success"].includes(j.status) && (
                    <button className="sm" disabled={busyId === j.id} onClick={() => retry(j.id)}>
                      {busyId === j.id ? <span className="spin" /> : "Retry"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {data && data.length === 0 && (
              <tr>
                <td colSpan={9} className="empty">
                  No jobs match the current filter.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
