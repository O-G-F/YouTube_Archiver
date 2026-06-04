import { Link } from "react-router-dom";
import type { VideoListItem } from "../api/types";
import { fmtDuration } from "../lib/format";
import { Thumb } from "./Thumb";

export function RelatedList({ videos }: { videos: VideoListItem[] }) {
  if (videos.length === 0) return <p className="muted small">none</p>;
  return (
    <div>
      {videos.map((v) => (
        <Link key={v.id} to={`/videos/${v.id}`} className="related-card">
          <Thumb videoId={v.id} has={v.has_thumbnail} size="sm" />
          <div style={{ minWidth: 0 }}>
            <div className="rc-title">{v.title ?? v.youtube_video_id}</div>
            <div className="rc-sub">
              {v.channel_title ?? "—"}
              {v.media_files_count > 0 ? " · ▶ saved" : " · meta only"}
              {v.duration ? ` · ${fmtDuration(v.duration)}` : ""}
            </div>
          </div>
        </Link>
      ))}
    </div>
  );
}
