// Display formatting helpers (pure, unit-tested in src/test).

export function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours()
  )}:${pad(d.getMinutes())}`;
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

export function fmtDuration(sec: number | null | undefined): string {
  if (sec == null) return "—";
  const s = Math.floor(sec % 60);
  const m = Math.floor((sec / 60) % 60);
  const h = Math.floor(sec / 3600);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function fmtUploadDate(s: string | null | undefined): string {
  // yt-dlp upload_date is YYYYMMDD
  if (!s) return "—";
  if (/^\d{8}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
  return s;
}

export function fmtCount(n: number | null | undefined): string {
  if (n == null) return "0";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}K`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

export function statusKind(status: string): "ok" | "err" | "warn" | "run" | "muted" {
  switch (status) {
    case "success":
      return "ok";
    case "failed":
      return "err";
    case "partial_success":
      return "warn";
    case "running":
      return "run";
    case "queued":
      return "muted";
    case "canceled":
      return "muted";
    default:
      return "muted";
  }
}

export function stateKind(state: string | null | undefined): "ok" | "err" | "warn" | "muted" {
  if (!state) return "muted";
  if (state === "available") return "ok";
  if (state === "comments_disabled" || state === "unavailable") return "err";
  if (state === "frozen" || state === "not_available") return "warn";
  return "muted";
}
