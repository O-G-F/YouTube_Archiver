import type { LiveChatMessage } from "../api/types";

export function LiveChat({ messages }: { messages: LiveChatMessage[] }) {
  if (messages.length === 0) {
    return <p className="muted small">No live chat stored. Use “Live chat refresh”.</p>;
  }
  return (
    <div>
      {messages.map((m) => {
        const cls = m.is_superchat ? "superchat" : m.is_member_message ? "member" : "";
        return (
          <div key={m.id} className={`lc-row ${cls}`}>
            <span className="t">{m.time_text ?? "—"}</span>
            {m.is_superchat && m.amount_text && <span className="lc-amount">{m.amount_text}</span>}
            {m.is_member_message && !m.is_superchat && <span className="badge run">member</span>}
            <span className="a">{m.author_name ?? "—"}</span>
            <span style={{ flex: 1, wordBreak: "break-word" }}>
              {m.message ?? ""}
              {m.is_deleted_or_missing && <span className="badge warn" style={{ marginLeft: 6 }}>missing</span>}
            </span>
          </div>
        );
      })}
    </div>
  );
}
