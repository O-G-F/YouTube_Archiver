import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox } from "./ui";

/**
 * Phase 11B: fresh-install setup checklist. Guides a first-time user through safe
 * setup steps with done / to-do status and safe deep-links — there are NO
 * dangerous auto-run controls. Shown automatically on a fresh install; pass
 * `forceShow` to display the ongoing setup status elsewhere (e.g. Settings).
 */
export function FirstRunChecklist({ forceShow = false }: { forceShow?: boolean }) {
  const fr = useFetch(() => api.firstRun(), []);
  const d = fr.data;
  if (!d) return null;
  if (!d.is_fresh && !forceShow) return null; // only intrude on a fresh install

  return (
    <div className="panel">
      <div className="spread">
        <h2>Getting started {d.is_fresh ? "" : `(${d.done_count}/${d.total_count})`}</h2>
        <button onClick={fr.reload} aria-label="Refresh setup checklist">
          ↻ {fr.loading && <span className="spin" />}
        </button>
      </div>
      <ErrorBox error={fr.error} />
      <p className="muted small">
        {d.is_fresh
          ? "This looks like a fresh install. Work through these steps to get set up — nothing here starts a download automatically."
          : "Setup status. Links open the relevant screen; nothing here starts a download automatically."}
      </p>
      {d.exposure_warning ? (
        <p className="row" role={d.exposure_level === "danger" ? "alert" : undefined}
           style={{ gap: 8, alignItems: "center", margin: "6px 0" }}>
          <span className={`badge ${d.exposure_level === "danger" ? "err" : "warn"}`}>
            {d.exposure_level === "danger" ? "⚠ exposed: auth disabled + 0.0.0.0" : "auth disabled"}
          </span>
          <span className="muted small">{d.exposure_note}</span>
        </p>
      ) : null}
      <ul style={{ listStyle: "none", padding: 0, margin: "8px 0 0" }}>
        {d.items.map((it) => (
          <li key={it.key} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "5px 0", borderTop: "1px solid var(--border)" }}>
            <span
              className={`badge ${it.done ? "ok" : it.danger ? "err" : it.warn ? "warn" : "muted"}`}
              aria-label={it.done ? "done" : it.danger ? "exposed" : it.warn ? "needs attention" : "to do"}
              style={{ minWidth: 22, textAlign: "center" }}
            >
              {it.done ? "✓" : it.danger ? "⚠" : it.warn ? "!" : "•"}
            </span>
            <span>
              <strong>{it.label}</strong>
              {it.optional ? <span className="muted small"> (optional)</span> : null}
              <span className="muted small"> — {it.detail}</span>{" "}
              {it.link ? <Link className="small" to={it.link}>open →</Link> : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
