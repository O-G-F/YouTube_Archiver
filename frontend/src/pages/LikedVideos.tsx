import { useState } from "react";
import { Link } from "react-router-dom";
import { api, LikedArchiveBody } from "../api/endpoints";
import { useFetch } from "../lib/useFetch";
import { useModalA11y } from "../lib/useModalA11y";
import { fmtDate } from "../lib/format";
import { ErrorBox, Loading } from "../components/ui";
import { Thumb } from "../components/Thumb";
import { LikedProgressDashboard } from "../components/LikedProgress";
import { LikedOperationsPanel } from "../components/LikedOperations";
import type { LikedArchivePlan, LikedArchiveEnqueueResult } from "../api/types";

const SOURCE_LABEL: Record<string, string> = {
  takeout_my_activity: "My Activity",
  takeout_youtube: "YT Takeout",
  takeout: "Takeout",
  youtube_data_api: "API",
};

type ModalKind = "metadata" | "archive" | null;

export default function LikedVideos() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [onlyMissingMeta, setOnlyMissingMeta] = useState(false);
  const [onlyMissingBody, setOnlyMissingBody] = useState(false);

  const stats = useFetch(() => api.likedVideosStats(), []);
  const { data, error, loading, reload } = useFetch(
    () =>
      api.likedVideos({
        q: query || undefined,
        source: source || undefined,
        only_missing_metadata: onlyMissingMeta,
        only_missing_body: onlyMissingBody,
        limit: 200,
      }),
    [query, source, onlyMissingMeta, onlyMissingBody]
  );

  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [plan, setPlan] = useState<LikedArchivePlan | null>(null);
  const [result, setResult] = useState<LikedArchiveEnqueueResult | null>(null);

  // confirm modal
  const [modal, setModal] = useState<ModalKind>(null);
  useModalA11y(modal !== null, () => setModal(null));
  const [mProfile, setMProfile] = useState("video_compressed_1080p_light");
  const [mLimit, setMLimit] = useState(10);
  const [allowLowDisk, setAllowLowDisk] = useState(false); // Phase 9A: override disk guard

  function baseFilters(): LikedArchiveBody {
    return {
      source: source || undefined,
      title: query || undefined,
      missing_metadata: onlyMissingMeta,
      missing_body: onlyMissingBody,
    };
  }

  async function runPlan() {
    setBusy("plan");
    setActionErr(null);
    setResult(null);
    try {
      const p = await api.likedArchivePlan({ ...baseFilters(), profile: mProfile });
      setPlan(p);
      setMLimit(p.recommended_limit || 10);
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  function openModal(kind: ModalKind) {
    setResult(null);
    setActionErr(null);
    setMProfile(kind === "metadata" ? "metadata_only" : plan?.recommended_profile || "video_compressed_1080p_light");
    setMLimit(plan?.recommended_limit || 10);
    setModal(kind);
  }

  async function submit(dryRun: boolean) {
    if (!modal) return;
    setBusy("enqueue");
    setActionErr(null);
    try {
      const body: LikedArchiveBody = {
        ...baseFilters(),
        profile: mProfile,
        limit: mLimit,
        dry_run: dryRun,
        // when targeting a specific job kind, default the matching missing-filter on
        missing_metadata: modal === "metadata" ? onlyMissingMeta || true : onlyMissingMeta,
        missing_body: modal === "archive" ? onlyMissingBody || true : onlyMissingBody,
        // Phase 9A: disk guard override applies to body archive only
        allow_low_disk: modal === "archive" ? allowLowDisk : undefined,
      };
      const r =
        modal === "metadata"
          ? await api.enqueueLikedMetadataV2(body)
          : await api.enqueueLikedArchive(body);
      setResult(r);
      if (r.blocked) {
        setActionErr(`DISK BLOCK: ${r.block_reason ?? "insufficient free space"} — 小さい limit にするか disk を増設してください。`);
      } else if (!dryRun) {
        setFlash(
          `${modal === "archive" ? "Archive" : "Metadata"} enqueue: created ${r.jobs_created} job(s)` +
            (r.downloads_body ? " (video BODY will be downloaded)" : " (metadata only, no body)")
        );
        setModal(null);
        reload();
      }
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function retryRetryable() {
    setBusy("retry");
    setActionErr(null);
    try {
      const r = await api.likedRetryFailed({ limit: 20 });
      setFlash(`Re-queued ${r.retried} retryable liked-archive job(s).`);
      reload();
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <div className="spread">
        <div>
          <h1 className="page-title">Liked videos</h1>
          <p className="page-sub">
            Imported from Google Takeout. Archive in <strong>small batches</strong> — metadata_only never downloads the
            body; a video profile <strong>does</strong>.
          </p>
        </div>
        <div className="row">
          <button disabled={busy === "plan"} onClick={runPlan}>
            {busy === "plan" ? <span className="spin" /> : "📋"} Plan archive
          </button>
          <button onClick={() => openModal("metadata")}>⤓ Enqueue metadata</button>
          <button onClick={() => openModal("archive")}>⬇ Enqueue archive…</button>
          <button disabled={busy === "retry"} onClick={retryRetryable}>
            {busy === "retry" ? <span className="spin" /> : "↻"} Retry retryable
          </button>
        </div>
      </div>

      <LikedProgressDashboard onChanged={reload} />

      <LikedOperationsPanel />

      <p className="muted small" style={{ margin: "10px 2px" }}>
        Runtime &amp; release status, backup / recovery readiness, and the audit trail
        moved to <Link to="/settings">System / Settings</Link>.
      </p>

      {stats.data && (
        <div className="cards">
          <div className="card"><div className="label">Total liked</div><div className="value">{stats.data.total}</div></div>
          <div className="card"><div className="label">Linked videos</div><div className="value sm">{stats.data.linked_videos}</div></div>
          <div className="card"><div className="label">Metadata fetched</div><div className="value sm">{stats.data.metadata_fetched}</div></div>
          <div className="card"><div className="label">Earliest</div><div className="value sm">{fmtDate(stats.data.earliest)}</div></div>
          <div className="card"><div className="label">Latest</div><div className="value sm">{fmtDate(stats.data.latest)}</div></div>
        </div>
      )}

      {plan && (
        <div className="panel">
          <div className="spread">
            <h2>Archive plan (preview — no jobs created)</h2>
            <button onClick={() => setPlan(null)}>✕</button>
          </div>
          {plan.blocked && (
            <div className="flash" style={{ background: "var(--err-bg, #3a1c1c)", marginBottom: 8 }}>
              ⛔ DISK BLOCK: {plan.block_reason} — 小さい limit にするか、disk を増設してください。
            </div>
          )}
          <div className="cards">
            <div className="card"><div className="label">candidates</div><div className="value">{plan.total_candidates}</div></div>
            <div className="card"><div className="label">missing metadata</div><div className="value sm">{plan.missing_metadata}</div></div>
            <div className="card"><div className="label">missing body</div><div className="value sm">{plan.missing_body}</div></div>
            <div className="card" title="missing body minus permanent (private/deleted/unavailable)"><div className="label">eligible missing body</div><div className="value sm">{plan.eligible_missing_body ?? "—"}</div></div>
            <div className="card"><div className="label">already have body</div><div className="value sm">{plan.has_body}</div></div>
            <div className="card"><div className="label">active jobs</div><div className="value sm">{plan.existing_active_jobs}</div></div>
          </div>
          <div className="cards" style={{ marginTop: 4 }}>
            <div className="card"><div className="label">requested / cap</div><div className="value sm">{plan.requested_limit ?? "—"} / {plan.cap_per_run ?? "—"}</div></div>
            <div className="card" title="max videos that fit while keeping min-free"><div className="label">disk-safe limit</div><div className="value sm">{plan.disk_safe_limit ?? "n/a"}</div></div>
            <div className="card"><div className="label">selected this run</div><div className="value sm">{plan.selected_count ?? "—"}</div></div>
            <div className="card" title={`limited by: ${plan.limiting_factor ?? "—"}`}><div className="label">rec. limit</div><div className="value sm">{plan.recommended_limit} <span className="muted">({plan.limiting_factor ?? "—"})</span></div></div>
            <div className="card"><div className="label">disk free</div><div className="value sm">
              <span className={`badge ${plan.disk_readable ? (plan.blocked ? "err" : "ok") : "warn"}`}>{plan.disk_readable ? `${plan.disk_free_gb} GiB` : "unreadable"}</span>
              <span className="muted"> min {plan.min_free_gb} GiB</span>
            </div></div>
            <div className="card" title={`estimate ${plan.size_estimate_source} (n=${plan.size_estimate_sample_count})`}><div className="label">est req / free after</div><div className="value sm">{plan.estimated_required_gb ?? "—"} / {plan.estimated_free_after_gb ?? "—"} GiB</div></div>
          </div>
          <ul className="muted small" style={{ lineHeight: 1.7 }}>
            {plan.notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </div>
      )}

      {flash && <div className="flash">{flash}</div>}
      <ErrorBox error={actionErr} />
      <ErrorBox error={error} />

      <form className="toolbar" onSubmit={(e) => { e.preventDefault(); setQuery(q); }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search title / channel / id" style={{ width: 220 }} />
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="">all sources</option>
          <option value="takeout_my_activity">My Activity</option>
          <option value="youtube_data_api">API</option>
          <option value="takeout_youtube">YT Takeout</option>
        </select>
        <label className="checkbox">
          <input type="checkbox" checked={onlyMissingMeta} onChange={(e) => setOnlyMissingMeta(e.target.checked)} />
          missing metadata
        </label>
        <label className="checkbox">
          <input type="checkbox" checked={onlyMissingBody} onChange={(e) => setOnlyMissingBody(e.target.checked)} />
          missing body
        </label>
        <button type="submit">Search</button>
        <button type="button" onClick={reload}>↻ {loading && <span className="spin" />}</button>
      </form>

      {loading && !data ? (
        <Loading />
      ) : data && data.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Video</th>
                <th>Channel</th>
                <th>Source</th>
                <th>Liked at</th>
                <th>Metadata</th>
                <th>Body</th>
                <th>Last job</th>
              </tr>
            </thead>
            <tbody>
              {data.map((lv) => (
                <tr key={lv.id}>
                  <td className="wrap">
                    <div className="video-thumb-cell">
                      {lv.video_id ? (
                        <Link to={`/videos/${lv.video_id}`}>
                          <Thumb videoId={lv.video_id} has={lv.has_metadata} size="row" />
                        </Link>
                      ) : (
                        <div className="thumb thumb-ph row">no id</div>
                      )}
                      <div style={{ minWidth: 0 }}>
                        {lv.video_id ? (
                          <Link to={`/videos/${lv.video_id}`}>{lv.title ?? "(metadata not fetched)"}</Link>
                        ) : (
                          <span>{lv.title ?? "(metadata not fetched)"}</span>
                        )}
                        <div className="muted mono small">{lv.youtube_video_id ?? "—"}</div>
                      </div>
                    </div>
                  </td>
                  <td className="muted small">{lv.channel_title ?? "—"}</td>
                  <td><span className="badge muted">{SOURCE_LABEL[lv.source ?? ""] ?? lv.source ?? "—"}</span></td>
                  <td className="muted small">{fmtDate(lv.liked_at)}</td>
                  <td>
                    {lv.has_metadata ? <span className="badge ok">fetched</span> : <span className="badge warn">未取得</span>}
                  </td>
                  <td>
                    {lv.has_body ? (
                      <span className="badge ok" title={`${lv.body_media_count} media file(s)`}>saved</span>
                    ) : (
                      <span className="badge muted">未保存</span>
                    )}
                  </td>
                  <td className="small">
                    {lv.latest_archive_job_id ? (
                      <Link to={`/jobs/${lv.latest_archive_job_id}`}>
                        <span className={`badge ${lv.latest_archive_job_status === "success" ? "ok" : lv.latest_archive_job_status === "failed" ? "err" : "muted"}`}>
                          {lv.latest_archive_job_status}
                        </span>
                      </Link>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty">
          No liked videos. Import a Takeout ZIP from the <Link to="/takeout">Takeout</Link> page.
        </div>
      )}

      {modal && (
        <div className="modal-overlay" role="dialog" aria-modal="true" onClick={() => setModal(null)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <h2>{modal === "archive" ? "Enqueue archive" : "Enqueue metadata"}</h2>
              <button onClick={() => setModal(null)}>✕</button>
            </div>
            {modal === "archive" ? (
              <div className="flash err" style={{ marginTop: 0 }}>
                ⚠ これは <strong>動画本体（video body）をダウンロード</strong>します。profile <code>{mProfile}</code> で少量ずつ実行してください（429 / Incomplete data に注意）。
              </div>
            ) : (
              <p className="muted small">metadata_only：<strong>動画本体は保存しません</strong>（info.json / description / thumbnail / subtitles のみ）。</p>
            )}
            <div className="form-grid">
              <label>profile
                {modal === "archive" ? (
                  <select value={mProfile} onChange={(e) => setMProfile(e.target.value)}>
                    <option value="video_compressed_1080p_light">video_compressed_1080p_light (推奨・comments無)</option>
                    <option value="video_compressed_1080p">video_compressed_1080p (comments有・大規模非推奨)</option>
                    <option value="video_proxy_1080p_mp4">video_proxy_1080p_mp4</option>
                    <option value="audio_opus_save_space">audio_opus_save_space</option>
                    <option value="audio_flac_best">audio_flac_best</option>
                  </select>
                ) : (
                  <input value={mProfile} readOnly />
                )}
              </label>
              <label>limit
                <input type="number" min={1} max={1000} value={mLimit} onChange={(e) => setMLimit(Number(e.target.value))} />
              </label>
            </div>
            {modal === "archive" && (
              <label className="checkbox" style={{ marginTop: 4 }}>
                <input type="checkbox" checked={allowLowDisk} onChange={(e) => setAllowLowDisk(e.target.checked)} />
                --allow-low-disk（disk guard を上書き・非推奨）
              </label>
            )}
            <p className="muted small">
              対象: source={source || "all"} / missing_metadata={String(onlyMissingMeta)} / missing_body={String(onlyMissingBody)}
              {query ? ` / q="${query}"` : ""}
            </p>
            {result && (
              <div className={`flash${result.blocked ? " err" : ""}`}>
                {result.blocked
                  ? `⛔ DISK BLOCK: ${result.block_reason ?? "insufficient free space"}`
                  : `${result.dry_run ? "DRY-RUN: " : ""}selected=${result.selected_count} created=${result.jobs_created} ` +
                    `skip_existing=${result.skipped_existing_job} skip_has_body=${result.skipped_already_has_body} ` +
                    `body=${String(result.downloads_body)}`}
              </div>
            )}
            <ErrorBox error={actionErr} />
            <div className="row" style={{ marginTop: 12 }}>
              <button disabled={busy === "enqueue"} onClick={() => submit(true)}>
                {busy === "enqueue" ? <span className="spin" /> : "🔍"} Dry-run
              </button>
              <button
                className={modal === "archive" ? "danger" : "primary"}
                disabled={busy === "enqueue"}
                onClick={() => submit(false)}
              >
                {busy === "enqueue" ? <span className="spin" /> : modal === "archive" ? "⬇ Download bodies" : "⤓ Create jobs"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
