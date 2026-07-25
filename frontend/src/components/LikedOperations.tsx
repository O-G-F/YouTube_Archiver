import { useState } from "react";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "./ui";
import type { ProductionCheck } from "../api/types";

function gib(n: number | null | undefined): string {
  return n == null ? "—" : `${n} GiB`;
}

function mib(n: number | null | undefined): string {
  return n == null ? "—" : `${n} MiB`;
}

/**
 * Phase 9A: read-only body-archive operations panel. Surfaces disk headroom,
 * the production body profile, queue/orphan/duplicate health, and DB-growth
 * signals (comments table, raw_json) so an operator can decide whether it's safe
 * to run another batch. Intentionally has NO run button — enqueue stays a
 * deliberate plan/dry-run → CLI/enqueue step (no full-archive shortcut here).
 */
const STATUS_BADGE: Record<string, string> = { pass: "ok", warn: "warn", fail: "err" };

export function LikedOperationsPanel() {
  const ops = useFetch(() => api.likedOperations(), []);
  const prod = useFetch(() => api.productionCheck(), []);
  const o = ops.data;
  const pr = prod.data;
  // Phase 9D: release-check is heavier (scans archive files) — run on demand.
  const [rel, setRel] = useState<ProductionCheck | null>(null);
  const [relBusy, setRelBusy] = useState(false);

  const diskLow =
    o != null && o.disk.readable && o.disk.free_gb != null && o.disk.free_gb < o.min_free_gb;

  async function runReleaseCheck() {
    setRelBusy(true);
    try {
      setRel(await api.releaseCheck());
    } catch {
      setRel(null);
    } finally {
      setRelBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="spread">
        <h2>Body archive operations (Phase 9A / 9B / 9D)</h2>
        <button onClick={() => { ops.reload(); prod.reload(); }}>↻ {(ops.loading || prod.loading) && <span className="spin" />}</button>
      </div>

      {/* Phase 9B/9C/9D: production + ingress readiness (read-only, PASS/WARN/FAIL) */}
      {pr && (
        <div className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 8 }}>
          <span className="muted small">production readiness:</span>
          <span className={`badge ${STATUS_BADGE[pr.overall] ?? "muted"}`}>{pr.overall.toUpperCase()}</span>
          <span className="muted small">
            pass {pr.counts.pass ?? 0} / warn {pr.counts.warn ?? 0} / fail {pr.counts.fail ?? 0}
          </span>
          <span className="badge muted" title="APP_ENV">{pr.app_env || "—"}</span>
          <span className={`badge ${pr.auth_mode === "disabled" ? "warn" : "ok"}`} title="AUTH_MODE">
            auth: {pr.auth_mode || "—"}
          </span>
          {pr.checks.filter((c) => c.status !== "pass").map((c) => (
            <span key={c.name} className={`badge ${STATUS_BADGE[c.status]}`} title={c.detail}>
              {c.name}: {c.status}
            </span>
          ))}
          <span className="muted small" style={{ flexBasis: "100%" }} title={pr.backup_reminder}>
            💾 backup before cutover: Postgres + Redis AOF volume + archive dir + secrets（値は表示しません）
          </span>
          <div className="row" style={{ flexBasis: "100%", gap: 8, alignItems: "center", marginTop: 2 }}>
            <button onClick={runReleaseCheck} disabled={relBusy}>
              {relBusy ? <span className="spin" /> : "▶"} Run release-check
            </button>
            {rel && (
              <>
                <span className={`badge ${STATUS_BADGE[rel.overall] ?? "muted"}`}>release: {rel.overall.toUpperCase()}</span>
                <span className="muted small">
                  pass {rel.counts.pass ?? 0} / warn {rel.counts.warn ?? 0} / fail {rel.counts.fail ?? 0}
                </span>
                {rel.checks.filter((c) => c.status !== "pass").slice(0, 8).map((c) => (
                  <span key={c.name} className={`badge ${STATUS_BADGE[c.status]}`} title={c.detail}>
                    {c.name}: {c.status}
                  </span>
                ))}
              </>
            )}
          </div>
        </div>
      )}
      <ErrorBox error={prod.error} />
      <ErrorBox error={ops.error} />
      {ops.loading && !o ? (
        <Loading />
      ) : o ? (
        <>
          <div className="cards">
            <div className="card">
              <div className="label">body saved</div>
              <div className="value">{o.body_saved}</div>
            </div>
            <div className="card">
              <div className="label">remaining eligible</div>
              <div className="value sm">
                {o.remaining_eligible_body}
                <span className="muted"> (perm {o.permanent_unique_videos} kept)</span>
              </div>
            </div>
            <div className="card">
              <div className="label">active / queued / running</div>
              <div className="value sm">
                {o.total_active_jobs} / {o.queued_jobs} / {o.running_jobs}
              </div>
            </div>
            <div className="card">
              <div className="label">body profile</div>
              <div className="value sm" title="Production default (comments-light) — avoids DB comments bloat">
                {o.default_body_profile}
              </div>
            </div>
            <div className="card">
              <div className="label">disk free</div>
              <div className="value sm">
                <span className={`badge ${o.disk.readable ? (diskLow ? "err" : "ok") : "warn"}`}>
                  {o.disk.readable ? gib(o.disk.free_gb) : "unreadable"}
                </span>
                <span className="muted"> / {gib(o.disk.total_gb)}</span>
              </div>
            </div>
            <div className="card">
              <div className="label">min-free guard</div>
              <div className="value sm">
                <span className={`badge ${diskLow ? "err" : "muted"}`}>{gib(o.min_free_gb)}</span>
                {diskLow && <span className="muted"> below — enqueue blocked</span>}
              </div>
            </div>
            <div className="card" title="Conservative p90 of saved videos; actual varies with length">
              <div className="label">est size/video</div>
              <div className="value sm">
                {mib(o.size_estimate.estimate_mb)}
                <span className="muted"> ({o.size_estimate.source})</span>
              </div>
            </div>
            <div className="card">
              <div className="label">orphan jobs</div>
              <div className="value sm">
                <span className={`badge ${o.orphan.orphan_found ? "warn" : "muted"}`}>
                  {o.orphan.orphan_found}
                </span>
                {o.orphan.rq_unreadable && <span className="muted"> (RQ n/a)</span>}
              </div>
            </div>
            <div className="card">
              <div className="label">duplicate media</div>
              <div className="value sm">
                <span className={`badge ${o.duplicate_video_media_files ? "err" : "muted"}`}>
                  {o.duplicate_video_media_files}
                </span>
              </div>
            </div>
            <div className="card" title="Comments-light profile keeps this from growing">
              <div className="label">comments table</div>
              <div className="value sm">{(o.comments_table_bytes / 1048576).toFixed(1)} MiB</div>
            </div>
            <div className="card" title="Must stay 0 — no raw personal JSON stored">
              <div className="label">raw_json stored</div>
              <div className="value sm">
                <span className={`badge ${o.raw_json_stored_total ? "err" : "ok"}`}>
                  {o.raw_json_stored_total}
                </span>
              </div>
            </div>
            <div className="card">
              <div className="label">workers</div>
              <div className="value sm">{o.worker_count}</div>
            </div>
          </div>
          <p className="muted small" style={{ marginTop: 4 }}>
            本番手順: preflight → disk check → plan/dry-run → 小規模 smoke test → staged batch。
            enqueue は disk guard で min-free を下回ると拒否されます（--allow-low-disk で上書き可、非推奨）。
            この画面は参照専用で、一括DLボタンは置いていません。
          </p>
        </>
      ) : null}
    </div>
  );
}
