import { useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type BackendAccountStatus } from "../transport/apiClient";

// Shared across sheets: rapid switching cannot create another status fan-out.
let running = 0;
const waiting = new Set<() => void>();
async function withStatusSlot<T>(signal: AbortSignal, load: () => Promise<T>): Promise<T> {
  await new Promise<void>((resolve, reject) => {
    const begin = () => {
      signal.removeEventListener("abort", abort);
      running++;
      resolve();
    };
    const abort = () => { waiting.delete(begin); reject(signal.reason); };
    if (signal.aborted) reject(signal.reason);
    else if (running < 4) begin();
    else { waiting.add(begin); signal.addEventListener("abort", abort, { once: true }); }
  });
  try { signal.throwIfAborted(); return await load(); }
  finally {
    running--;
    const next = waiting.values().next().value;
    if (next) { waiting.delete(next); next(); }
  }
}

export function useBackendAccountStatus(token: string, nodeId: string, backend: string, enabled = true) {
  const client = useQueryClient();
  const forceRefresh = useRef(false);
  const queryKey = ["backend-auth-status", backend, nodeId];
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => {
      const refresh = forceRefresh.current;
      forceRefresh.current = false;
      return withStatusSlot(signal, async () => {
        try { return await api.backendAuthStatus(token, nodeId, backend, signal, refresh); }
        catch (error) {
          if (signal.aborted) throw error;
          // A transport failure is also an unavailable observation. Keep the
          // same short backoff on reopen instead of retrying on every click.
          return { state: "unavailable" as const, checked_at: new Date().toISOString() };
        }
      });
    },
    enabled: Boolean(token) && enabled,
    staleTime: (query) => {
      const ttl = query.state.data?.state === "unavailable" ? 30_000 : 300_000;
      const observed = Date.parse(query.state.data?.checked_at ?? "");
      // A gateway cache hit must not buy another five minutes of freshness.
      return Number.isFinite(observed) ? Math.max(0, ttl - Math.max(0, query.state.dataUpdatedAt - observed)) : ttl;
    },
    refetchInterval: (query) => {
      const ttl = query.state.data?.state === "unavailable" || query.state.error ? 30_000 : 300_000;
      const observed = Date.parse(query.state.data?.checked_at ?? "");
      return Number.isFinite(observed) && !query.state.error ? Math.max(1000, ttl - Math.max(0, Date.now() - observed)) : ttl;
    },
    gcTime: 600_000,
    retry: false,
  });
  return {
    ...query,
    cancel: () => client.cancelQueries({ queryKey }),
    refresh: () => { forceRefresh.current = true; return query.refetch(); },
    record: (status: BackendAccountStatus) => client.setQueryData(queryKey, status),
  };
}
