import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtBytes, fmtDate, fmtDuration, fmtUploadDate } from "../lib/format";
import { Bool, ErrorBox, KV, Loading, StateBadge, StatusBadge } from "../components/ui";
import type { MediaFile } from "../api/types";

function Player({ videoId, mf }: { videoId: number; mf: MediaFile }) {
  const src = `/api/videos/${videoId}/media/${mf.id}`;
  if (mf.media_type === "audio") {
    return <audio className="video-player" controls preload="none" src={src} />;
  }
  return <video className="video-player" controls preload="none" src={src} />;
}

export default function VideoDetail() {
  const { id } = useParams();
  const vid = Number(id);
  const video = useFetch(() => api.video(vid), [vid]);
  const cstats = useFetch(() => api.videoCommentStats(vid), [vid]);
  const comments = useFetch(() => api.videoComments(vid, { limit: 25 }), [vid]);
  const lcstats = useFetch(() => api.videoLiveChatStats(vid), [vid]);
  const livechat = useFetch(() => api.videoLiveChat(vid, { limit: 25 }), [vid]);
  const snapshots = useFetch(() => api.videoSnapshots(vid), [vid]);
  const jobs = useFetch(() => api.videoJobs(vid), [vid]);
  const collections = useFetch(() => api.videoCollections(vid), [vid]);

  const [flash, setFlash] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function refreshComments() {
    setBusy("comments");
    setActionErr(null);
    try {
      const j = await api.refreshVideoComments(vid);
      setFlash(`Created comments_refresh job #${j.id}.`);
      jobs.reload();
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }
  async function refreshLiveChat() {
    setBusy("livechat");
    setActionErr(null);
    try {
      const j = await api.refreshVideoLiveChat(vid);
      setFlash(`Created live_chat_refresh job #${j.id}.`);
      jobs.reload();
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (video.loading && !video.data) return <Loading what={`video #${vid}`} />;
  if (video.error) return <ErrorBox error={video.error} />;
  if (!video.data) return null;
  const v = video.data;
  const playable = v.media_files.find((m) => m.media_type === "video") || v.media_files[0];

  return (
    <div>
      <div className="spread">
        <h1 className="page-title">{v.title ?? v.youtube_video_id}</h1>
        <div className="row">
          <button disabled={busy === "comments"} onClick={refreshComments}>
            {busy === "comments" ? <span className="spin" /> : "↻"} Comments refresh
          </button>
          <button disabled={busy === "livechat"} onClick={refreshLiveChat}>
            {busy === "livechat" ? <span className="spin" /> : "↻"} Live chat refresh
          </button>
        </div>
      </div>
      <p className="page-sub">
        <Link to="/videos">← videos</Link> · <span className="mono">{v.youtube_video_id}</span> ·{" "}
        {v.url && (
          <a href={v.url} target="_blank" rel="noreferrer">
            open on YouTube ↗
          </a>
        )}
      </p>
      {flash && <div className="flash">{flash}</div>}
      <ErrorBox error={actionErr} />

      <div className="grid2">
        <div className="panel">
          <h2>Player</h2>
          {v.media_files.length > 0 && playable ? (
            <Player videoId={vid} mf={playable} />
          ) : (
            <div className="empty">未保存 — no media body downloaded for this video.</div>
          )}
          <h3>Media files ({v.media_files.length})</h3>
          {v.media_files.length === 0 ? (
            <span className="muted small">none</span>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Type</th>
                    <th>Profile</th>
                    <th>Container</th>
                    <th>Res</th>
                    <th>Size</th>
                    <th className="wrap">Path</th>
                  </tr>
                </thead>
                <tbody>
                  {v.media_files.map((m) => (
                    <tr key={m.id}>
                      <td>{m.media_type}</td>
                      <td className="muted small">{m.profile ?? "—"}</td>
                      <td>{m.container ?? "—"}</td>
                      <td className="small">{m.width && m.height ? `${m.width}×${m.height}` : "—"}</td>
                      <td className="small">{fmtBytes(m.filesize)}</td>
                      <td className="wrap mono small">{m.path}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="panel">
          <h2>Metadata</h2>
          <KV
            rows={[
              ["Channel", v.channel_title ?? "—"],
              ["Uploaded", fmtUploadDate(v.upload_date)],
              ["Duration", fmtDuration(v.duration)],
              ["Availability", v.availability ?? "—"],
              ["Short / Live", `${v.is_short ? "short" : "—"} / ${v.is_live ? "live" : "—"}`],
              ["Subtitles", String(v.subtitle_count)],
              ["First seen", fmtDate(v.first_seen_at)],
              ["Last metadata refresh", fmtDate(v.last_metadata_refresh_at)],
              ["comments_state", <StateBadge state={v.comments_state ?? "ok"} />],
              ["comments policy", v.comments_refresh_policy ?? "—"],
              ["next comments refresh", fmtDate(v.next_comments_refresh_at)],
              ["live_chat_state", <StateBadge state={v.live_chat_state} />],
              ["has live chat", <Bool value={v.has_live_chat} />],
              ["next live chat refresh", fmtDate(v.next_live_chat_refresh_at)],
            ]}
          />
        </div>
      </div>

      <div className="grid2">
        <div className="panel">
          <div className="spread">
            <h2>Comments</h2>
            {cstats.data && (
              <span className="muted small">
                total {cstats.data.total} · active {cstats.data.active} · missing {cstats.data.missing} · authors{" "}
                {cstats.data.distinct_authors}
              </span>
            )}
          </div>
          <ErrorBox error={comments.error} />
          {comments.loading ? (
            <Loading />
          ) : comments.data && comments.data.length > 0 ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="num">likes</th>
                    <th>author</th>
                    <th className="wrap">text</th>
                  </tr>
                </thead>
                <tbody>
                  {comments.data.map((c) => (
                    <tr key={c.id}>
                      <td className="num">{c.like_count ?? 0}</td>
                      <td className="small">
                        {c.author_name ?? "—"}
                        {c.is_deleted_or_missing && <span className="badge warn" style={{ marginLeft: 4 }}>missing</span>}
                      </td>
                      <td className="wrap small">{c.text ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted small">No comments stored. Use “Comments refresh”.</p>
          )}
        </div>

        <div className="panel">
          <div className="spread">
            <h2>Live chat</h2>
            {lcstats.data && (
              <span className="muted small">
                total {lcstats.data.total} · super chats {lcstats.data.superchats} · members{" "}
                {lcstats.data.member_messages} · <StateBadge state={lcstats.data.live_chat_state} />
              </span>
            )}
          </div>
          <ErrorBox error={livechat.error} />
          {livechat.loading ? (
            <Loading />
          ) : livechat.data && livechat.data.length > 0 ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>time</th>
                    <th>type</th>
                    <th>author</th>
                    <th className="wrap">message</th>
                  </tr>
                </thead>
                <tbody>
                  {livechat.data.map((m) => (
                    <tr key={m.id}>
                      <td className="small mono">{m.time_text ?? "—"}</td>
                      <td className="small">
                        {m.is_superchat ? (
                          <span className="badge ok">{m.amount_text ?? "super"}</span>
                        ) : m.is_member_message ? (
                          <span className="badge run">member</span>
                        ) : (
                          <span className="muted">text</span>
                        )}
                      </td>
                      <td className="small">{m.author_name ?? "—"}</td>
                      <td className="wrap small">{m.message ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted small">No live chat stored. Use “Live chat refresh”.</p>
          )}
        </div>
      </div>

      <div className="grid2">
        <div className="panel">
          <h2>Metadata snapshots</h2>
          {snapshots.data && snapshots.data.length > 0 ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Type</th>
                    <th>Fetched</th>
                    <th>Checksum</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshots.data.map((s) => (
                    <tr key={s.id}>
                      <td>#{s.id}</td>
                      <td>{s.snapshot_type}</td>
                      <td className="small muted">{fmtDate(s.fetched_at)}</td>
                      <td className="mono small">{s.checksum ? s.checksum.slice(0, 12) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted small">No snapshots.</p>
          )}
        </div>

        <div className="panel">
          <h2>Related</h2>
          <h3>Jobs ({jobs.data?.length ?? 0})</h3>
          <div className="tag-list">
            {jobs.data?.map((j) => (
              <Link key={j.id} to={`/jobs/${j.id}`} className="badge muted">
                #{j.id} {j.type} <StatusBadge status={j.status} />
              </Link>
            ))}
            {jobs.data && jobs.data.length === 0 && <span className="muted small">none</span>}
          </div>
          <h3>Collections ({collections.data?.length ?? 0})</h3>
          <div className="tag-list">
            {collections.data?.map((c) => (
              <Link key={c.id} to={`/collections/${c.id}`} className="badge muted">
                {c.type}: {c.title ?? `#${c.id}`}
              </Link>
            ))}
            {collections.data && collections.data.length === 0 && <span className="muted small">none</span>}
          </div>
        </div>
      </div>
    </div>
  );
}
