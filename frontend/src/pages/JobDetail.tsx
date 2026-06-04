import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { ErrorBox, JsonBlock, KV, Loading, StatusBadge } from "../components/ui";

type Tab = "command" | "stdout" | "stderr";

export default function JobDetail() {
  const { id } = useParams();
  const jobId = Number(id);
  const { data: job, error, loading, reload } = useFetch(() => api.job(jobId), [jobId]);
  const logs = useFetch(() => api.jobLogs(jobId, 2000), [jobId]);
  const [tab, setTab] = useState<Tab>("command");
  const [busy, setBusy] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);

  async function retry() {
    setBusy(true);
    setActionErr(null);
    try {
      await api.retryJob(jobId);
      reload();
      logs.reload();
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (loading && !job) return <Loading what={`job #${jobId}`} />;
  if (error) return <ErrorBox error={error} />;
  if (!job) return null;

  const logText = logs.data ? logs.data[tab] : null;

  return (
    <div>
      <div className="spread">
        <h1 className="page-title">
          Job #{job.id} <StatusBadge status={job.status} />
        </h1>
        <div className="row">
          {["failed", "canceled", "partial_success"].includes(job.status) && (
            <button className="primary" disabled={busy} onClick={retry}>
              {busy ? <span className="spin" /> : "↻"} Retry
            </button>
          )}
          <button onClick={() => { reload(); logs.reload(); }}>↻ Refresh</button>
        </div>
      </div>
      <p className="page-sub">
        <Link to="/jobs">← back to jobs</Link>
      </p>
      <ErrorBox error={actionErr} />

      <div className="grid2">
        <div className="panel">
          <h2>Info</h2>
          <KV
            rows={[
              ["Type", job.type],
              ["Status", <StatusBadge status={job.status} />],
              ["Profile", job.profile ? `${job.profile.name} (${job.profile.media_mode})` : job.profile_name ?? "—"],
              ["URL", <span className="mono small">{job.url ?? "—"}</span>],
              ["Priority", String(job.priority)],
              ["Progress", `${job.progress}%`],
              ["RQ job id", <span className="mono small">{job.rq_job_id ?? "—"}</span>],
              ["Created", fmtDate(job.created_at)],
              ["Started", fmtDate(job.started_at)],
              ["Finished", fmtDate(job.finished_at)],
              ["Output dir", <span className="mono small">{job.output_dir ?? "—"}</span>],
            ]}
          />
          <h3>Related</h3>
          <div className="row">
            {job.video ? (
              <Link to={`/videos/${job.video.id}`}>
                video: {job.video.title ?? job.video.youtube_video_id}
              </Link>
            ) : (
              <span className="muted">no related video</span>
            )}
            {job.collection_id && <Link to={`/collections/${job.collection_id}`}>collection #{job.collection_id}</Link>}
            {job.parent_job_id && <Link to={`/jobs/${job.parent_job_id}`}>parent job #{job.parent_job_id}</Link>}
          </div>
          {job.error_message && (
            <>
              <h3>Error message</h3>
              <pre className="log">{job.error_message}</pre>
            </>
          )}
        </div>

        <div className="panel">
          <h2>job.meta</h2>
          <JsonBlock value={job.meta} />
        </div>
      </div>

      <div className="panel">
        <h2>Logs (tail, secrets redacted)</h2>
        <div className="tabs">
          {(["command", "stdout", "stderr"] as Tab[]).map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
          <button className="right" onClick={() => logs.reload()}>
            ↻
          </button>
        </div>
        <ErrorBox error={logs.error} />
        {logs.loading ? (
          <Loading what="logs" />
        ) : logText ? (
          <pre className="log">{logText}</pre>
        ) : (
          <p className="muted small">
            No {tab} log available {logs.data && !logs.data.available ? "(log directory missing)" : ""}.
          </p>
        )}
      </div>
    </div>
  );
}
