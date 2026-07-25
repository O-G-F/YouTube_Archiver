// Tiny fetch wrapper. All paths are relative ("/api/...") so the same build
// works in dev (Vite proxies /api) and in production (served same-origin by
// FastAPI). Override the origin with VITE_API_BASE if the API lives elsewhere.

const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export class ApiError extends Error {
  status: number;
  detail: string;
  body: unknown;
  constructor(status: number, detail: string, body: unknown) {
    super(`HTTP ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

async function parse(res: Response): Promise<unknown> {
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res.text();
}

// Phase 9C: read the double-submit CSRF token cookie and echo it on mutations.
const CSRF_COOKIE = "ytarch_csrf";
function readCookie(name: string): string | null {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : null;
}

// A global handler the app registers so any 401 (expired/absent session) routes
// the user back to the login screen.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}
const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function extractDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    return JSON.stringify(d);
  }
  if (typeof body === "string" && body) return body.slice(0, 500);
  return fallback;
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method || "GET").toUpperCase();
  const extraHeaders: Record<string, string> = {};
  if (MUTATING.has(method)) {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) extraHeaders["X-CSRF-Token"] = csrf;
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      credentials: "same-origin", // send the session cookie
      ...init,
      headers: { Accept: "application/json", ...extraHeaders, ...(init?.headers || {}) },
    });
  } catch (e) {
    throw new ApiError(0, `network error: ${(e as Error).message}`, null);
  }
  const body = await parse(res).catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && onUnauthorized) onUnauthorized();
    throw new ApiError(res.status, extractDetail(body, res.statusText), body);
  }
  return body as T;
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path);
}

export function apiPost<T>(path: string, json?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: json !== undefined ? { "Content-Type": "application/json" } : {},
    body: json !== undefined ? JSON.stringify(json) : undefined,
  });
}

export function apiPatch<T>(path: string, json?: unknown): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(json ?? {}),
  });
}

// Plain-text fetch (job log streams).
export async function apiText(path: string): Promise<string> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new ApiError(res.status, res.statusText, null);
  return res.text();
}
