/**
 * Server-state hooks for the read-only A27 Work / Case API. Mirrors the
 * useLiveData polling discipline (TanStack Query) and translates raw payloads
 * through ../transport/workAdapter so components only see ../domain/work types.
 *
 * Everything here is READ-ONLY: no mutations, no optimistic writes. The Work
 * substrate only populates when the gateway runs with HARNESS_FLOW_DRIVE on;
 * until then these return empty lists — which the UI renders honestly.
 */
import { useMemo } from "react";
import { useQuery, useQueries } from "@tanstack/react-query";
import { api, ApiError } from "../transport/apiClient";
import {
  toWorkList,
  toCaseDetail,
  toCaseTimeline,
  toCaseGraph,
  normalizeSessionRole,
} from "../transport/workAdapter";
import type { CaseDetail, SessionAffiliation, WorkBucket } from "../domain/work";
import { useAuthStore } from "../stores/authStore";

const POLL_MS = 3000;
// Case detail/lineage change far less often than the live list; the affiliation
// index fans out one detail fetch per case, so we poll it gently.
const DETAIL_POLL_MS = 15000;

const retry = (count: number, err: unknown) =>
  !(err instanceof ApiError && [401, 500].includes(err.status)) && count < 3;

/** The Work inbox: case summaries + bucket tallies. Optionally one bucket. */
export function useWorkList(bucket?: WorkBucket, limit = 100) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["work-list", bucket ?? "all", limit],
    queryFn: async () => toWorkList(await api.work(token, { bucket, limit })),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    retry,
  });
}

/** One case's full detail (summary + ledger + parent/children). */
export function useWorkDetail(flowRunId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["work-detail", flowRunId],
    queryFn: async () => toCaseDetail(await api.workDetail(token, flowRunId!)),
    enabled: Boolean(token) && Boolean(flowRunId),
    refetchInterval: DETAIL_POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}

/** One case's append-only audit timeline + evidence pointers. */
export function useWorkTimeline(flowRunId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["work-timeline", flowRunId],
    queryFn: async () => toCaseTimeline(await api.workTimeline(token, flowRunId!)),
    enabled: Boolean(token) && Boolean(flowRunId),
    refetchInterval: DETAIL_POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}

/** One case's compact lineage graph (parent / self / children). */
export function useWorkGraph(flowRunId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["work-graph", flowRunId],
    queryFn: async () => toCaseGraph(await api.workGraph(token, flowRunId!)),
    enabled: Boolean(token) && Boolean(flowRunId),
    refetchInterval: DETAIL_POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}

/**
 * Authoritative session→case affiliation index.
 *
 * There is no bulk reverse (session→case) endpoint in A27, so we resolve each
 * case's detail and read its `ledger.sessions` links — the ONLY authoritative
 * source of a session's role in a case. A session absent from every ledger has
 * NO entry (the Sessions surface then shows it as standalone — never inferred).
 *
 * Cost: one cached detail fetch per case, shared by the same query key with
 * WorkDetail (so opening a case is warm) and polled gently. With the substrate
 * flag OFF the work list is empty, so this makes ZERO detail fetches.
 *
 * If a session is linked by more than one case (should not happen for an owned
 * role), the first resolved wins and we keep it stable — we never fabricate a
 * "primary" the substrate didn't assert.
 */
export function useSessionAffiliations(): {
  index: Map<string, SessionAffiliation>;
  isLoading: boolean;
} {
  const token = useAuthStore((s) => s.token);
  const list = useWorkList();
  const cases = list.data?.cases ?? [];

  const detailQueries = useQueries({
    queries: cases.map((c) => ({
      queryKey: ["work-detail", c.flowRunId],
      queryFn: async () =>
        toCaseDetail(await api.workDetail(token, c.flowRunId)),
      enabled: Boolean(token),
      refetchInterval: DETAIL_POLL_MS,
      staleTime: DETAIL_POLL_MS,
      retry,
    })),
  });

  const index = useMemo(() => {
    const map = new Map<string, SessionAffiliation>();
    for (const q of detailQueries) {
      const detail = q.data as CaseDetail | undefined;
      if (!detail) continue;
      for (const link of detail.ledger.sessions) {
        const sid = link.entityId;
        if (!sid || map.has(sid)) continue;
        map.set(sid, {
          sessionId: sid,
          flowRunId: detail.summary.flowRunId,
          role: normalizeSessionRole(link.role),
          caseTitle: detail.summary.title,
        });
      }
    }
    return map;
    // Re-derive when any detail payload changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailQueries.map((q) => q.dataUpdatedAt).join(",")]);

  return {
    index,
    isLoading: list.isLoading || detailQueries.some((q) => q.isLoading),
  };
}
