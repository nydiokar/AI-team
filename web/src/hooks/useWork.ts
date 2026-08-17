/**
 * Server-state hooks for the read-only A27 Work / Case API. Same tooling as
 * useLiveData (TanStack Query, token-gated, retry-on-non-auth-error) but its
 * OWN polling tiers, not a mirror of useLiveData's POLL_MS/SLOW_POLL_MS: the
 * inbox list polls at POLL_MS (3s) like the rest of the live surface, but case
 * detail/lineage/timeline/affiliations poll at the slower DETAIL_POLL_MS (15s,
 * see below) since they change far less often. Raw payloads are translated
 * through ../transport/workAdapter so components only see ../domain/work types.
 *
 * Most hooks here are read-only projections. The orphan sweep mutation is the
 * narrow operator maintenance path for stale open Cases; it writes through the
 * backend interrupt path and then invalidates these projections.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../transport/apiClient";
import {
  toWorkList,
  toCaseDetail,
  toCaseTimeline,
  toCaseGraph,
  toCaseRoster,
  toSessionAffiliationIndex,
} from "../transport/workAdapter";
import type { SessionAffiliation, WorkBucket } from "../domain/work";
import type {
  RawCaseOrphanSweepResponse,
  RawCaseResumeResponse,
} from "../transport/rawApi";
import { useAuthStore } from "../stores/authStore";

const EMPTY_AFFILIATIONS = new Map<string, SessionAffiliation>();

const POLL_MS = 3000;
// Case detail/lineage/affiliations change far less often than the live list, so
// we poll them gently. (The affiliation index is one whole-substrate query since
// A29 — no per-case fanout.)
const DETAIL_POLL_MS = 15000;
// The roster is the LIVE head of the case — who is working right now and which
// scripts are running — so it polls on the fast tier, not the gentle detail tier.
const LIVE_POLL_MS = 5000;

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

/** One case's LIVE roster: sessions (manager/workers) with tokens/turns + the
 *  watch_job scripts they own. The operational "who is doing what right now" head
 *  of the case; polls on the fast (5s) tier. */
export function useWorkRoster(flowRunId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["work-roster", flowRunId],
    queryFn: async () => toCaseRoster(await api.workRoster(token, flowRunId!)),
    enabled: Boolean(token) && Boolean(flowRunId),
    refetchInterval: LIVE_POLL_MS,
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

/** Dry-run or block open Cases whose Manager session is gone/inactive. */
export function useSweepOrphanedCases() {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  return useMutation<
    RawCaseOrphanSweepResponse,
    ApiError,
    { dryRun?: boolean; limit?: number; reason?: string }
  >({
    mutationFn: (vars) => api.sweepCaseOrphans(token, vars),
    retry: false,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["work-list"] });
      qc.invalidateQueries({ queryKey: ["work-detail"] });
      qc.invalidateQueries({ queryKey: ["work-timeline"] });
      qc.invalidateQueries({ queryKey: ["work-roster"] });
      qc.invalidateQueries({ queryKey: ["work-affiliations"] });
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
}

/** Operator state control for one non-terminal Case. */
export function useSetCaseState(flowRunId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  return useMutation<
    { ok: boolean; changed?: boolean; status: string; reason?: string },
    ApiError,
    { state: "open" | "blocked"; reason?: string }
  >({
    mutationFn: (vars) => api.setCaseState(token, flowRunId!, vars),
    retry: false,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["work-list"] });
      qc.invalidateQueries({ queryKey: ["work-detail", flowRunId] });
      qc.invalidateQueries({ queryKey: ["work-timeline", flowRunId] });
      qc.invalidateQueries({ queryKey: ["work-roster", flowRunId] });
      qc.invalidateQueries({ queryKey: ["work-affiliations"] });
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
}

/** Manual operator close for stale/non-terminal Cases. */
export function useCloseCaseManually(flowRunId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  return useMutation<
    { ok: boolean; closed: boolean; reason: string | null },
    ApiError,
    { reason?: string }
  >({
    mutationFn: (vars) => api.closeCaseManually(token, flowRunId!, vars),
    retry: false,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["work-list"] });
      qc.invalidateQueries({ queryKey: ["work-detail", flowRunId] });
      qc.invalidateQueries({ queryKey: ["work-timeline", flowRunId] });
      qc.invalidateQueries({ queryKey: ["work-roster", flowRunId] });
      qc.invalidateQueries({ queryKey: ["work-affiliations"] });
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
}

/**
 * [quota-resume] One Case's resume state: is it paused on quota, when does the
 * window reopen, what would resuming cost, and is a decision already pending.
 *
 * Polls on the LIVE tier, not the gentle detail tier: while a Case is paused
 * this is the surface the operator is watching for "can I go now?", and the
 * backing read is three bounded indexed queries.
 */
export function useCaseResumeState(caseId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["case-resume-state", caseId],
    queryFn: () => api.caseResumeState(token, caseId!),
    enabled: Boolean(token) && Boolean(caseId),
    refetchInterval: LIVE_POLL_MS,
    placeholderData: (prev) => prev,
    retry,
  });
}

/**
 * [quota-resume] Resume a Case now. The SAME server-side leased path the
 * automatic quota-restore uses, which is what makes "I resumed it myself and
 * then the engine resumed it too" structurally impossible — the loser gets
 * `resume_in_flight` (409) instead of a second Manager.
 */
export function useResumeCase(caseId: string | undefined) {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  return useMutation<RawCaseResumeResponse, ApiError, { mode?: string }>({
    mutationFn: (vars) => api.resumeCase(token, caseId!, vars),
    retry: false,
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["case-resume-state", caseId] });
      qc.invalidateQueries({ queryKey: ["work-detail", caseId] });
      qc.invalidateQueries({ queryKey: ["work-timeline", caseId] });
      qc.invalidateQueries({ queryKey: ["work-roster", caseId] });
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
}

/**
 * [quota-resume] Pending Case-level resume proposals, RAW.
 *
 * Deliberately not `useApprovals` (which maps to the domain ApprovalRequest and
 * drops `payload`): the decision the operator makes here — resume this Case or
 * not — is carried entirely in that payload (case id, objective excerpt, cost
 * estimate, quota reset). One shared endpoint, no per-case fanout.
 */
export function useCaseResumeApprovals() {
  const token = useAuthStore((s) => s.token);
  return useQuery({
    queryKey: ["approvals", "pending", "case_resume"],
    queryFn: async () =>
      (await api.approvals(token, "pending")).filter((a) => a.action === "case_resume"),
    enabled: Boolean(token),
    refetchInterval: POLL_MS,
    retry,
  });
}
