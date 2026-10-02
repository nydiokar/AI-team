import type { QueryClient } from "@tanstack/react-query";
import type { RawEvent } from "../transport/rawApi";
import {
  recordActivityOnlyInvalidations,
  recordCaseInvalidations,
  recordFullSessionInvalidations,
  recordTaskListInvalidation,
  recordReconnectResync,
} from "./refreshMetrics";

function textField(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/**
 * Progress-only events: high-frequency live signals that mutate NOTHING in the
 * persisted read-models — only the live activity ticker. `task_activity` is the
 * per-tool-use label ("Using Bash", "Writing response…", "Thinking…") emitted
 * ~1×/sec/turn by the driver (`claude_driver.py`); the message/turn/usage rows do
 * not change until the turn actually completes (`mesh_result`/`claude_finished`).
 * So a progress ping refreshes ONLY `session-activity`, never the expensive
 * transcript read the task set out to reduce. Adding a new pure-progress event?
 * List it here so it stays scoped.
 */
const PROGRESS_ONLY_EVENTS: ReadonlySet<string> = new Set(["task_activity"]);

/**
 * [A82 Stage 6] Post-commit managed queue signal (`turn_queue_changed`). A
 * queue-only change (admit/edit/withdraw/pause/activate/claim/start) refreshes
 * ONLY the queue card read-model + the session list (opState comes from the
 * ledger overlay) — never the transcript. A TERMINAL outcome is a real
 * conversation fact: full session refresh, so the finished exchange replaces
 * the card in the same batch.
 */
const TURN_QUEUE_EVENT = "turn_queue_changed";
const TURN_TERMINAL_CHANGES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "cancelled",
  "failed_node_offline",
  "resolved",
]);

export interface LiveInvalidationTarget {
  /** sessions needing a FULL read-model refresh (messages/turns/usage/activity) */
  sessions: Set<string>;
  /** sessions needing ONLY the live activity-ticker refresh (progress pings) */
  activitySessions: Set<string>;
  /** [A82 Stage 6] sessions needing ONLY the queue cards + session list refresh */
  queueSessions?: Set<string>;
  cases: Set<string>;
  tasks: boolean;
  approvals: boolean;
}

export function collectLiveInvalidationTargets(
  raws: RawEvent[],
): LiveInvalidationTarget {
  const target: LiveInvalidationTarget = {
    sessions: new Set<string>(),
    activitySessions: new Set<string>(),
    queueSessions: new Set<string>(),
    cases: new Set<string>(),
    tasks: false,
    approvals: false,
  };

  for (const raw of raws) {
    const sessionId = textField(raw.session_id);
    const eventName = String(raw.event ?? "");
    const progressOnly = PROGRESS_ONLY_EVENTS.has(eventName);

    if (progressOnly) {
      // A progress ping churns nothing but the ticker — never the transcript,
      // task/job lists, cases, or approvals. Record the session and move on.
      if (sessionId) target.activitySessions.add(sessionId);
      continue;
    }

    if (eventName === TURN_QUEUE_EVENT) {
      if (!sessionId) continue;
      if (TURN_TERMINAL_CHANGES.has(String(raw.change ?? ""))) {
        target.sessions.add(sessionId);
        target.tasks = true;
      } else {
        target.queueSessions?.add(sessionId);
        // A withdrawal is terminal for the task lists, never a transcript fact.
        if (raw.change === "withdrawn") target.tasks = true;
      }
      continue;
    }

    if (sessionId) target.sessions.add(sessionId);

    const caseId =
      textField(raw.case_id) ??
      textField(raw.flow_run_id) ??
      textField(raw.current_case_id);
    if (caseId) target.cases.add(caseId);

    if (textField(raw.task_id)) target.tasks = true;
    if (eventName.startsWith("approval")) target.approvals = true;
  }

  return target;
}

export function invalidateLiveTargets(
  queryClient: QueryClient,
  target: LiveInvalidationTarget,
): void {
  const queueOnly = target.queueSessions ?? new Set<string>();
  if (target.sessions.size > 0 || target.tasks || queueOnly.size > 0) {
    queryClient.invalidateQueries({ queryKey: ["sessions"] });
  }
  for (const sessionId of queueOnly) {
    if (target.sessions.has(sessionId)) continue; // full refresh below covers it
    queryClient.invalidateQueries({ queryKey: ["session-turn-queue", sessionId] });
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
    recordTaskListInvalidation();
  }
  if (target.approvals) {
    queryClient.invalidateQueries({ queryKey: ["approvals"] });
  }

  for (const sessionId of target.sessions) {
    queryClient.invalidateQueries({ queryKey: ["session-messages", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["session-turns", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["session-turn-queue", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["session-usage", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["session-activity", sessionId] });
    queryClient.invalidateQueries({ queryKey: ["work-affiliations"] });
    queryClient.invalidateQueries({ queryKey: ["jobs"] });
  }
  recordFullSessionInvalidations(target.sessions.size);

  // Progress pings (task_activity): refresh ONLY the live activity ticker, and
  // only for sessions NOT already fully invalidated above (a real event in the
  // same batch supersedes the ping). This keeps the ~1×/sec/turn ping stream off
  // the expensive transcript read-models.
  let activityOnly = 0;
  for (const sessionId of target.activitySessions) {
    if (target.sessions.has(sessionId)) continue;
    queryClient.invalidateQueries({ queryKey: ["session-activity", sessionId] });
    activityOnly += 1;
  }
  recordActivityOnlyInvalidations(activityOnly);

  recordCaseInvalidations(target.cases.size);
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
  ["session-turn-queue"],
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
  recordReconnectResync();
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
      activitySessions: new Set(),
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
      activitySessions: new Set(),
      cases: new Set([decodeURIComponent(workMatch[1])]),
      tasks: true,
      approvals: true,
    });
  }
}
