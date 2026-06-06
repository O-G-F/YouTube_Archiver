import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "./ui";

function CheckBadge({ status }: { status: string }) {
  const cls = status === "ok" ? "ok" : status === "failed" ? "err" : "warn";
  return <span className={`badge ${cls}`}>{status}</span>;
}

export function YouTubeDiagnostics() {
  const doctor = useFetch(() => api.doctorYoutube(), []);
  const [url, setUrl] = useState("https://youtu.be/dQw4w9WgXcQ");
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<{ id: number; what: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function run(kind: "metadata" | "video") {
    setBusy(kind);
    setErr(null);
    setFlash(null);
    try {
      const j = await api.youtubeDiagnosticsRun({
        url,
        include_video_download: kind === "video",
      });
      setFlash({ id: j.id, what: kind === "video" ? "metadata + subtitles + small video" : "metadata + subtitles" });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const d = doctor.data;
  return (
    <div className="panel">
      <div className="spread">
        <h2>YouTube fetch stability (diagnostics)</h2>
        <button onClick={doctor.reload}>↻</button>
      </div>
      <ErrorBox error={doctor.error} />
      {doctor.loading && !d ? (
        <Loading />
      ) : d ? (
        <>
          <div className="cards">
            <div className="card"><div className="label">yt-dlp</div><div className="value sm mono">{d.ytdlp_version ?? "—"}</div></div>
            <div className="card"><div className="label">deno</div><div className="value sm"><span className={`badge ${d.deno_available ? "ok" : "warn"}`}>{d.deno_available ? "yes" : "no"}</span></div></div>
            <div className="card"><div className="label">remote-components</div><div className="value sm mono">{d.remote_components ?? "off"}</div></div>
            <div className="card"><div className="label">curl_cffi</div><div className="value sm"><span className={`badge ${d.curl_cffi_installed ? "ok" : "warn"}`}>{d.curl_cffi_installed ? "installed" : "no"}</span></div></div>
            <div className="card"><div className="label">impersonate targets</div><div className="value sm">{d.impersonate_targets}</div></div>
            <div className="card"><div className="label">cookies</div><div className="value sm"><span className={`badge ${d.cookies.configured ? "ok" : "warn"}`}>{d.cookies.configured ? "configured" : "no"}</span></div></div>
            <div className="card"><div className="label">cookies readable</div><div className="value sm"><span className={`badge ${d.cookies.readable ? "ok" : "muted"}`}>{d.cookies.readable ? "yes" : "no"}</span></div></div>
            <div className="card"><div className="label">PO token</div><div className="value sm"><span className={`badge ${d.po_token_configured ? "ok" : "muted"}`}>{d.po_token_configured ? "set" : "no"}</span></div></div>
          </div>

          <h3>Checks</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Check</th><th>Status</th><th className="wrap">Detail</th></tr></thead>
              <tbody>
                {d.checks.map((c) => (
                  <tr key={c.name}>
                    <td>{c.name}</td>
                    <td><CheckBadge status={c.status} /></td>
                    <td className="wrap small muted">{c.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h3>Recommendations</h3>
          <ul className="muted small" style={{ lineHeight: 1.7 }}>
            {d.recommendations.map((r, i) => <li key={i}>{r}</li>)}
          </ul>

          <h3>Run a live test</h3>
          <p className="muted small">
            metadata / subtitles テストは<strong>本体を保存しません</strong>。video テストは一時ディレクトリにDLして<strong>すぐ削除</strong>（DB の media body は増えません）。secret は表示されません。
          </p>
          <div className="row">
            <input value={url} onChange={(e) => setUrl(e.target.value)} style={{ width: 280 }} placeholder="https://youtu.be/…" />
            <button disabled={!url || busy === "metadata"} onClick={() => run("metadata")}>
              {busy === "metadata" ? <span className="spin" /> : "▶"} Metadata + subtitles
            </button>
            <button disabled={!url || busy === "video"} onClick={() => run("video")}>
              {busy === "video" ? <span className="spin" /> : "▶"} + small video
            </button>
          </div>
          <ErrorBox error={err} />
          {flash && (
            <div className="flash">
              Created youtube_diagnostic job (<Link to={`/jobs/${flash.id}`}>#{flash.id}</Link>) — runs {flash.what} on the worker; see the job detail for results & recommendations.
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}
