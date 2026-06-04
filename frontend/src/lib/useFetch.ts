import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";

export interface FetchState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Generic data hook. Re-runs when any value in `deps` changes. Optionally
 * polls every `pollMs` milliseconds (used by the Jobs screen).
 */
export function useFetch<T>(
  fn: () => Promise<T>,
  deps: unknown[] = [],
  pollMs?: number
): FetchState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const run = useCallback(async (showLoading: boolean) => {
    if (showLoading) setLoading(true);
    try {
      const result = await fnRef.current();
      setData(result);
      setError(null);
    } catch (e) {
      const msg =
        e instanceof ApiError ? `${e.detail}` : (e as Error).message || "request failed";
      setError(msg);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    run(true);
    if (pollMs && pollMs > 0) {
      const id = setInterval(() => run(false), pollMs);
      return () => clearInterval(id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps.concat(pollMs));

  return { data, error, loading, reload: () => run(true) };
}
