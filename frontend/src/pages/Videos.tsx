import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDuration, fmtUploadDate } from "../lib/format";
import { ErrorBox, Loading, StateBadge } from "../components/ui";
import { Thumb } from "../components/Thumb";

const COMMENT_STATES = ["", "comments_disabled", "unavailable", "frozen"];
const LIVECHAT_STATES = ["", "available", "not_available", "unavailable", "frozen"];
const SORTS = [
  { v: "added_desc", label: "Recently added" },
  { v: "added_asc", label: "Oldest added" },
  { v: "upload_desc", label: "Newest upload" },
  { v: "upload_asc", label: "Oldest upload" },
  { v: "title", label: "Title A–Z" },
];
const PAGE = 50;

export default function Videos() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [channel, setChannel] = useState("");
  const [commentsState, setCommentsState] = useState("");
  const [liveState, setLiveState] = useState("");
  const [hasMedia, setHasMedia] = useState("");
  const [sort, setSort] = useState("added_desc");
  const [page, setPage] = useState(0);

  const channels = useFetch(() => api.videoChannels(), []);
  const { data, error, loading, reload } = useFetch(
    () =>
      api.videos({
        q: query || undefined,
        channel_id: channel || undefined,
        comments_state: commentsState || undefined,
        live_chat_state: liveState || undefined,
        has_media: hasMedia === "" ? undefined : hasMedia === "yes",
        sort,
        limit: PAGE,
        offset: page * PAGE,
      }),
    [query, channel, commentsState, liveState, hasMedia, sort, page]
  );

  function resetPageThen(fn: () => void) {
    setPage(0);
    fn();
  }

  return (
    <div>
      <h1 className="page-title">Videos</h1>
      <p className="page-sub">Saved / registered videos. “body” = downloaded video/audio (0 = 未保存).</p>

      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault();
          resetPageThen(() => setQuery(q));
        }}
      >
        <div className="field inline">
          <label>Search</label>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="title / channel / id" style={{ width: 200 }} />
        </div>
        <div className="field inline">
          <label>Channel</label>
          <select value={channel} onChange={(e) => resetPageThen(() => setChannel(e.target.value))}>
            <option value="">all</option>
            {channels.data?.map((c) => (
              <option key={c.channel_id ?? ""} value={c.channel_id ?? ""}>
                {(c.channel_title ?? c.channel_id ?? "?").slice(0, 28)} ({c.count})
              </option>
            ))}
          </select>
        </div>
        <div className="field inline">
          <label>Sort</label>
          <select value={sort} onChange={(e) => resetPageThen(() => setSort(e.target.value))}>
            {SORTS.map((s) => (
              <option key={s.v} value={s.v}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field inline">
          <label>comments</label>
          <select value={commentsState} onChange={(e) => resetPageThen(() => setCommentsState(e.target.value))}>
            {COMMENT_STATES.map((s) => (
              <option key={s} value={s}>
                {s || "any"}
              </option>
            ))}
          </select>
        </div>
        <div className="field inline">
          <label>live chat</label>
          <select value={liveState} onChange={(e) => resetPageThen(() => setLiveState(e.target.value))}>
            {LIVECHAT_STATES.map((s) => (
              <option key={s} value={s}>
                {s || "any"}
              </option>
            ))}
          </select>
        </div>
        <div className="field inline">
          <label>body</label>
          <select value={hasMedia} onChange={(e) => resetPageThen(() => setHasMedia(e.target.value))}>
            <option value="">any</option>
            <option value="yes">has body</option>
            <option value="no">no body</option>
          </select>
        </div>
        <button type="submit">Search</button>
        <button type="button" onClick={reload}>
          ↻ {loading && <span className="spin" />}
        </button>
      </form>

      <ErrorBox error={error} />

      {loading && !data ? (
        <Loading />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Video</th>
                <th>Channel</th>
                <th>Uploaded</th>
                <th>Dur</th>
                <th>comments</th>
                <th>live chat</th>
                <th className="num">body</th>
              </tr>
            </thead>
            <tbody>
              {data?.map((v) => (
                <tr key={v.id}>
                  <td className="wrap">
                    <div className="video-thumb-cell">
                      <Link to={`/videos/${v.id}`}>
                        <Thumb videoId={v.id} has={v.has_thumbnail} size="row" />
                      </Link>
                      <div style={{ minWidth: 0 }}>
                        <Link to={`/videos/${v.id}`}>{v.title ?? v.youtube_video_id}</Link>
                        <div className="muted mono small">{v.youtube_video_id}</div>
                      </div>
                    </div>
                  </td>
                  <td className="muted small">{v.channel_title ?? "—"}</td>
                  <td className="muted small">{fmtUploadDate(v.upload_date)}</td>
                  <td className="muted small">{fmtDuration(v.duration)}</td>
                  <td>
                    <StateBadge state={v.comments_state ?? "ok"} />
                  </td>
                  <td>
                    <StateBadge state={v.live_chat_state} />
                  </td>
                  <td className="num">
                    {v.media_files_count > 0 ? (
                      <span className="badge ok">{v.media_files_count}</span>
                    ) : (
                      <span className="muted">0</span>
                    )}
                  </td>
                </tr>
              ))}
              {data && data.length === 0 && (
                <tr>
                  <td colSpan={7} className="empty">
                    No videos found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <div className="pager">
        <button disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>
          ← Prev
        </button>
        <span className="muted small">page {page + 1}</span>
        <button disabled={!data || data.length < PAGE} onClick={() => setPage((p) => p + 1)}>
          Next →
        </button>
      </div>
    </div>
  );
}
