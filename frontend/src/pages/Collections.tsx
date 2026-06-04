import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { Bool, ErrorBox, Loading } from "../components/ui";

const POLICIES = ["manual", "new_only", "refresh"];

export default function Collections() {
  const { data, error, loading, reload } = useFetch(() => api.collections(), []);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  async function withBusy(id: number, fn: () => Promise<unknown>, ok?: string) {
    setBusyId(id);
    setActionErr(null);
    try {
      await fn();
      if (ok) setFlash(ok);
      reload();
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div>
      <div className="spread">
        <div>
          <h1 className="page-title">Collections</h1>
          <p className="page-sub">Playlists & channels tracked for re-crawl.</p>
        </div>
        <button onClick={reload}>↻ Refresh</button>
      </div>
      {flash && <div className="flash">{flash}</div>}
      <ErrorBox error={error} />
      <ErrorBox error={actionErr} />

      {loading && !data ? (
        <Loading />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Type</th>
                <th className="wrap">Title</th>
                <th className="num">Items</th>
                <th>Enabled</th>
                <th>Policy</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {data?.map((c) => (
                <tr key={c.id}>
                  <td>
                    <Link to={`/collections/${c.id}`}>#{c.id}</Link>
                  </td>
                  <td>{c.type}</td>
                  <td className="wrap">
                    <Link to={`/collections/${c.id}`}>{c.title ?? c.url ?? `#${c.id}`}</Link>
                  </td>
                  <td className="num">{c.item_count}</td>
                  <td>
                    <Bool value={c.enabled} />
                  </td>
                  <td>
                    <select
                      value={c.crawl_policy ?? "new_only"}
                      disabled={busyId === c.id}
                      onChange={(e) =>
                        withBusy(c.id, () => api.patchCollection(c.id, { crawl_policy: e.target.value }), "Policy updated.")
                      }
                    >
                      {POLICIES.map((p) => (
                        <option key={p} value={p}>
                          {p}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <div className="row">
                      {c.enabled ? (
                        <button className="sm" disabled={busyId === c.id} onClick={() => withBusy(c.id, () => api.disableCollection(c.id))}>
                          Disable
                        </button>
                      ) : (
                        <button className="sm" disabled={busyId === c.id} onClick={() => withBusy(c.id, () => api.enableCollection(c.id))}>
                          Enable
                        </button>
                      )}
                      <button
                        className="sm"
                        disabled={busyId === c.id}
                        onClick={() => withBusy(c.id, () => api.refreshCollection(c.id), `Re-crawl job created for #${c.id}.`)}
                      >
                        Refresh
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {data && data.length === 0 && (
                <tr>
                  <td colSpan={7} className="empty">
                    No collections. Add a playlist/channel from “Add / Archive”.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
