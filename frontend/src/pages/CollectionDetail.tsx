import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { Bool, ErrorBox, KV, Loading } from "../components/ui";
import { Thumb } from "../components/Thumb";

const TYPE_LABELS: Record<string, string> = {
  playlist: "Playlist",
  takeout_playlist: "Playlist (Takeout)",
  channel: "Channel (subscription)",
  channel_videos: "Channel · videos",
  channel_shorts: "Channel · shorts",
  channel_streams: "Channel · streams",
};

export default function CollectionDetail() {
  const { id } = useParams();
  const cid = Number(id);
  const coll = useFetch(() => api.collection(cid), [cid]);
  const [includeRemoved, setIncludeRemoved] = useState(true);
  const items = useFetch(() => api.collectionItems(cid, { include_removed: includeRemoved, limit: 500 }), [
    cid,
    includeRemoved,
  ]);

  const [maxItems, setMaxItems] = useState("");
  const [flash, setFlash] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setBusy(true);
    setActionErr(null);
    try {
      const j = await api.refreshCollection(cid, maxItems ? Number(maxItems) : undefined);
      setFlash(`Re-crawl job #${j.id} created.`);
      items.reload();
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (coll.loading && !coll.data) return <Loading what={`collection #${cid}`} />;
  if (coll.error) return <ErrorBox error={coll.error} />;
  if (!coll.data) return null;
  const c = coll.data;

  return (
    <div>
      <h1 className="page-title">{c.title ?? `Collection #${c.id}`}</h1>
      <p className="page-sub">
        <Link to="/collections">← collections</Link>
      </p>
      {flash && <div className="flash">{flash}</div>}
      <ErrorBox error={actionErr} />

      <div className="grid2">
        <div className="panel">
          <h2>Info</h2>
          <KV
            rows={[
              ["Type", <span className="badge muted">{TYPE_LABELS[c.type] ?? c.type}</span>],
              ["Enabled", <Bool value={c.enabled} />],
              ["Crawl policy", c.crawl_policy ?? "—"],
              ["Items", String(c.item_count)],
              ["Playlist id", <span className="mono small">{c.youtube_playlist_id ?? "—"}</span>],
              ["Channel id", <span className="mono small">{c.youtube_channel_id ?? "—"}</span>],
              ["URL", <span className="mono small">{c.url ?? "—"}</span>],
              ["Updated", fmtDate(c.updated_at)],
            ]}
          />
        </div>
        <div className="panel">
          <h2>Re-crawl</h2>
          <div className="field">
            <label>max_items (optional, 0/empty = unlimited)</label>
            <input
              type="number"
              min={0}
              value={maxItems}
              onChange={(e) => setMaxItems(e.target.value)}
              placeholder="e.g. 50"
              style={{ width: 140 }}
            />
          </div>
          <button className="primary" disabled={busy} onClick={refresh}>
            {busy ? <span className="spin" /> : "▶"} Create re-crawl job
          </button>
          <p className="muted small" style={{ marginTop: 10 }}>
            Honors the collection’s crawl policy (removed-detection for “refresh”). Never re-downloads existing bodies.
          </p>
        </div>
      </div>

      <div className="panel">
        <div className="spread">
          <h2>Items ({items.data?.length ?? 0})</h2>
          <label className="checkbox">
            <input type="checkbox" checked={includeRemoved} onChange={(e) => setIncludeRemoved(e.target.checked)} />
            include removed
          </label>
        </div>
        <ErrorBox error={items.error} />
        {items.loading ? (
          <Loading />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pos</th>
                  <th>Video</th>
                  <th>Discovered</th>
                  <th>Last seen</th>
                  <th>Removed</th>
                </tr>
              </thead>
              <tbody>
                {items.data?.map((it) => (
                  <tr key={it.id} style={it.removed_at ? { opacity: 0.5 } : undefined}>
                    <td className="muted small">{it.position ?? "—"}</td>
                    <td>
                      <div className="video-thumb-cell">
                        {it.video_id ? (
                          <Link to={`/videos/${it.video_id}`}>
                            <Thumb videoId={it.video_id} has={true} size="row" />
                          </Link>
                        ) : (
                          <div className="thumb thumb-ph row">no image</div>
                        )}
                        <span className="mono small">
                          {it.video_id ? (
                            <Link to={`/videos/${it.video_id}`}>{it.youtube_video_id ?? `#${it.video_id}`}</Link>
                          ) : (
                            it.youtube_video_id ?? "—"
                          )}
                        </span>
                      </div>
                    </td>
                    <td className="muted small">{fmtDate(it.discovered_at)}</td>
                    <td className="muted small">{fmtDate(it.last_seen_at)}</td>
                    <td className="small">
                      {it.removed_at ? <span className="badge err">{fmtDate(it.removed_at)}</span> : <span className="muted">—</span>}
                    </td>
                  </tr>
                ))}
                {items.data && items.data.length === 0 && (
                  <tr>
                    <td colSpan={5} className="empty">
                      No items.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
