import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "../components/ui";

// Where each library category currently links (existing screens) until full
// sync lands in a later phase.
const LINKS: Record<string, string | null> = {
  liked_videos: null,
  watch_history: null, // backed by /api/watch-history (no dedicated screen yet)
  search_history: null,
  subscriptions: null,
  playlists: "/collections",
};

export default function Library() {
  const { data, error, loading } = useFetch(() => api.librarySummary(), []);

  return (
    <div>
      <h1 className="page-title">Library</h1>
      <p className="page-sub">
        ライブラリ分類（将来の同期に向けた土台）。<strong>Liked videos</strong> は未同期で、後続フェーズで
        Google Takeout / YouTube Data API の両方を検討します。
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

      <div className="panel">
        <h2>Notes</h2>
        <ul className="muted small" style={{ lineHeight: 1.8 }}>
          <li>Subscriptions / playlists / watch history は既に Takeout インポートで取り込めます（Takeout 画面）。</li>
          <li>Liked videos は Takeout の likes と YouTube Data API のどちらも将来活用する想定です（Phase 6A 以降）。</li>
          <li>このページの型/API（<code>/api/library/summary</code>）は将来カテゴリを追加しやすい形にしています。</li>
        </ul>
      </div>
    </div>
  );
}
