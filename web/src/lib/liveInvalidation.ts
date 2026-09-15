import type { QueryClient } from "@tanstack/react-query";
import type { RawEvent } from "../transport/rawApi";

function textField(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export interface LiveInvalidationTarget {
  sessions: Set<string>;
  cases: Set<string>;
  tasks: boolean;
  approvals: boolean;
}

export function collectLiveInvalidationTargets(
  raws: RawEvent[],
): LiveInvalidationTarget {
  const target: LiveInvalidationTarget = {
    sessions: new Set<string>(),
    cases: new Set<string>(),
    tasks: false,
    approvals: false,
  };

  for (const raw of raws) {
    const sessionId = textField(raw.session_id);
    if (sessionId) target.sessions.add(sessionId);

    const caseId =
      textField(raw.case_id) ??
      textField(raw.flow_run_id) ??
      textField(raw.current_case_id);
    if (caseId) target.cases.add(caseId);

    if (textField(raw.task_id)) target.tasks = true;
    if (String(raw.event ?? "").startsWith("approval")) target.approvals = true;
  }

  return target;
}

export function invalidateLiveTargets(
  queryClient: QueryClient,
  target: LiveInvalidationTarget,
): void {
  if (target.sessions.size > 0 || target.tasks) {
    queryClient.invalidateQueries({ queryKey: ["sessions"] });
  }
  if (target.tasks) {
    queryClient.invalidateQueries({ queryKey: ["tasks"] });
    queryClient.invalidateQueries({ queryKey: ["task-sections"] });
    queryClient.invalidateQueries({ queryKey: ["jobs"] });
    // A81: a new artifact (results/<task>.json) appears exactly when a task
    // produces a result — the same moment a task_id-bearing event fires. Without
    // this the artifacts list had NO covering event and would go stale once its
    // aggressive poll dropped to the safety net.
    queryClient.invalidateQueries({ queryKey: ["artifacts"] });
  }
  if (target.approvals) {
    queryClient.invalidateQueries({ queryKey: ["approvals"] });
  }

  for (const sessionId of target.sessions) {
    queryClient.invalidateQueries({ queryKey: ["session-messages", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["session-turns", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["session-usage", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["session-activity", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["work-affiliations"] });
    queryClient.invalidateQueries({ queryKey: ["jobs"] });
  }

  for (const caseId of target.cases) {
    queryClient.invalidateQueries({ queryKey: ["work-list"] });
    queryClient.invalidateQueries({ queryKey: ["work-detail", caseId] });
    queryClient.invalidateQueries({ queryKey: ["work-timeline", caseId] });
    queryClient.invalidateQueries({ queryKey: ["work-graph", caseId] });
    queryClient.invalidateQueries({ queryKey: ["work-roster", caseId] });
    queryClient.invalidateQueries({ queryKey: ["case-resume-state", caseId] });
    queryClient.invalidateQueries({ queryKey: ["work-affiliations"] });
  }
}

/**
 * Live read-model query-key prefixes — the set the event bridge keeps fresh.
 * Used by the reconnect-resync to refetch everything visible after an SSE gap.
 * react-query only refetches ACTIVE (mounted) queries, so a broad invalidate here
 * is bounded to what's actually on screen.
 */
const LIVE_QUERY_KEYS: readonly (readonly [string])[] = [
  ["sessions"],
  ["tasks"],
  ["task-sections"],
  ["jobs"],
  ["approvals"],
  ["artifacts"],
  ["session-messages"],
  ["session-turns"],
  ["session-usage"],
  ["session-activity"],
  ["work-list"],
  ["work-detail"],
  ["work-timeline"],
  ["work-graph"],
  ["work-roster"],
  ["work-affiliations"],
  ["case-resume-state"],
] as const;

/**
 * Reconnect-resync (A81). The SSE stream reconnects with `since=0` and the server
 * returns only its recent TAIL (not an offset-replay), so a disconnect longer than
 * that window drops events permanently. On any SSE re-open we invalidate every live
 * read-model once so a dropped-event window can never strand stale data. Idempotent
 * with the tail-replay+dedupe path (short drops are covered twice; long drops only
 * here).
 */
export function invalidateAllLive(queryClient: QueryClient): void {
  if (import.meta.env?.DEV) {
    // Observable freshness signal (dev only) — proves the resync fired.
    console.debug("[live] reconnect resync — invalidating live read-models");
  }
  for (const queryKey of LIVE_QUERY_KEYS) {
    queryClient.invalidateQueries({ queryKey });
  }
}

export function invalidateRouteTarget(
  queryClient: QueryClient,
  pathname: string,
): void {
  const sessionMatch = pathname.match(/^\/sessions\/([^/?#]+)/);
  if (sessionMatch) {
    invalidateLiveTargets(queryClient, {
      sessions: new Set([decodeURIComponent(sessionMatch[1])]),
      cases: new Set(),
      tasks: false,
      approvals: true,
    });
    return;
  }

  const workMatch = pathname.match(/^\/work\/([^/?#]+)/);
  if (workMatch) {
    invalidateLiveTargets(queryClient, {
      sessions: new Set(),
      cases: new Set([decodeURIComponent(workMatch[1])]),
      tasks: true,
      approvals: true,
    });
  }
}
