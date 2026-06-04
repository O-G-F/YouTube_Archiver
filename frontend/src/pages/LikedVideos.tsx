import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDate } from "../lib/format";
import { ErrorBox, Loading } from "../components/ui";
import { Thumb } from "../components/Thumb";

export default function LikedVideos() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [onlyMissing, setOnlyMissing] = useState(false);
  const stats = useFetch(() => api.likedVideosStats(), []);
  const { data, error, loading, reload } = useFetch(
    () => api.likedVideos({ q: query || undefined, only_missing_metadata: onlyMissing, limit: 200 }),
    [query, onlyMissing]
  );

  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);

  async function enqueueOne(videoUrl: string | null, key: string) {
    if (!videoUrl) return;
    setBusy(key);
    setActionErr(null);
    try {
      const j = await api.archiveUrl({ url: videoUrl, profile: "metadata_only" });
      setFlash(`Created metadata_only job #${j.id}.`);
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function enqueueMissing() {
    setBusy("bulk");
    setActionErr(null);
    try {
      const r = await api.enqueueLikedMetadata({ limit: 50, only_missing_metadata: true });
      setFlash(`Enqueued ${r.jobs_created} metadata_only job(s) for liked videos missing metadata.`);
      reload();
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <div className="spread">
        <div>
          <h1 className="page-title">Liked videos</h1>
          <p className="page-sub">Imported from Google Takeout. metadata_only enqueue never downloads the body.</p>
        </div>
        <button disabled={busy === "bulk"} onClick={enqueueMissing}>
          {busy === "bulk" ? <span className="spin" /> : "⤓"} Fetch metadata (missing)
        </button>
      </div>

      {stats.data && (
        <div className="cards">
          <div className="card"><div className="label">Total liked</div><div className="value">{stats.data.total}</div></div>
          <div className="card"><div className="label">Linked videos</div><div className="value sm">{stats.data.linked_videos}</div></div>
          <div className="card"><div className="label">Metadata fetched</div><div className="value sm">{stats.data.metadata_fetched}</div></div>
          <div className="card"><div className="label">Earliest</div><div className="value sm">{fmtDate(stats.data.earliest)}</div></div>
          <div className="card"><div className="label">Latest</div><div className="value sm">{fmtDate(stats.data.latest)}</div></div>
        </div>
      )}

      {flash && <div className="flash">{flash}</div>}
      <ErrorBox error={actionErr} />
      <ErrorBox error={error} />

      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(q);
        }}
      >
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search title / channel / id" style={{ width: 240 }} />
        <label className="checkbox">
          <input type="checkbox" checked={onlyMissing} onChange={(e) => setOnlyMissing(e.target.checked)} />
          only missing metadata
        </label>
        <button type="submit">Search</button>
        <button type="button" onClick={reload}>↻ {loading && <span className="spin" />}</button>
      </form>

      {loading && !data ? (
        <Loading />
      ) : data && data.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Video</th>
                <th>Channel</th>
                <th>Liked at</th>
                <th>Metadata</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.map((lv) => (
                <tr key={lv.id}>
                  <td className="wrap">
                    <div className="video-thumb-cell">
                      {lv.video_id ? (
                        <Link to={`/videos/${lv.video_id}`}>
                          <Thumb videoId={lv.video_id} has={lv.metadata_fetched} size="row" />
                        </Link>
                      ) : (
                        <div className="thumb thumb-ph row">no id</div>
                      )}
                      <div style={{ minWidth: 0 }}>
                        {lv.video_id ? (
                          <Link to={`/videos/${lv.video_id}`}>{lv.title ?? "(metadata not fetched)"}</Link>
                        ) : (
                          <span>{lv.title ?? "(metadata not fetched)"}</span>
                        )}
                        <div className="muted mono small">{lv.youtube_video_id ?? "—"}</div>
                      </div>
                    </div>
                  </td>
                  <td className="muted small">{lv.channel_title ?? "—"}</td>
                  <td className="muted small">{fmtDate(lv.liked_at)}</td>
                  <td>
                    {lv.metadata_fetched ? (
                      <span className="badge ok">fetched</span>
                    ) : (
                      <span className="badge warn">未取得</span>
                    )}
                  </td>
                  <td>
                    {!lv.metadata_fetched && (
                      <button
                        className="sm"
                        disabled={busy === `one-${lv.id}` || !lv.url}
                        onClick={() => enqueueOne(lv.url, `one-${lv.id}`)}
                      >
                        {busy === `one-${lv.id}` ? <span className="spin" /> : "Fetch metadata"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty">
          No liked videos. Import a Takeout ZIP from the <Link to="/takeout">Takeout</Link> page.
        </div>
      )}
    </div>
  );
}
