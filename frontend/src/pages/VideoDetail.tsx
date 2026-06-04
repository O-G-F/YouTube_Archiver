import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, mediaUrl } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { fmtBytes, fmtDate, fmtDuration, fmtUploadDate } from "../lib/format";
import { Bool, ErrorBox, KV, Loading, StateBadge } from "../components/ui";
import { JobBadges } from "../components/JobBadges";
import { Comments } from "../components/Comments";
import { LiveChat } from "../components/LiveChat";
import { RelatedList } from "../components/RelatedList";
import type { MediaFile, VideoDetail as VideoDetailT } from "../api/types";

type Tab = "comments" | "livechat" | "details";

function Player({ videoId, v }: { videoId: number; v: VideoDetailT }) {
  const body =
    v.media_files.find((m) => m.media_type === "video") ||
    v.media_files.find((m) => m.media_type === "audio");
  if (!body) {
    return (
      <div className="player-shell">
        <div className="player-empty">
          <div style={{ fontSize: 30 }}>🎞️</div>
          <strong>未保存</strong>
          <span className="small">No media body downloaded — use a video/audio profile to archive it.</span>
        </div>
      </div>
    );
  }
  const src = mediaUrl(videoId, body.id);
  if (body.media_type === "audio") {
    return (
      <div className="player-shell audio">
        <audio controls preload="metadata" src={src} />
      </div>
    );
  }
  return (
    <div className="player-shell">
      <video controls preload="metadata" src={src} />
    </div>
  );
}

function ChannelAvatar({ title }: { title: string | null }) {
  return <div className="avatar">{(title || "?").charAt(0).toUpperCase()}</div>;
}

export default function VideoDetail() {
  const { id } = useParams();
  const vid = Number(id);
  const video = useFetch(() => api.video(vid), [vid]);
  const related = useFetch(() => api.videoRelated(vid), [vid]);
  const cstats = useFetch(() => api.videoCommentStats(vid), [vid]);
  const comments = useFetch(() => api.videoComments(vid, { limit: 100 }), [vid]);
  const lcstats = useFetch(() => api.videoLiveChatStats(vid), [vid]);
  const livechat = useFetch(() => api.videoLiveChat(vid, { limit: 200 }), [vid]);
  const snapshots = useFetch(() => api.videoSnapshots(vid), [vid]);
  const jobs = useFetch(() => api.videoJobs(vid), [vid]);
  const collections = useFetch(() => api.videoCollections(vid), [vid]);

  const [tab, setTab] = useState<Tab>("comments");
  const [descOpen, setDescOpen] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function refresh(kind: "comments" | "livechat") {
    setBusy(kind);
    setActionErr(null);
    try {
      const j = kind === "comments" ? await api.refreshVideoComments(vid) : await api.refreshVideoLiveChat(vid);
      setFlash(`Created ${kind === "comments" ? "comments_refresh" : "live_chat_refresh"} job #${j.id} (runs on the worker).`);
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

  return (
    <div>
      <p className="page-sub" style={{ marginBottom: 12 }}>
        <Link to="/videos">← videos</Link>
      </p>
      {flash && <div className="flash">{flash}</div>}
      <ErrorBox error={actionErr} />

      <div className="watch-layout">
        {/* ---- main column ---- */}
        <div>
          <Player videoId={vid} v={v} />

          <h1 className="video-title">{v.title ?? v.youtube_video_id}</h1>
          <div className="video-subline">
            <span className="channel-chip">
              <ChannelAvatar title={v.channel_title} />
              {v.channel_title ?? "Unknown channel"}
            </span>
            <span>{fmtUploadDate(v.upload_date)}</span>
            <span>· {fmtDuration(v.duration)}</span>
            {v.availability && <span>· {v.availability}</span>}
            <span className="mono small">· {v.youtube_video_id}</span>
            <span className="right" />
            {v.url && (
              <a href={v.url} target="_blank" rel="noreferrer">
                YouTube ↗
              </a>
            )}
            <button disabled={busy === "comments"} onClick={() => refresh("comments")}>
              {busy === "comments" ? <span className="spin" /> : "↻"} Comments
            </button>
            <button disabled={busy === "livechat"} onClick={() => refresh("livechat")}>
              {busy === "livechat" ? <span className="spin" /> : "↻"} Live chat
            </button>
          </div>

          <DescriptionBlock v={v} open={descOpen} setOpen={setDescOpen} />

          <div className="tabs" style={{ marginTop: 18 }}>
            <button className={tab === "comments" ? "active" : ""} onClick={() => setTab("comments")}>
              Comments {cstats.data ? `(${cstats.data.active})` : ""}
            </button>
            <button className={tab === "livechat" ? "active" : ""} onClick={() => setTab("livechat")}>
              Live chat {lcstats.data ? `(${lcstats.data.active})` : ""}
            </button>
            <button className={tab === "details" ? "active" : ""} onClick={() => setTab("details")}>
              Details
            </button>
          </div>

          {tab === "comments" && (
            <div className="panel">
              <div className="spread">
                <h2>Comments</h2>
                {cstats.data && (
                  <span className="muted small">
                    total {cstats.data.total} · active {cstats.data.active} · missing {cstats.data.missing} · authors{" "}
                    {cstats.data.distinct_authors} · <StateBadge state={cstats.data.comments_state ?? "ok"} />
                  </span>
                )}
              </div>
              <ErrorBox error={comments.error} />
              {comments.loading ? <Loading /> : <Comments comments={comments.data ?? []} />}
            </div>
          )}

          {tab === "livechat" && (
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
              {lcstats.data && lcstats.data.live_chat_state === "not_available" && (
                <div className="flash" style={{ borderColor: "var(--warn)" }}>
                  この動画にはライブチャットがありません（<code>not_available</code>）。通常動画では正常な状態です。
                </div>
              )}
              <ErrorBox error={livechat.error} />
              {livechat.loading ? <Loading /> : <LiveChat messages={livechat.data ?? []} />}
            </div>
          )}

          {tab === "details" && (
            <DetailsTab
              v={v}
              snapshots={snapshots.data ?? []}
              jobs={jobs.data ?? []}
              collections={collections.data ?? []}
            />
          )}
        </div>

        {/* ---- sidebar: related ---- */}
        <aside>
          <div className="panel">
            <h2>Same channel</h2>
            {related.loading ? <Loading /> : <RelatedList videos={related.data?.same_channel ?? []} />}
          </div>
          <div className="panel">
            <h2>From the same collection</h2>
            {related.loading ? <Loading /> : <RelatedList videos={related.data?.same_collection ?? []} />}
          </div>
        </aside>
      </div>
    </div>
  );
}

function DescriptionBlock({
  v,
  open,
  setOpen,
}: {
  v: VideoDetailT;
  open: boolean;
  setOpen: (b: boolean) => void;
}) {
  const desc = v.description ?? null;
  if (!desc) return null;
  const long = desc.length > 220;
  return (
    <div>
      <div className={"description" + (long && !open ? " clamped" : "")}>{desc}</div>
      {long && (
        <button className="link-btn" onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

function MediaTable({ files, videoId }: { files: MediaFile[]; videoId: number }) {
  if (files.length === 0) return <span className="muted small">none</span>;
  return (
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
            <th></th>
          </tr>
        </thead>
        <tbody>
          {files.map((m) => (
            <tr key={m.id}>
              <td>{m.media_type}</td>
              <td className="muted small">{m.profile ?? "—"}</td>
              <td>{m.container ?? "—"}</td>
              <td className="small">{m.width && m.height ? `${m.width}×${m.height}` : "—"}</td>
              <td className="small">{fmtBytes(m.filesize)}</td>
              <td className="wrap mono small">{m.path}</td>
              <td>
                {(m.media_type === "video" || m.media_type === "audio") && (
                  <a className="btn sm" href={mediaUrl(videoId, m.id)} target="_blank" rel="noreferrer">
                    open
                  </a>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DetailsTab({
  v,
  snapshots,
  jobs,
  collections,
}: {
  v: VideoDetailT;
  snapshots: import("../api/types").MetadataSnapshot[];
  jobs: import("../api/types").Job[];
  collections: import("../api/types").Collection[];
}) {
  return (
    <>
      <div className="grid2">
        <div className="panel">
          <h2>Metadata</h2>
          <KV
            rows={[
              ["Channel", v.channel_title ?? "—"],
              ["Uploaded", fmtUploadDate(v.upload_date)],
              ["Duration", fmtDuration(v.duration)],
              ["Availability", v.availability ?? "—"],
              ["Subtitles", String(v.subtitle_count)],
              ["First seen", fmtDate(v.first_seen_at)],
              ["comments_state", <StateBadge state={v.comments_state ?? "ok"} />],
              ["next comments refresh", fmtDate(v.next_comments_refresh_at)],
              ["live_chat_state", <StateBadge state={v.live_chat_state} />],
              ["has live chat", <Bool value={v.has_live_chat} />],
              ["next live chat refresh", fmtDate(v.next_live_chat_refresh_at)],
            ]}
          />
        </div>
        <div className="panel">
          <h2>Media files ({v.media_files.length})</h2>
          <MediaTable files={v.media_files} videoId={v.id} />
        </div>
      </div>

      <div className="grid2">
        <div className="panel">
          <h2>Snapshots</h2>
          {snapshots.length > 0 ? (
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
                  {snapshots.map((s) => (
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
          <h3>Jobs ({jobs.length})</h3>
          <div className="tag-list">
            {jobs.map((j) => (
              <Link key={j.id} to={`/jobs/${j.id}`} className="badge muted">
                #{j.id} {j.type} <JobBadges job={j} />
              </Link>
            ))}
            {jobs.length === 0 && <span className="muted small">none</span>}
          </div>
          <h3>Collections ({collections.length})</h3>
          <div className="tag-list">
            {collections.map((c) => (
              <Link key={c.id} to={`/collections/${c.id}`} className="badge muted">
                {c.type}: {c.title ?? `#${c.id}`}
              </Link>
            ))}
            {collections.length === 0 && <span className="muted small">none</span>}
          </div>
        </div>
      </div>
    </>
  );
}
