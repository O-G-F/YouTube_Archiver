import { useState } from "react";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtBytes, fmtDate } from "../lib/format";
import { ErrorBox, Loading } from "../components/ui";
import type { TakeoutImportAll, TakeoutPreview } from "../api/types";

export default function Takeout() {
  const files = useFetch(() => api.takeoutFiles(), []);
  const [path, setPath] = useState("");
  const [preview, setPreview] = useState<TakeoutPreview | null>(null);
  const [importResult, setImportResult] = useState<TakeoutImportAll | null>(null);
  const [dryRun, setDryRun] = useState(true);
  const [limit, setLimit] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function doPreview(p?: string) {
    const target = p ?? path;
    if (!target) return;
    setBusy("preview");
    setErr(null);
    setPreview(null);
    setImportResult(null);
    setPath(target);
    try {
      setPreview(await api.takeoutPreview(target));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function doImport() {
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
        Import Google Takeout data placed under TAKEOUT_IMPORT_ROOT. Personal data (watch/search/raw) is summarized as
        counts only — never dumped here.
      </p>
      <ErrorBox error={err} />
      <ErrorBox error={files.error} />

      <div className="panel">
        <div className="spread">
          <h2>Available ZIP files</h2>
          <button onClick={files.reload}>↻</button>
        </div>
        {files.loading && !files.data ? (
          <Loading />
        ) : (
          <>
            <p className="muted small mono">{files.data?.root}</p>
            {files.data && files.data.files.length > 0 ? (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th className="wrap">File</th>
                      <th>Size</th>
                      <th>Modified</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {files.data.files.map((f) => (
                      <tr key={f.name}>
                        <td className="wrap mono small">{f.name}</td>
                        <td className="small">{fmtBytes(f.size)}</td>
                        <td className="muted small">{fmtDate(f.modified_at)}</td>
                        <td>
                          <button className="sm" onClick={() => doPreview(f.name)}>
                            Preview
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted small">
                No .zip files found. Copy a Takeout ZIP into the import directory shown above.
              </p>
            )}
          </>
        )}
      </div>

      <div className="panel">
        <h2>Preview / Import</h2>
        <div className="row">
          <div className="field inline" style={{ flex: 1, minWidth: 280 }}>
            <label>ZIP path (relative to import root)</label>
            <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="takeout-XXXX.zip" style={{ width: "100%" }} />
          </div>
          <button disabled={!path || busy === "preview"} onClick={() => doPreview()}>
            {busy === "preview" ? <span className="spin" /> : "🔍"} Preview
          </button>
        </div>

        {preview && (
          <>
            <h3>Counts</h3>
            <div className="cards">
              <div className="card"><div className="label">Watch history</div><div className="value sm">{preview.watch_history_count}</div></div>
              <div className="card"><div className="label">Search history</div><div className="value sm">{preview.search_history_count}</div></div>
              <div className="card"><div className="label">Likes</div><div className="value sm">{preview.likes_count}</div></div>
              <div className="card"><div className="label">Subscriptions</div><div className="value sm">{preview.subscriptions_count}</div></div>
              <div className="card"><div className="label">Playlists</div><div className="value sm">{preview.playlists_count}</div></div>
            </div>
            {preview.warnings.length > 0 && (
              <div className="error-box">{preview.warnings.join("\n")}</div>
            )}

            <h3>Import all</h3>
            <div className="row">
              <label className="checkbox">
                <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} /> dry-run (no DB writes)
              </label>
              <div className="field inline">
                <label>per-section limit (optional)</label>
                <input type="number" min={0} value={limit} onChange={(e) => setLimit(e.target.value)} style={{ width: 120 }} />
              </div>
              <button className="primary" disabled={busy === "import"} onClick={doImport}>
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
    </div>
  );
}
