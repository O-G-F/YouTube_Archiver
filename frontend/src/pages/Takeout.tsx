import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtBytes } from "../lib/format";
import { ErrorBox, Loading } from "../components/ui";
import { TakeoutSessions } from "../components/TakeoutSessions";
import { TakeoutDbStats } from "../components/TakeoutDbStats";
import { TakeoutOps } from "../components/TakeoutOps";
import { ProductionImportWizard } from "../components/ProductionImportWizard";
import type { TakeoutBenchmark, TakeoutImportAll, TakeoutInspect, TakeoutPreview } from "../api/types";

const KIND_LABEL: Record<string, string> = {
  youtube_takeout: "YouTube Takeout",
  my_activity_takeout: "My Activity Takeout",
  takeout_index: "Index only",
  unknown_takeout: "Unknown",
};
function KindBadge({ kind }: { kind: string }) {
  const cls =
    kind === "my_activity_takeout" ? "ok" : kind === "youtube_takeout" ? "run" : kind === "takeout_index" ? "warn" : "muted";
  return <span className={`badge ${cls}`}>{KIND_LABEL[kind] ?? kind}</span>;
}

export default function Takeout() {
  const discover = useFetch(() => api.takeoutDiscover(false), []);
  const [path, setPath] = useState("");
  const [preview, setPreview] = useState<TakeoutPreview | null>(null);
  const [importResult, setImportResult] = useState<TakeoutImportAll | null>(null);
  const [likedResult, setLikedResult] = useState<string | null>(null);
  const [dryRun, setDryRun] = useState(true);
  const [limit, setLimit] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [registry, setRegistry] = useState<TakeoutInspect | null>(null);
  const [sessionsKey, setSessionsKey] = useState(0);
  const [jobMode, setJobMode] = useState(false);
  const [noRawJson, setNoRawJson] = useState(false);
  const [bench, setBench] = useState<TakeoutBenchmark | null>(null);

  async function doBenchmark(kind: string) {
    if (!path) return;
    setBusy("bench");
    setErr(null);
    setBench(null);
    const n = limit ? Number(limit) : undefined;
    try {
      setBench(await api.takeoutBenchmark({ path, kind, limit: n, dry_run: true }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function doPreview(p?: string) {
    const target = p ?? path;
    if (!target) return;
    setBusy("preview");
    setErr(null);
    setPreview(null);
    setImportResult(null);
    setLikedResult(null);
    setRegistry(null);
    setPath(target);
    try {
      const [pv, ins] = await Promise.all([
        api.takeoutPreview(target),
        api.takeoutInspect(target, true).catch(() => null),
      ]);
      setPreview(pv);
      setRegistry(ins);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function doImportKind(kind: "watch" | "search") {
    if (!path) return;
    setBusy(kind);
    setErr(null);
    const n = limit ? Number(limit) : undefined;
    try {
      if (jobMode) {
        const j = await api.takeoutImportJob(kind === "watch" ? "watch-history" : "search-history", { path, limit: n, dry_run: dryRun, store_raw_json: !noRawJson });
        setLikedResult(`${kind} history import queued as background job #${j.id}` + (dryRun ? " [dry-run]" : "") + (noRawJson ? " [no-raw-json]" : "") + ".");
      } else {
        const r = kind === "watch"
          ? await api.takeoutImportWatch({ path, limit: n, dry_run: dryRun })
          : await api.takeoutImportSearch({ path, limit: n, dry_run: dryRun });
        setLikedResult(
          `${kind === "watch" ? "Watch" : "Search"} history: imported ${r.imported_count}, ` +
            `skipped ${r.skipped_duplicate_count}, failed ${r.failed_count} (scanned ${r.scanned})` +
            (r.dry_run ? " [dry-run]" : "")
        );
      }
      setSessionsKey((k) => k + 1);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function doImportLiked(p?: string) {
    const target = p ?? path;
    if (!target) return;
    setBusy("liked");
    setErr(null);
    setLikedResult(null);
    setPath(target);
    const n = limit ? Number(limit) : undefined;
    try {
      if (jobMode) {
        const j = await api.takeoutImportJob("liked-videos", { path: target, limit: n, dry_run: dryRun, store_raw_json: !noRawJson });
        setLikedResult(`Liked import queued as background job #${j.id}` + (dryRun ? " [dry-run]" : "") + (noRawJson ? " [no-raw-json]" : "") + ".");
      } else {
        const r = await api.takeoutImportLiked({ path: target, limit: n });
        setLikedResult(
          `Imported ${r.imported_count} liked (skipped ${r.skipped_duplicate_count}, updated ${r.updated_count ?? 0}, video stubs ${r.videos_created}) from ${r.source_kind}.`
        );
        discover.reload();
      }
      setSessionsKey((k) => k + 1);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function doImportAll() {
    if (!path) return;
    setBusy("import");
    setErr(null);
    setImportResult(null);
    const n = limit ? Number(limit) : undefined;
    try {
      setImportResult(
        await api.takeoutImportAll({
          path,
          dry_run: dryRun,
          limit_watch: n,
          limit_search: n,
          limit_subscriptions: n,
          limit_playlists: n,
          limit_items: n,
          limit_liked: n,
        })
      );
      setSessionsKey((k) => k + 1);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <h1 className="page-title">Takeout import</h1>
      <p className="page-sub">
        Import Google Takeout placed under TAKEOUT_IMPORT_ROOT. <strong>Liked videos の全履歴は「My Activity」Takeout</strong>
        に入っています（YouTube Takeout には通常 liked=0）。個人データは件数のみ表示します。
      </p>
      <ErrorBox error={err} />
      <ErrorBox error={discover.error} />

      <div className="panel">
        <div className="spread">
          <h2>Detected Takeout ZIPs</h2>
          <button onClick={discover.reload}>↻ {discover.loading && <span className="spin" />}</button>
        </div>
        <p className="muted small mono">{discover.data?.root}</p>
        {discover.loading && !discover.data ? (
          <Loading />
        ) : discover.data && discover.data.archives.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="wrap">File</th>
                  <th>Kind</th>
                  <th>Size</th>
                  <th>Liked source</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {discover.data.archives.map((a) => (
                  <tr key={a.name}>
                    <td className="wrap mono small">{a.name}</td>
                    <td><KindBadge kind={a.archive_kind} /></td>
                    <td className="small">{fmtBytes(a.size)}</td>
                    <td className="small muted">
                      {a.archive_kind === "takeout_index"
                        ? "目次のみ (実データではない)"
                        : a.liked_source_kind === "takeout_my_activity"
                        ? "My Activity ✓"
                        : a.liked_source_kind === "takeout_youtube"
                        ? "YouTube CSV"
                        : "—"}
                    </td>
                    <td>
                      <div className="row">
                        <button className="sm" disabled={a.archive_kind === "takeout_index"} onClick={() => doPreview(a.name)}>
                          Preview
                        </button>
                        {a.liked_source_kind?.startsWith("takeout") && (
                          <button className="sm" disabled={busy === "liked"} onClick={() => doImportLiked(a.name)}>
                            Import liked
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted small">No .zip files found. Copy Takeout ZIPs into the import directory shown above.</p>
        )}
        <p className="muted small" style={{ marginTop: 8 }}>
          ※ サーバ上のパスを選択します（ブラウザからのアップロードは将来対応）。`archive_browser.html` のみの ZIP は
          <strong> 目次のみ</strong>で、実データではありません。
        </p>
      </div>

      {likedResult && <div className="flash">{likedResult} <Link to="/liked-videos">View liked videos →</Link></div>}

      <div className="panel">
        <h2>Preview / Import</h2>
        <div className="row">
          <div className="field inline" style={{ flex: 1, minWidth: 280 }}>
            <label>ZIP path (relative to import root)</label>
            <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="takeout-XXXX.zip" style={{ width: "100%" }} />
          </div>
          <div className="field inline">
            <label>limit (optional)</label>
            <input type="number" min={0} value={limit} onChange={(e) => setLimit(e.target.value)} style={{ width: 110 }} />
          </div>
          <button disabled={!path || busy === "preview"} onClick={() => doPreview()}>
            {busy === "preview" ? <span className="spin" /> : "🔍"} Preview
          </button>
          <button disabled={!path || busy === "liked"} onClick={() => doImportLiked()}>
            {busy === "liked" ? <span className="spin" /> : "⤓"} Import liked
          </button>
          <button disabled={!path || busy === "watch"} onClick={() => doImportKind("watch")}>
            {busy === "watch" ? <span className="spin" /> : "⤓"} Import watch
          </button>
          <button disabled={!path || busy === "search"} onClick={() => doImportKind("search")}>
            {busy === "search" ? <span className="spin" /> : "⤓"} Import search
          </button>
          <button disabled={!path || busy === "bench"} onClick={() => doBenchmark("liked_videos")}>
            {busy === "bench" ? <span className="spin" /> : "⏱"} Benchmark
          </button>
          <label className="checkbox" style={{ alignSelf: "center" }}>
            <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} /> dry-run
          </label>
          <label className="checkbox" style={{ alignSelf: "center" }} title="Run import on the worker (large imports)">
            <input type="checkbox" checked={jobMode} onChange={(e) => setJobMode(e.target.checked)} /> background job
          </label>
          <label className="checkbox" style={{ alignSelf: "center" }} title="Do not persist raw activity blobs (privacy / DB size)">
            <input type="checkbox" checked={noRawJson} onChange={(e) => setNoRawJson(e.target.checked)} /> no-raw-json
          </label>
        </div>

        <p className="muted small">
          大容量（liked ~11k / watch ~90k）は <strong>dry-run / limit / background job</strong> を推奨。
          ストリーム解析（ijson）で省メモリ。raw_json/絶対パスは保存しません。
        </p>
        {bench && (
          <div className="flash">
            <strong>Benchmark ({bench.kind})</strong>: scanned {bench.scanned}, {bench.entries_per_second ?? "—"} entries/s,
            peak {bench.peak_memory_mb ?? "—"} MB, parser <code>{bench.parser_backend}</code>, source {bench.source_kind ?? "—"}
            {bench.dry_run ? " (dry-run)" : ""}.
          </div>
        )}

        {registry && registry.registry.length > 0 && (
          <div style={{ marginTop: 8 }}>
            <span className="muted small">detected sources (deep):</span>{" "}
            {registry.registry.map((r, i) => (
              <span key={i} className="badge muted" title={r.member}>{r.kind}</span>
            ))}
          </div>
        )}

        {preview && (
          <>
            <div className="row" style={{ marginTop: 10 }}>
              <KindBadge kind={preview.archive_kind ?? "unknown_takeout"} />
              <span className="muted small">liked source: {preview.liked_source_kind ?? "—"}</span>
            </div>
            <h3>Counts</h3>
            <div className="cards">
              <div className="card"><div className="label">Liked videos</div><div className="value sm">{preview.likes_count}</div></div>
              <div className="card"><div className="label">Watch history</div><div className="value sm">{preview.watch_history_count}</div></div>
              <div className="card"><div className="label">Search history</div><div className="value sm">{preview.search_history_count}</div></div>
              <div className="card"><div className="label">Subscriptions</div><div className="value sm">{preview.subscriptions_count}</div></div>
              <div className="card"><div className="label">Playlists</div><div className="value sm">{preview.playlists_count}</div></div>
            </div>
            {preview.archive_kind === "youtube_takeout" && preview.likes_count === 0 && (
              <div className="flash" style={{ borderColor: "var(--warn)" }}>
                YouTube Takeout に liked videos はありません（通常です）。高評価の全履歴は <strong>My Activity Takeout</strong> を指定してください。
              </div>
            )}
            {preview.liked_samples.length > 0 && (
              <>
                <h3>Liked samples</h3>
                <div className="tag-list">
                  {preview.liked_samples.map((s, i) => (
                    <span key={i} className="badge muted">{(s.title ?? s.youtube_video_id ?? "?").slice(0, 40)}</span>
                  ))}
                </div>
              </>
            )}
            {preview.warnings.length > 0 && <div className="error-box">{preview.warnings.join("\n")}</div>}

            <h3>Import all (watch / search / subs / playlists / liked)</h3>
            <div className="row">
              <label className="checkbox">
                <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} /> dry-run (no DB writes)
              </label>
              <button className="primary" disabled={busy === "import"} onClick={doImportAll}>
                {busy === "import" ? <span className="spin" /> : "▶"} {dryRun ? "Dry-run import-all" : "Import all"}
              </button>
            </div>
          </>
        )}

        {importResult && (
          <>
            <h3>{importResult.dry_run ? "Dry-run result" : "Import result"}</h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Section</th>
                    <th className="num">imported</th>
                    <th className="num">skipped dup</th>
                    <th className="num">failed</th>
                    <th className="num">scanned</th>
                  </tr>
                </thead>
                <tbody>
                  {(["watch_history", "search_history", "subscriptions"] as const).map((k) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td className="num">{importResult[k].imported_count}</td>
                      <td className="num">{importResult[k].skipped_duplicate_count}</td>
                      <td className="num">{importResult[k].failed_count}</td>
                      <td className="num">{importResult[k].scanned}</td>
                    </tr>
                  ))}
                  <tr>
                    <td>playlists</td>
                    <td className="num">{importResult.playlists.playlists_imported}p / {importResult.playlists.items_imported}i</td>
                    <td className="num">{importResult.playlists.items_skipped}</td>
                    <td className="num">—</td>
                    <td className="num">{importResult.playlists.scanned_playlists}</td>
                  </tr>
                  <tr>
                    <td>liked_videos</td>
                    <td className="num">{importResult.liked_videos.imported_count}</td>
                    <td className="num">{importResult.liked_videos.skipped_duplicate_count}</td>
                    <td className="num">{importResult.liked_videos.failed_count}</td>
                    <td className="num">{importResult.liked_videos.scanned}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      <ProductionImportWizard path={path} />
      <TakeoutOps path={path} />
      <TakeoutDbStats path={path} onChanged={() => setSessionsKey((k) => k + 1)} />
      <TakeoutSessions reloadKey={sessionsKey} />
    </div>
  );
}
