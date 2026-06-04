import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "../components/ui";

const LINKS: Record<string, string | null> = {
  liked_videos: "/liked-videos",
  watch_history: null,
  search_history: null,
  subscriptions: null,
  playlists: "/collections",
};

const SOURCE_LABEL: Record<string, string> = {
  takeout_my_activity: "My Activity",
  takeout_youtube: "YouTube Takeout",
  takeout: "Takeout",
  youtube_data_api: "YouTube Data API",
};

export default function Library() {
  const { data, error, loading } = useFetch(() => api.librarySummary(), []);
  const oauth = useFetch(() => api.youtubeApiStatus(), []);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const liked = data?.categories.find((c) => c.key === "liked_videos");
  const sources = data?.liked_sources ?? {};

  async function syncApi() {
    setBusy(true);
    setSyncMsg(null);
    try {
      const r = await api.youtubeApiSyncLiked({ limit: 200, stop_on_existing: true });
      setSyncMsg(
        r.ok
          ? `Synced ${r.imported_count} new liked video(s) from the API (stopped_on_existing=${r.stopped_on_existing}).`
          : `API not available: [${r.classification}] ${r.message}`
      );
    } catch (e) {
      setSyncMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h1 className="page-title">Library</h1>
      <p className="page-sub">
        ライブラリ分類。<strong>高評価の全履歴は My Activity Takeout</strong>、最近分は YouTube Data API（差分）で補完します。
      </p>
      <ErrorBox error={error} />

      {loading && !data ? (
        <Loading />
      ) : (
        <div className="cards">
          {data?.categories.map((c) => {
            const to = LINKS[c.key];
            const inner = (
              <div className="card" style={c.available ? undefined : { opacity: 0.65 }}>
                <div className="label">{c.label}</div>
                <div className="value">{c.available ? c.count : "—"}</div>
                {!c.available && <span className="badge muted">planned</span>}
                {c.note && <div className="muted small" style={{ marginTop: 6 }}>{c.note}</div>}
              </div>
            );
            return to ? (
              <Link key={c.key} to={to} style={{ textDecoration: "none", color: "inherit" }}>
                {inner}
              </Link>
            ) : (
              <div key={c.key}>{inner}</div>
            );
          })}
        </div>
      )}

      <div className="grid2">
        <div className="panel">
          <h2>Liked videos by source</h2>
          {liked && liked.count === 0 ? (
            <div className="flash" style={{ borderColor: "var(--warn)" }}>
              <strong>高評価動画が 0 件です。</strong>
              <ul className="small" style={{ marginTop: 6, lineHeight: 1.7 }}>
                <li>現在の Takeout に liked videos が含まれていない可能性があります。</li>
                <li><strong>My Activity Takeout</strong>（マイ アクティビティ/YouTube）を <Link to="/takeout">Takeout 画面</Link>で指定してください。</li>
                <li>YouTube Data API 同期は OAuth 設定済みなら利用できます（下記）。</li>
              </ul>
            </div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Source</th><th className="num">Count</th></tr>
                </thead>
                <tbody>
                  {Object.entries(sources).map(([src, n]) => (
                    <tr key={src}>
                      <td><span className="badge muted">{SOURCE_LABEL[src] ?? src}</span></td>
                      <td className="num">{n}</td>
                    </tr>
                  ))}
                  {Object.keys(sources).length === 0 && (
                    <tr><td colSpan={2} className="muted small">none</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
          <p className="muted small" style={{ marginTop: 8 }}>
            <Link to="/liked-videos">Liked videos 一覧 →</Link>（metadata_only enqueue は本体を保存しません）
          </p>
        </div>

        <div className="panel">
          <h2>YouTube Data API (differential)</h2>
          <ErrorBox error={oauth.error} />
          {oauth.data ? (
            <>
              <div className="kv">
                <div className="k">enabled</div><div className="v">{String(oauth.data.enabled)}</div>
                <div className="k">configured</div><div className="v">{String(oauth.data.configured)}</div>
                <div className="k">method</div><div className="v">{oauth.data.method}</div>
              </div>
              {!oauth.data.configured ? (
                <div className="flash" style={{ marginTop: 10 }}>
                  OAuth 未設定です。設定すると<strong>差分同期</strong>が使えます（既存に到達したら停止）。設定手順は README 参照。
                  API は<strong>過去全件を保証しません（実用上 ~5000 件）</strong>。全履歴は My Activity Takeout を使用。
                </div>
              ) : (
                <button className="primary" style={{ marginTop: 10 }} disabled={busy} onClick={syncApi}>
                  {busy ? <span className="spin" /> : "↻"} Sync liked (differential)
                </button>
              )}
              {syncMsg && <div className="flash" style={{ marginTop: 10 }}>{syncMsg}</div>}
            </>
          ) : (
            <Loading />
          )}
        </div>
      </div>

      <div className="panel">
        <h2>初回 DB 構築（Hybrid bootstrap）</h2>
        <ul className="muted small" style={{ lineHeight: 1.8 }}>
          <li><strong>初回</strong>: My Activity Takeout（liked 全履歴）+ YouTube Takeout（watch/search/subs/playlists）。</li>
          <li><strong>逐次更新</strong>: YouTube Data API（差分・既存到達で停止）。</li>
          <li>CLI: <code>archiver library bootstrap --youtube-takeout … --myactivity-takeout …</code></li>
          <li>API: <code>POST /api/library/bootstrap</code>（Takeout 画面から個別 import も可）。</li>
        </ul>
      </div>
    </div>
  );
}
