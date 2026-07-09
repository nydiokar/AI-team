/**
 * Server-state hooks for the read-only A27 Work / Case API. Mirrors the
 * useLiveData polling discipline (TanStack Query) and translates raw payloads
 * through ../transport/workAdapter so components only see ../domain/work types.
 *
 * Everything here is READ-ONLY: no mutations, no optimistic writes. The Work
 * substrate only populates when the gateway runs with HARNESS_FLOW_DRIVE on;
 * until then these return empty lists — which the UI renders honestly.
 */
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../transport/apiClient";
import {
  toWorkList,
  toCaseDetail,
  toCaseTimeline,
  toCaseGraph,
  toSessionAffiliationIndex,
} from "../transport/workAdapter";
import type { SessionAffiliation, WorkBucket } from "../domain/work";
import { useAuthStore } from "../stores/authStore";

const EMPTY_AFFILIATIONS = new Map<string, SessionAffiliation>();

const POLL_MS = 3000;
// Case detail/lineage/affiliations change far less often than the live list, so
// we poll them gently. (The affiliation index is one whole-substrate query since
// A29 — no per-case fanout.)
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
 * A29 backs this with ONE whole-substrate endpoint
 * (`/api/work/affiliations/sessions`) — a single JOIN of the session flow_links
 * to their cases. This replaces the A28 approach (fetch each case's detail and
 * read `ledger.sessions`), which was O(N) requests AND capped at the first 100
 * cases: a session linked to a case beyond that window rendered a FALSE
 * "Standalone". Now every session link in the backlog resolves, regardless of
 * how large the case set grows.
 *
 * A session absent from the index has NO entry (the Sessions surface shows it as
 * standalone — never inferred). Multi-case links are deduplicated server-side to
 * the session's MOST RECENT case (the endpoint is newest-link-first); we never
 * fabricate a "primary" the substrate did not assert. With the substrate flag OFF
 * the response is empty (zero cost).
 */
export function useSessionAffiliations(): {
  index: Map<string, SessionAffiliation>;
  isLoading: boolean;
} {
  const token = useAuthStore((s) => s.token);
  const query = useQuery({
    queryKey: ["work-affiliations"],
    queryFn: async () => toSessionAffiliationIndex(await api.workAffiliations(token)),
    enabled: Boolean(token),
    refetchInterval: DETAIL_POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });

  return {
    index: query.data ?? EMPTY_AFFILIATIONS,
    isLoading: query.isLoading,
  };
}
