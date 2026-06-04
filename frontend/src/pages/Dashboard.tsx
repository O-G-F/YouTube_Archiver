import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { Card, ErrorBox, JsonBlock, Loading, StatusBadge } from "../components/ui";
import type { Doctor, SchedulerRunOnceResult } from "../api/types";

export default function Dashboard() {
  const { data, error, loading, reload } = useFetch(() => api.dashboard(), []);
  const [doctor, setDoctor] = useState<Doctor | null>(null);
  const [doctorErr, setDoctorErr] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<SchedulerRunOnceResult | null>(null);
  const [busy, setBusy] = useState(false);

  async function runDoctor() {
    setDoctorErr(null);
    try {
      setDoctor(await api.doctor());
    } catch (e) {
      setDoctorErr((e as Error).message);
    }
  }

  async function runScheduler(parts: { collections: boolean; comments: boolean }) {
    setBusy(true);
    setRunResult(null);
    try {
      setRunResult(await api.schedulerRunOnce(parts));
      reload();
    } catch (e) {
      setDoctorErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (loading && !data) return <Loading what="dashboard" />;

  return (
    <div>
      <div className="spread">
        <div>
          <h1 className="page-title">Dashboard</h1>
          <p className="page-sub">System health and archive overview.</p>
        </div>
        <button onClick={reload}>↻ Refresh</button>
      </div>
      <ErrorBox error={error} />

      {data && (
        <>
          <div className="cards">
            <Card
              label="API"
              sm
              value={<span className={`badge ${data.health.status === "ok" ? "ok" : "warn"}`}>{data.health.status}</span>}
            />
            <Card label="Database" sm value={<span className={`badge ${data.health.database ? "ok" : "err"}`}>{data.health.database ? "up" : "down"}</span>} />
            <Card label="Redis" sm value={<span className={`badge ${data.health.redis ? "ok" : "err"}`}>{data.health.redis ? "up" : "down"}</span>} />
            <Card label="yt-dlp" sm value={<span className="mono small">{data.health.ytdlp_version ?? "—"}</span>} />
            <Card label="Videos" value={data.counts.videos} />
            <Card label="Collections" value={data.counts.collections} />
            <Card label="Comments" value={data.counts.comments} />
            <Card label="Comments due" value={data.counts.comments_due} />
            <Card label="Live chat msgs" value={data.counts.live_chat_messages} />
            <Card label="Live chat due" value={data.counts.live_chat_due} />
            <Card label="Watch history" value={data.counts.watch_history} />
            <Card label="Snapshots" value={data.counts.metadata_snapshots} />
          </div>

          <div className="grid2">
            <div className="panel">
              <h2>Jobs by status</h2>
              <div className="row gap-lg">
                {Object.entries(data.job_stats.by_status).length === 0 && (
                  <span className="muted">No jobs yet.</span>
                )}
                {Object.entries(data.job_stats.by_status).map(([s, n]) => (
                  <div key={s}>
                    <StatusBadge status={s} /> <strong>{n}</strong>
                  </div>
                ))}
              </div>
              <h3>By type</h3>
              <div className="row gap-lg">
                {Object.entries(data.job_stats.by_type).map(([t, n]) => (
                  <span key={t} className="muted">
                    {t}: <strong style={{ color: "var(--text)" }}>{n}</strong>
                  </span>
                ))}
              </div>
            </div>

            <div className="panel">
              <h2>Scheduler</h2>
              <div className="row gap-lg">
                <span>
                  collections:{" "}
                  <span className={`badge ${data.scheduler.enabled ? "ok" : "muted"}`}>
                    {data.scheduler.enabled ? "enabled" : "off"}
                  </span>
                </span>
                <span>
                  comments:{" "}
                  <span className={`badge ${data.scheduler.comments_enabled ? "ok" : "muted"}`}>
                    {data.scheduler.comments_enabled ? "enabled" : "off"}
                  </span>
                </span>
                <span className="muted">interval {data.scheduler.interval_seconds}s</span>
              </div>
              <div className="row gap-lg" style={{ marginTop: 8 }}>
                <span className="muted">crawlable: {data.scheduler.crawlable_collections}</span>
                <span className="muted">due comments: {data.scheduler.due_comment_videos}</span>
                <span className="muted">frozen: {data.scheduler.frozen_comment_videos}</span>
              </div>
              <div className="row" style={{ marginTop: 12 }}>
                <button disabled={busy} onClick={() => runScheduler({ collections: true, comments: true })}>
                  {busy ? <span className="spin" /> : "▶"} Run-once (all)
                </button>
                <button disabled={busy} onClick={() => runScheduler({ collections: false, comments: true })}>
                  Comments only
                </button>
                <button disabled={busy} onClick={() => runScheduler({ collections: true, comments: false })}>
                  Collections only
                </button>
              </div>
              {runResult && (
                <div className="flash" style={{ marginTop: 10 }}>
                  Created {runResult.jobs_created} job(s) — comments:{" "}
                  {runResult.comments_jobs_created}, collections: {runResult.collection_jobs_created}, submitted{" "}
                  {runResult.submitted}. {runResult.job_ids.length > 0 && `ids: ${runResult.job_ids.join(", ")}`}
                </div>
              )}
            </div>
          </div>

          <div className="panel">
            <div className="spread">
              <h2>Latest jobs</h2>
              <Link to="/jobs">all jobs →</Link>
            </div>
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
                  </tr>
                </thead>
                <tbody>
                  {data.latest_jobs.map((j) => (
                    <tr key={j.id}>
                      <td>
                        <Link to={`/jobs/${j.id}`}>#{j.id}</Link>
                      </td>
                      <td>
                        <StatusBadge status={j.status} />
                      </td>
                      <td>{j.type}</td>
                      <td className="muted">{j.profile_name ?? "—"}</td>
                      <td className="wrap mono small">{j.url ?? "—"}</td>
                      <td className="muted small">{fmtDate(j.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="panel">
            <div className="spread">
              <h2>Doctor</h2>
              <button onClick={runDoctor}>Run diagnostics</button>
            </div>
            <ErrorBox error={doctorErr} />
            {doctor ? (
              <div className="table-wrap" style={{ marginTop: 10 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Check</th>
                      <th>Result</th>
                      <th className="wrap">Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {doctor.checks.map((c) => (
                      <tr key={c.name}>
                        <td>{c.name}</td>
                        <td>
                          <span className={`badge ${c.ok ? "ok" : "err"}`}>{c.ok ? "ok" : "fail"}</span>
                        </td>
                        <td className="wrap mono small">{c.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted small">Click “Run diagnostics” to check write access, tools, DB and Redis.</p>
            )}
          </div>

          <details className="panel">
            <summary className="muted">Raw dashboard JSON</summary>
            <JsonBlock value={data} />
          </details>
        </>
      )}
    </div>
  );
}
