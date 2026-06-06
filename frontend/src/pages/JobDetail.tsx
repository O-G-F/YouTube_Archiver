import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { ErrorBox, JsonBlock, KV, Loading, StatusBadge } from "../components/ui";
import { JobClassificationNote } from "../components/JobBadges";

type Tab = "command" | "stdout" | "stderr";

export default function JobDetail() {
  const { id } = useParams();
  const jobId = Number(id);
  const { data: job, error, loading, reload } = useFetch(() => api.job(jobId), [jobId]);
  const logs = useFetch(() => api.jobLogs(jobId, 2000), [jobId]);
  const [tab, setTab] = useState<Tab>("command");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  async function retry() {
    setBusy("retry");
    setActionErr(null);
    try {
      await api.retryJob(jobId);
      reload();
      logs.reload();
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function retrySubtitles() {
    if (!job?.video) return;
    setBusy("subs");
    setActionErr(null);
    setFlash(null);
    try {
      const j = await api.refreshVideoSubtitles(job.video.id);
      setFlash(`Created subtitles_refresh job #${j.id} (body is not re-downloaded).`);
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (loading && !job) return <Loading what={`job #${jobId}`} />;
  if (error) return <ErrorBox error={error} />;
  if (!job) return null;

  const logText = logs.data ? logs.data[tab] : null;
  const reasons = job.classification?.reasons ?? [];
  const canRetry = ["failed", "canceled", "partial_success"].includes(job.status);

  // youtube_diagnostic report (lives in job.meta) — Phase 7B
  const meta = (job.meta ?? {}) as Record<string, unknown>;
  const diag = meta.diagnostic as
    | { overall?: string; steps?: Array<Record<string, unknown>>; reasons?: string[] }
    | undefined;
  const recommendations = (meta.recommendations as string[] | undefined) ?? [];
  const isLikedArchive = meta.source_action === "liked_archive";
  const schedulerRunId = meta.scheduler_run_id as string | undefined;

  return (
    <div>
      <div className="spread">
        <h1 className="page-title">
          Job #{job.id} <StatusBadge status={job.status} />
        </h1>
        <div className="row">
          {canRetry && (
            <button className="primary" disabled={busy === "retry"} onClick={retry}>
              {busy === "retry" ? <span className="spin" /> : "↻"} Retry
            </button>
          )}
          {reasons.includes("subtitles_failed") && job.video && (
            <button disabled={busy === "subs"} onClick={retrySubtitles}>
              {busy === "subs" ? <span className="spin" /> : "📝"} Retry subtitles only
            </button>
          )}
          <button onClick={() => { reload(); logs.reload(); }}>↻ Refresh</button>
        </div>
      </div>
      <p className="page-sub">
        <Link to="/jobs">← back to jobs</Link>
      </p>
      <ErrorBox error={actionErr} />
      {flash && <div className="flash">{flash}</div>}
      <JobClassificationNote job={job} />

      {isLikedArchive && (
        <div className="flash">
          ❤ From <Link to="/liked-videos">Liked videos</Link> — requested profile{" "}
          <code>{String(meta.requested_profile ?? job.profile_name ?? "")}</code>
          {meta.requested_profile && meta.requested_profile !== "metadata_only"
            ? " (downloads the video body)"
            : " (metadata only, no body)"}
          {schedulerRunId && (
            <>
              {" "}· scheduler run{" "}
              <Link to={`/jobs?scheduler_run_id=${schedulerRunId}`}>
                <code>{schedulerRunId.slice(0, 10)}</code>
              </Link>
            </>
          )}
          .
        </div>
      )}

      {job.type === "youtube_diagnostic" && (diag || recommendations.length > 0) && (
        <div className="panel">
          <div className="spread">
            <h2>YouTube diagnostic</h2>
            {diag?.overall && (
              <span className={`badge ${diag.overall === "success" ? "ok" : diag.overall === "failed" ? "err" : "warn"}`}>
                {diag.overall}
              </span>
            )}
          </div>
          {diag?.steps && diag.steps.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Step</th><th>Status</th><th>Duration</th><th>Body created</th><th className="wrap">Reasons</th></tr>
                </thead>
                <tbody>
                  {diag.steps.map((s, i) => {
                    const st = String(s.status ?? "");
                    const cls = (s.classification ?? {}) as { reasons?: string[] };
                    return (
                      <tr key={i}>
                        <td className="mono small">{String(s.name ?? "")}</td>
                        <td><span className={`badge ${st === "success" ? "ok" : st === "failed" ? "err" : "warn"}`}>{st}</span></td>
                        <td className="small">{String(s.duration_seconds ?? "—")}s</td>
                        <td>
                          <span className={`badge ${s.media_body_created ? "warn" : "muted"}`}>
                            {s.media_body_created ? "yes (temp, discarded)" : "no"}
                          </span>
                        </td>
                        <td className="wrap small muted">{(cls.reasons ?? []).join(", ") || "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {recommendations.length > 0 && (
            <>
              <h3>Recommendations</h3>
              <ul className="muted small" style={{ lineHeight: 1.7 }}>
                {recommendations.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </>
          )}
          <p className="muted small">
            診断は「完全解決の保証」ではなく設定と傾向を確認する道具です。DB の media body は増えません（video テストも一時DL→削除）。
          </p>
        </div>
      )}

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
              ["Retry count", String(job.retry_count ?? 0)],
              ["Next retry", fmtDate(job.next_retry_at)],
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
