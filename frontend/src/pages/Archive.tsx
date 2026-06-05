import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "../components/ui";
import type { Profile } from "../api/types";

function ProfileSelect({
  profiles,
  value,
  onChange,
}: {
  profiles: Profile[];
  value: string;
  onChange: (v: string) => void;
}) {
  const sel = profiles.find((p) => p.name === value);
  return (
    <div>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">(default profile)</option>
        {profiles.map((p) => (
          <option key={p.name} value={p.name}>
            {p.name} — {p.media_mode}
          </option>
        ))}
      </select>
      {sel?.description && (
        <div className="muted small" style={{ marginTop: 4, maxWidth: 360 }}>
          {sel.description}
        </div>
      )}
    </div>
  );
}

export default function Archive() {
  const profiles = useFetch(() => api.profiles(), []);
  const plist = profiles.data ?? [];

  // single URL
  const [url, setUrl] = useState("");
  const [urlProfile, setUrlProfile] = useState("");
  // expand
  const [expandUrl, setExpandUrl] = useState("");
  const [expandProfile, setExpandProfile] = useState("");
  const [expandMax, setExpandMax] = useState("");
  // channel
  const [chUrl, setChUrl] = useState("");
  const [chProfile, setChProfile] = useState("");
  const [chVideos, setChVideos] = useState(true);
  const [chShorts, setChShorts] = useState(false);
  const [chStreams, setChStreams] = useState(false);
  const [chMax, setChMax] = useState("");

  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<{ ids: number[]; what: string } | null>(null);

  async function run(kind: string, fn: () => Promise<{ id: number } | { id: number }[]>) {
    setBusy(kind);
    setErr(null);
    setCreated(null);
    try {
      const res = await fn();
      const ids = Array.isArray(res) ? res.map((j) => j.id) : [res.id];
      setCreated({ ids, what: kind });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (profiles.loading && !profiles.data) return <Loading what="profiles" />;

  return (
    <div>
      <h1 className="page-title">Add / Archive</h1>
      <p className="page-sub">Register URLs, expand playlists/channels — jobs run on the worker.</p>

      <div className="panel">
        <h2>Profiles & outcomes</h2>
        <div className="kv">
          <div className="k mono">metadata_only</div>
          <div className="v small">info.json / 説明 / 字幕 / サムネのみ。<strong>本体は保存しない</strong>（Video Detail は「未保存」）。</div>
          <div className="k mono">video_compressed_1080p</div>
          <div className="v small">既定。1080p 以下・Web 再生向け。<strong>本体を保存</strong>するのでブラウザ再生可。</div>
          <div className="k mono">video_best_archive</div>
          <div className="v small">最高画質 mkv で長期保存（重い）。コメント/ライブチャット/サムネも保存。</div>
          <div className="k mono">subtitles_refresh_only</div>
          <div className="v small">字幕だけ再取得（本体は保存しない）。字幕が 429 で失敗したとき Job 詳細から実行。</div>
        </div>
        <p className="muted small" style={{ marginTop: 10 }}>
          ジョブが <span className="badge warn">partial_success</span> や{" "}
          <span className="badge warn">429</span> になることがあります。<strong>429</strong> は YouTube
          側の一時的なレート制限（主に字幕取得）で、Phase 上のブロッカーではありません。少し待って Jobs 画面から{" "}
          <strong>Retry</strong> してください。
        </p>
      </div>

      <ErrorBox error={err} />
      <ErrorBox error={profiles.error} />
      {created && (
        <div className="flash">
          Created {created.what} job(s):{" "}
          {created.ids.map((id, i) => (
            <span key={id}>
              {i > 0 && ", "}
              <Link to={`/jobs/${id}`}>#{id}</Link>
            </span>
          ))}
        </div>
      )}

      <div className="grid2">
        <div className="panel">
          <h2>Single video URL</h2>
          <div className="field">
            <label>YouTube URL or video id</label>
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://youtu.be/dQw4w9WgXcQ"
              style={{ width: "100%" }}
            />
          </div>
          <div className="field">
            <label>Profile</label>
            <ProfileSelect profiles={plist} value={urlProfile} onChange={setUrlProfile} />
          </div>
          <button
            className="primary"
            disabled={!url || busy === "download"}
            onClick={() => run("download", () => api.archiveUrl({ url, profile: urlProfile || undefined }))}
          >
            {busy === "download" ? <span className="spin" /> : "▶"} Archive
          </button>
        </div>

        <div className="panel">
          <h2>Expand playlist / channel URL</h2>
          <div className="field">
            <label>Playlist or channel(-tab) URL</label>
            <input
              value={expandUrl}
              onChange={(e) => setExpandUrl(e.target.value)}
              placeholder="https://www.youtube.com/playlist?list=…"
              style={{ width: "100%" }}
            />
          </div>
          <div className="row">
            <div className="field inline">
              <label>Profile</label>
              <ProfileSelect profiles={plist} value={expandProfile} onChange={setExpandProfile} />
            </div>
            <div className="field inline">
              <label>max_items</label>
              <input type="number" min={0} value={expandMax} onChange={(e) => setExpandMax(e.target.value)} style={{ width: 110 }} />
            </div>
          </div>
          <button
            className="primary"
            disabled={!expandUrl || busy === "expand"}
            onClick={() =>
              run("expand", () =>
                api.archiveExpand({
                  url: expandUrl,
                  profile: expandProfile || undefined,
                  max_items: expandMax ? Number(expandMax) : undefined,
                })
              )
            }
          >
            {busy === "expand" ? <span className="spin" /> : "▶"} Expand
          </button>
        </div>
      </div>

      <div className="panel">
        <h2>Add channel (tabs)</h2>
        <div className="field">
          <label>Channel URL (e.g. https://www.youtube.com/@example)</label>
          <input value={chUrl} onChange={(e) => setChUrl(e.target.value)} placeholder="https://www.youtube.com/@example" style={{ width: "100%" }} />
        </div>
        <div className="row">
          <label className="checkbox">
            <input type="checkbox" checked={chVideos} onChange={(e) => setChVideos(e.target.checked)} /> videos
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={chShorts} onChange={(e) => setChShorts(e.target.checked)} /> shorts
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={chStreams} onChange={(e) => setChStreams(e.target.checked)} /> streams
          </label>
          <div className="field inline">
            <label>Profile</label>
            <ProfileSelect profiles={plist} value={chProfile} onChange={setChProfile} />
          </div>
          <div className="field inline">
            <label>max_items</label>
            <input type="number" min={0} value={chMax} onChange={(e) => setChMax(e.target.value)} style={{ width: 110 }} />
          </div>
        </div>
        <button
          className="primary"
          disabled={!chUrl || (!chVideos && !chShorts && !chStreams) || busy === "channel"}
          onClick={() =>
            run("channel", () =>
              api.addChannel({
                url: chUrl,
                profile: chProfile || undefined,
                videos: chVideos,
                shorts: chShorts,
                streams: chStreams,
                max_items: chMax ? Number(chMax) : undefined,
              })
            )
          }
        >
          {busy === "channel" ? <span className="spin" /> : "▶"} Add channel
        </button>
        <p className="muted small" style={{ marginTop: 8 }}>
          At least one of videos / shorts / streams must be selected (a bare channel root needs an explicit tab).
        </p>
      </div>
    </div>
  );
}
