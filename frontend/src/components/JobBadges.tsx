import type { Job } from "../api/types";
import { StatusBadge } from "./ui";

/** Status badge plus derived hints (429 / partial / warnings) from the backend
 * classification (falls back to status/meta when classification is absent). */
export function JobBadges({ job, showWarnings = false }: { job: Job; showWarnings?: boolean }) {
  const c = job.classification;
  const reasons = c?.reasons ?? [];
  const rateLimited = c?.rate_limited ?? Boolean((job.meta as { rate_limited?: boolean } | null)?.rate_limited);
  const partial = c?.partial ?? job.status === "partial_success";
  return (
    <span className="row" style={{ display: "inline-flex", gap: 6 }}>
      <StatusBadge status={job.status} />
      {rateLimited && <span className="badge warn" title="HTTP 429 rate limit">429</span>}
      {reasons.includes("incomplete_data") && (
        <span className="badge warn" title="Incomplete data received (YouTube throttling)">throttled</span>
      )}
      {reasons.includes("fragments_failed") && <span className="badge warn" title="Fragment download failed">frags</span>}
      {partial && job.status !== "partial_success" && <span className="badge warn">partial</span>}
      {showWarnings &&
        c?.warnings.map((w, i) => (
          <span key={i} className="warn-chip" title={w}>
            ⚠ {w.length > 40 ? w.slice(0, 40) + "…" : w}
          </span>
        ))}
    </span>
  );
}

/** Inline explanation box shown on the job detail page. */
export function JobClassificationNote({ job }: { job: Job }) {
  const c = job.classification;
  if (!c) return null;
  if (!c.rate_limited && !c.partial && c.warnings.length === 0 && c.reasons.length === 0) return null;
  return (
    <div className="flash" style={{ borderColor: "var(--warn)", background: "rgba(210,153,34,.10)" }}>
      <strong>{c.summary ?? "Notes"}</strong>
      {c.reasons.length > 0 && (
        <div style={{ marginTop: 4 }}>
          {c.reasons.map((r) => (
            <span key={r} className="badge muted" style={{ marginRight: 4 }}>
              {r}
            </span>
          ))}
          {c.retryable && <span className="badge run">retryable</span>}
        </div>
      )}
      {c.rate_limited && (
        <div className="small" style={{ marginTop: 4 }}>
          YouTube が一時的にレート制限（HTTP 429）を返しました。多くは字幕取得時の一時的な制限です。少し時間をおいて
          <strong> Retry </strong>してください。
        </div>
      )}
      {c.partial && (
        <div className="small" style={{ marginTop: 4 }}>
          一部の出力は保存されましたが、yt-dlp が非ゼロ終了しました（partial_success）。本体/メタデータは利用できる場合があります。
        </div>
      )}
      {c.warnings.length > 0 && (
        <div style={{ marginTop: 6 }}>
          {c.warnings.map((w, i) => (
            <span key={i} className="warn-chip">⚠ {w}</span>
          ))}
        </div>
      )}
    </div>
  );
}
