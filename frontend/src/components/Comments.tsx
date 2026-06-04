import { useState } from "react";
import type { Comment } from "../api/types";
import { fmtCount, fmtDate } from "../lib/format";

function initial(name: string | null): string {
  return (name || "?").replace(/^@/, "").charAt(0).toUpperCase();
}

function CommentItem({ c, replies, reply }: { c: Comment; replies?: Comment[]; reply?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const text = c.text || "";
  const long = text.length > 280;
  return (
    <div className={reply ? "comment reply" : "comment"}>
      <div className="avatar sm">{initial(c.author_name)}</div>
      <div className="body">
        <div className="head">
          <span className="author">{c.author_name || "—"}</span>
          <span className="meta">{fmtDate(c.published_at)}</span>
          {c.is_deleted_or_missing && <span className="badge warn">missing</span>}
        </div>
        <div className={"text" + (long && !expanded ? " clamped" : "")}>{text}</div>
        {long && (
          <button className="link-btn" onClick={() => setExpanded((e) => !e)}>
            {expanded ? "Show less" : "Read more"}
          </button>
        )}
        <div className="meta">♥ {fmtCount(c.like_count)}</div>
        {replies && replies.length > 0 && (
          <div className="replies">
            {replies.map((r) => (
              <CommentItem key={r.id} c={r} reply />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Best-effort threaded comments (parent/child where both are in the page). */
export function Comments({ comments }: { comments: Comment[] }) {
  const ids = new Set(comments.map((c) => c.comment_id));
  const byParent = new Map<string, Comment[]>();
  const tops: Comment[] = [];
  for (const c of comments) {
    const p = c.parent_comment_id;
    if (p && p !== "root" && ids.has(p)) {
      const arr = byParent.get(p) ?? [];
      arr.push(c);
      byParent.set(p, arr);
    } else {
      tops.push(c);
    }
  }
  if (comments.length === 0) {
    return <p className="muted small">No comments stored. Use “Comments refresh”.</p>;
  }
  return (
    <div>
      {tops.map((c) => (
        <CommentItem key={c.id} c={c} replies={byParent.get(c.comment_id)} />
      ))}
    </div>
  );
}
