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
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      headers: { Accept: "application/json", ...(init?.headers || {}) },
      ...init,
    });
  } catch (e) {
    throw new ApiError(0, `network error: ${(e as Error).message}`, null);
  }
  const body = await parse(res).catch(() => null);
  if (!res.ok) {
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
