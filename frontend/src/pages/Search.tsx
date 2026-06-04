import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { ApiError } from "../api/client";
import { ErrorBox, Loading } from "../components/ui";
import type { SearchResponse, SearchResult } from "../api/types";

const TYPES = [
  { key: "video", label: "Videos" },
  { key: "comment", label: "Comments" },
  { key: "live_chat", label: "Live chat" },
  { key: "collection", label: "Collections" },
];

function typeBadge(t: SearchResult["type"]) {
  const cls = t === "video" ? "run" : t === "collection" ? "muted" : t === "comment" ? "ok" : "warn";
  return <span className={`badge ${cls}`}>{t}</span>;
}

function Hit({ r }: { r: SearchResult }) {
  const to =
    r.type === "collection" && r.collection_id
      ? `/collections/${r.collection_id}`
      : r.video_id
      ? `/videos/${r.video_id}`
      : "#";
  return (
    <Link to={to} className="search-hit">
      <div className="row" style={{ gap: 8 }}>
        {typeBadge(r.type)}
        <strong>{r.title ?? "—"}</strong>
        {r.author_name && <span className="muted small">· {r.author_name}</span>}
        {r.extra && <span className="muted small">· {r.extra}</span>}
      </div>
      {r.snippet && <div className="snip">{r.snippet}</div>}
    </Link>
  );
}

export default function Search() {
  const [q, setQ] = useState("");
  const [active, setActive] = useState<Record<string, boolean>>({
    video: true,
    comment: true,
    live_chat: true,
    collection: true,
  });
  const [data, setData] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(e?: React.FormEvent) {
    e?.preventDefault();
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    const types = TYPES.filter((t) => active[t.key]).map((t) => t.key).join(",");
    try {
      setData(await api.search({ q: q.trim(), types: types || undefined, limit: 30 }));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : (err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  const grouped = TYPES.map((t) => ({
    ...t,
    rows: (data?.results ?? []).filter((r) => r.type === t.key),
  }));

  return (
    <div>
      <h1 className="page-title">Search</h1>
      <p className="page-sub">Across video titles/channels, comments, live chat, and collections (raw data はマスク).</p>

      <form className="toolbar" onSubmit={run}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="search…"
          style={{ width: 320 }}
          autoFocus
        />
        <button className="primary" type="submit" disabled={loading || !q.trim()}>
          {loading ? <span className="spin" /> : "🔍"} Search
        </button>
        <div className="row" style={{ marginLeft: 8 }}>
          {TYPES.map((t) => (
            <label key={t.key} className="checkbox">
              <input
                type="checkbox"
                checked={active[t.key]}
                onChange={(e) => setActive((a) => ({ ...a, [t.key]: e.target.checked }))}
              />
              {t.label}
            </label>
          ))}
        </div>
      </form>

      <ErrorBox error={error} />
      {loading ? (
        <Loading />
      ) : data ? (
        data.total === 0 ? (
          <div className="empty">No results for “{data.query}”.</div>
        ) : (
          <>
            <p className="muted small">{data.total} result(s) for “{data.query}”.</p>
            {grouped.map(
              (g) =>
                g.rows.length > 0 && (
                  <div className="panel" key={g.key}>
                    <h2>
                      {g.label} ({g.rows.length})
                    </h2>
                    {g.rows.map((r, i) => (
                      <Hit key={i} r={r} />
                    ))}
                  </div>
                )
            )}
          </>
        )
      ) : (
        <p className="muted small">Type a query and press Search.</p>
      )}
    </div>
  );
}
