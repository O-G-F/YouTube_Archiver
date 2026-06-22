import { useState } from "react";
import { api } from "../api/endpoints";
import { ErrorBox } from "./ui";
import type { ImportReport } from "../api/types";

type Status = "idle" | "running" | "ok" | "warn" | "fail";

const STEPS = [
  { key: "preflight", label: "1. System preflight" },
  { key: "preflight_large", label: "2. Preflight large" },
  { key: "benchmark", label: "3. Dry-run benchmark" },
  { key: "staged", label: "4. Staged import" },
  { key: "verify", label: "5. Verify" },
  { key: "dbstats", label: "6. DB stats" },
  { key: "report", label: "7. Report" },
] as const;

const STAGE_LIMITS: Record<string, (number | "full")[]> = {
  liked_videos: [100, 1000, 5000, "full"],
  watch_history: [1000, 10000, 50000, "full"],
};

function Dot({ s }: { s: Status }) {
  const cls = s === "ok" ? "ok" : s === "warn" ? "warn" : s === "fail" ? "err" : "muted";
  const txt = s === "idle" ? "—" : s === "running" ? "…" : s;
  return <span className={`badge ${cls}`}>{txt}</span>;
}

/** Phase 6G: guided production import (preflight → staged → verify → report). */
export function ProductionImportWizard({ path }: { path: string }) {
  const [kind, setKind] = useState<"liked_videos" | "watch_history">("watch_history");
  const [noRawJson, setNoRawJson] = useState(true);
  const [status, setStatus] = useState<Record<string, Status>>({});
  const [detail, setDetail] = useState<Record<string, string>>({});
  const [report, setReport] = useState<ImportReport | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  function set(step: string, s: Status, d: string) {
    setStatus((p) => ({ ...p, [step]: s }));
    setDetail((p) => ({ ...p, [step]: d }));
  }

  async function run(step: string, fn: () => Promise<void>) {
    if (!path && step !== "report" && step !== "dbstats") {
      setErr("Enter a ZIP path above first.");
      return;
    }
    setBusy(step);
    setErr(null);
    set(step, "running", "");
    try {
      await fn();
    } catch (e) {
      set(step, "fail", (e as Error).message);
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const doPreflight = () =>
    run("preflight", async () => {
      const h = await api.systemHealthFull();
      const stale = h.workers.length > 0 && !h.worker_build_match;
      set(
        "preflight",
        h.ok ? "ok" : stale ? "fail" : "warn",
        `db=${h.database} redis=${h.redis} workers=${h.workers.length} build_match=${h.worker_build_match}` +
          (stale ? " — STALE WORKER: rebuild web+worker" : ""),
      );
    });

  const doPreflightLarge = () =>
    run("preflight_large", async () => {
      const p = await api.takeoutPreflightLarge({ path, kind });
      const r = p.results[kind];
      set(
        "preflight_large",
        p.ok ? "ok" : "fail",
        `parser=${p.parser_backend}` + (r ? ` sample=${r.sample_scanned} eps=${r.entries_per_second} peak=${r.peak_memory_mb}MB db_rows=${r.current_db_count}` : ""),
      );
    });

  const doBenchmark = () =>
    run("benchmark", async () => {
      const b = await api.takeoutBenchmarkLarge({ path });
      const k = b.results[kind];
      set("benchmark", "ok", k ? `${kind}: scanned=${k.scanned} eps=${k.entries_per_second} peak=${k.peak_memory_mb}MB (dry-run)` : "dry-run benchmark ok");
    });

  async function doStage(limit: number | "full") {
    if (limit === "full") {
      const ok = window.confirm(
        `Full ${kind} import (no limit). This imports ALL entries (~90k for watch). ` +
          `raw_json is ${noRawJson ? "OFF (recommended)" : "ON (large DB growth!)"}. Continue?`,
      );
      if (!ok) return;
    }
    await run("staged", async () => {
      const apiKind = kind === "liked_videos" ? "liked-videos" : "watch-history";
      const job = await api.takeoutImportJob(apiKind, {
        path,
        limit: limit === "full" ? undefined : limit,
        dry_run: false,
        store_raw_json: !noRawJson,
      });
      set("staged", "ok", `submitted job #${job.id} (limit=${limit}, no-raw-json=${noRawJson}). Monitor via Verify/Report.`);
    });
  }

  const doVerify = () =>
    run("verify", async () => {
      const r = await api.takeoutImportReportLatest();
      setReport(r);
      const s: Status = !r.leak_check_ok ? "fail" : r.status === "success" ? "ok" : r.status === "running" ? "warn" : "fail";
      set("verify", s, `status=${r.status} imported=${r.imported} store_raw_json=${r.store_raw_json} leak_ok=${r.leak_check_ok} job=${r.job_status}`);
    });

  const doDbStats = () =>
    run("dbstats", async () => {
      const d = await api.dbStats();
      set("dbstats", "ok", `${d.total_size_mb}MB watch=${d.watch_history_events} liked=${d.liked_videos} raw_json_blobs=${d.raw_json_stored_total}`);
    });

  const doReport = () =>
    run("report", async () => {
      const r = await api.takeoutImportReportLatest();
      setReport(r);
      set("report", r.ok ? "ok" : "warn", r.recommended_next_action ?? "report loaded");
    });

  const RUNNERS: Record<string, () => void> = {
    preflight: doPreflight,
    preflight_large: doPreflightLarge,
    benchmark: doBenchmark,
    verify: doVerify,
    dbstats: doDbStats,
    report: doReport,
  };

  return (
    <div className="panel">
      <h2>Production import wizard</h2>
      <p className="muted small">
        本番 import を <strong>preflight → staged → verify → report</strong> の順で安全に。
        no-raw-json は既定 ON（raw blob 非保存・正規化フィールドは保持）。full import は確認ダイアログ付き。
      </p>
      <ErrorBox error={err} />
      <div className="row" style={{ marginBottom: 8 }}>
        <label className="small">kind
          <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)} style={{ marginLeft: 6 }}>
            <option value="watch_history">watch_history</option>
            <option value="liked_videos">liked_videos</option>
          </select>
        </label>
        <label className="checkbox" title="Recommended: do not persist raw activity blobs">
          <input type="checkbox" checked={noRawJson} onChange={(e) => setNoRawJson(e.target.checked)} /> no-raw-json (recommended)
        </label>
        {!noRawJson && <span className="badge warn" title="advanced">raw-json ON — large DB growth</span>}
      </div>

      <div className="table-wrap">
        <table>
          <thead><tr><th>Step</th><th>Status</th><th>Detail</th><th></th></tr></thead>
          <tbody>
            {STEPS.map((st) => (
              <tr key={st.key}>
                <td className="small">{st.label}</td>
                <td><Dot s={status[st.key] ?? "idle"} /></td>
                <td className="small muted" style={{ maxWidth: 360 }}>{detail[st.key] ?? ""}</td>
                <td>
                  {st.key === "staged" ? (
                    <div className="row">
                      {STAGE_LIMITS[kind].map((lim) => (
                        <button key={String(lim)} className="sm" disabled={busy === "staged"}
                          onClick={() => doStage(lim)}>
                          {lim === "full" ? "full ⚠" : `+${lim}`}
                        </button>
                      ))}
                    </div>
                  ) : (
                    <button className="sm" disabled={busy === st.key} onClick={RUNNERS[st.key]}>
                      {busy === st.key ? <span className="spin" /> : "▶"} run
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {report && (
        <div className="flash" style={{ borderColor: report.ok ? "var(--ok)" : "var(--warn)" }}>
          <strong>Report {report.session_id}</strong> ({report.import_kind}) — status {report.status},
          imported {report.imported}, store_raw_json {String(report.store_raw_json)},
          raw_json blobs total {report.db_stats?.raw_json_stored_total ?? "—"},
          leak {report.leak_check_ok ? "clean ✓" : `LEAK ${report.leak_findings.join(",")}`}.
          <div className="small" style={{ marginTop: 4 }}>→ {report.recommended_next_action}</div>
        </div>
      )}
    </div>
  );
}
