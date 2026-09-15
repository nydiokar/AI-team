/**
 * Live-refresh metrics (measurement for the A81 follow-up).
 *
 * The event bridge invalidates react-query keys on real SSE events. A high-frequency
 * `task_activity` progress ping ("Using Bash", "Writing response…") must refresh ONLY
 * the live activity ticker — never the expensive transcript read-models
 * (`session-messages`/`session-turns`/`session-usage`). These counters make that split
 * observable so it can be PROVEN, not assumed:
 *
 *   - `fullSessionInvalidations` climbs only on real turn boundaries (a completed
 *     result, a new dispatch) — this is the count of transcript reads triggered.
 *   - `activityOnlyInvalidations` climbs on every progress ping — cheap ticker refresh.
 *
 * During an actively-running turn `fullSessionInvalidations` should stay ~flat while
 * `activityOnlyInvalidations` ticks up. If `fullSessionInvalidations` tracks the ping
 * rate, the scoping regressed.
 *
 * Always-on (the counters are a handful of integers — no PII, no secrets), and exposed
 * on `window.__aiTeamRefreshMetrics` so the operator can read them from the console on
 * the LIVE app: `__aiTeamRefreshMetrics.snapshot()` / `.reset()`.
 */

export interface RefreshMetricsSnapshot {
  /** transcript read-model refreshes (session-messages/turns/usage) triggered */
  fullSessionInvalidations: number;
  /** activity-ticker-only refreshes from progress pings (task_activity) */
  activityOnlyInvalidations: number;
  /** tasks/jobs/artifacts list refreshes */
  taskListInvalidations: number;
  /** work-* (case) read-model refreshes */
  caseInvalidations: number;
  /** reconnect-resync sweeps (invalidateAllLive) */
  reconnectResyncs: number;
  /** epoch ms when this window started (last reset) */
  since: number;
  /** ms elapsed in the current window */
  elapsedMs: number;
}

const counters = {
  fullSessionInvalidations: 0,
  activityOnlyInvalidations: 0,
  taskListInvalidations: 0,
  caseInvalidations: 0,
  reconnectResyncs: 0,
  since: Date.now(),
};

export function recordFullSessionInvalidations(n: number): void {
  if (n > 0) counters.fullSessionInvalidations += n;
}
export function recordActivityOnlyInvalidations(n: number): void {
  if (n > 0) counters.activityOnlyInvalidations += n;
}
export function recordTaskListInvalidation(): void {
  counters.taskListInvalidations += 1;
}
export function recordCaseInvalidations(n: number): void {
  if (n > 0) counters.caseInvalidations += n;
}
export function recordReconnectResync(): void {
  counters.reconnectResyncs += 1;
}

export function snapshotRefreshMetrics(): RefreshMetricsSnapshot {
  return {
    fullSessionInvalidations: counters.fullSessionInvalidations,
    activityOnlyInvalidations: counters.activityOnlyInvalidations,
    taskListInvalidations: counters.taskListInvalidations,
    caseInvalidations: counters.caseInvalidations,
    reconnectResyncs: counters.reconnectResyncs,
    since: counters.since,
    elapsedMs: Date.now() - counters.since,
  };
}

export function resetRefreshMetrics(): void {
  counters.fullSessionInvalidations = 0;
  counters.activityOnlyInvalidations = 0;
  counters.taskListInvalidations = 0;
  counters.caseInvalidations = 0;
  counters.reconnectResyncs = 0;
  counters.since = Date.now();
}

// Expose on the window so the operator can inspect the live app from devtools.
// Guarded for SSR / test environments without a DOM.
if (typeof window !== "undefined") {
  (window as unknown as Record<string, unknown>).__aiTeamRefreshMetrics = {
    snapshot: snapshotRefreshMetrics,
    reset: resetRefreshMetrics,
  };
}
