import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtDuration, fmtUploadDate } from "../lib/format";
import { ErrorBox, Loading, StateBadge } from "../components/ui";

const COMMENT_STATES = ["", "comments_disabled", "unavailable", "frozen"];
const LIVECHAT_STATES = ["", "available", "not_available", "unavailable", "frozen"];

export default function Videos() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [commentsState, setCommentsState] = useState("");
  const [liveState, setLiveState] = useState("");
  const [hasMedia, setHasMedia] = useState("");

  const { data, error, loading, reload } = useFetch(
    () =>
      api.videos({
        q: query || undefined,
        comments_state: commentsState || undefined,
        live_chat_state: liveState || undefined,
        has_media: hasMedia === "" ? undefined : hasMedia === "yes",
        limit: 200,
      }),
    [query, commentsState, liveState, hasMedia]
  );

  return (
    <div>
      <h1 className="page-title">Videos</h1>
      <p className="page-sub">Archived / registered videos and their refresh state.</p>

      <form
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(q);
        }}
      >
        <div className="field inline">
          <label>Search (title / channel / id)</label>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="rick astley…" style={{ width: 220 }} />
        </div>
        <div className="field inline">
          <label>comments_state</label>
          <select value={commentsState} onChange={(e) => setCommentsState(e.target.value)}>
            {COMMENT_STATES.map((s) => (
              <option key={s} value={s}>
                {s || "any"}
              </option>
            ))}
          </select>
        </div>
        <div className="field inline">
          <label>live_chat_state</label>
          <select value={liveState} onChange={(e) => setLiveState(e.target.value)}>
            {LIVECHAT_STATES.map((s) => (
              <option key={s} value={s}>
                {s || "any"}
              </option>
            ))}
          </select>
        </div>
        <div className="field inline">
          <label>body</label>
          <select value={hasMedia} onChange={(e) => setHasMedia(e.target.value)}>
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

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th className="wrap">Title</th>
              <th>Channel</th>
              <th>Uploaded</th>
              <th>Dur</th>
              <th>Avail</th>
              <th>comments</th>
              <th>live chat</th>
              <th className="num">body</th>
            </tr>
          </thead>
          <tbody>
            {data?.map((v) => (
              <tr key={v.id}>
                <td>
                  <Link to={`/videos/${v.id}`}>#{v.id}</Link>
                </td>
                <td className="wrap">
                  <Link to={`/videos/${v.id}`}>{v.title ?? v.youtube_video_id}</Link>
                  <div className="muted mono small">{v.youtube_video_id}</div>
                </td>
                <td className="muted small">{v.channel_title ?? "—"}</td>
                <td className="muted small">{fmtUploadDate(v.upload_date)}</td>
                <td className="muted small">{fmtDuration(v.duration)}</td>
                <td className="muted small">{v.availability ?? "—"}</td>
                <td>
                  <StateBadge state={v.comments_state ?? "ok"} />
                </td>
                <td>
                  <StateBadge state={v.live_chat_state} />
                </td>
                <td className="num">{v.media_files_count}</td>
              </tr>
            ))}
            {data && data.length === 0 && (
              <tr>
                <td colSpan={9} className="empty">
                  No videos found.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
