import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { ErrorBox, Loading } from "../components/ui";
import { YouTubeDiagnostics } from "../components/YouTubeDiagnostics";
import { ReleaseInfoPanel } from "../components/ReleaseInfoPanel";
import { BackupReadinessPanel } from "../components/BackupReadinessPanel";
import { AuditPanel } from "../components/AuditPanel";
import { FirstRunChecklist } from "../components/FirstRunChecklist";

export default function Settings() {
  const settings = useFetch(() => api.settings(), []);
  const doctor = useFetch(() => api.doctor(), []);
  const sched = useFetch(() => api.schedulerStatus(), []);

  return (
    <div>
      <h1 className="page-title">System / Settings</h1>
      <p className="page-sub">Read-only. Secrets (cookies, tokens, DB/Redis credentials) are never shown.</p>

      <FirstRunChecklist forceShow />

      {/* System status (moved here from Liked videos in Phase 11B) */}
      <ReleaseInfoPanel />
      <BackupReadinessPanel />
      <AuditPanel />

      <YouTubeDiagnostics />

      <div className="panel">
        <div className="spread">
          <h2>Doctor</h2>
          <button onClick={doctor.reload}>↻ Re-run</button>
        </div>
        <ErrorBox error={doctor.error} />
        {doctor.loading && !doctor.data ? (
          <Loading />
        ) : doctor.data ? (
          <>
            <p>
              Overall:{" "}
              <span className={`badge ${doctor.data.ok ? "ok" : "err"}`}>{doctor.data.ok ? "healthy" : "issues"}</span>
            </p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Check</th>
                    <th>Result</th>
                    <th className="wrap">Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {doctor.data.checks.map((c) => (
                    <tr key={c.name}>
                      <td>{c.name}</td>
                      <td>
                        <span className={`badge ${c.ok ? "ok" : "err"}`}>{c.ok ? "ok" : "fail"}</span>
                      </td>
                      <td className="wrap mono small">{c.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </div>

      <div className="grid2">
        <div className="panel">
          <h2>Scheduler status</h2>
          <ErrorBox error={sched.error} />
          {sched.data && (
            <div className="kv">
              {Object.entries(sched.data).map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}>
                  <div className="k">{k}</div>
                  <div className="v">{String(v)}</div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="panel">
          <h2>Profiles</h2>
          <ErrorBox error={settings.error} />
          {settings.data && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Mode</th>
                    <th>Builtin</th>
                    <th className="wrap">Description</th>
                  </tr>
                </thead>
                <tbody>
                  {settings.data.profiles.map((p) => (
                    <tr key={p.name}>
                      <td className="mono small">{p.name}</td>
                      <td>{p.media_mode}</td>
                      <td>
                        <span className={`badge ${p.is_builtin ? "muted" : "run"}`}>{p.is_builtin ? "builtin" : "custom"}</span>
                      </td>
                      <td className="wrap small muted">{p.description ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <div className="panel">
        <h2>Configuration</h2>
        {settings.loading && !settings.data ? (
          <Loading />
        ) : settings.data ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Key</th>
                  <th className="wrap">Value</th>
                  <th className="wrap">Note</th>
                </tr>
              </thead>
              <tbody>
                {settings.data.items.map((it) => (
                  <tr key={it.key}>
                    <td className="mono small">{it.key}</td>
                    <td className="wrap mono small">{it.value}</td>
                    <td className="wrap small muted">{it.note ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>
    </div>
  );
}
