/**
 * State transition tables — UI-0 deliverable (spec §14 Phase 0).
 *
 * These document the LEGAL transitions for each lifecycle so the UI can reason
 * about "is this state change expected?" and grey out impossible actions. They
 * are derived from the backend reality (gap-doc) + the spec's lifecycles.
 *
 * Where a transition needs a backend move that isn't built, it's annotated. UI-0
 * is the contract — these tables name the full model; the live read API only
 * exercises the subset reachable today.
 */
import type {
  SessionLifecycle,
  SessionOpState,
  TaskState,
  ConnectionState,
} from "./status";

// ── Session lifecycle ──────────────────────────────────────────────────────
export const SESSION_LIFECYCLE_TRANSITIONS: Record<
  SessionLifecycle,
  SessionLifecycle[]
> = {
  open: ["closed"],
  closed: ["open"], // reopen/branch (spec §3.3) — needs a write path (Move F).
};

// ── Session operational state ──────────────────────────────────────────────
// `waiting_for_approval` is reachable only once Move H wires approvals.
export const SESSION_OP_TRANSITIONS: Record<SessionOpState, SessionOpState[]> = {
  idle: ["running"],
  running: ["idle", "waiting_for_input", "waiting_for_approval", "failed_attention"],
  waiting_for_input: ["running", "idle"],
  waiting_for_approval: ["running", "idle", "failed_attention"], // Move H
  failed_attention: ["running", "idle"],
};

// ── Task lifecycle (the full 9-state model; G′ for the missing states) ──────
export const TASK_TRANSITIONS: Record<TaskState, TaskState[]> = {
  queued: ["dispatching", "cancelled", "connection_unknown"],
  dispatching: ["running", "failed", "connection_unknown"],
  running: [
    "waiting_for_input",
    "waiting_for_approval",
    "succeeded",
    "failed",
    "cancelled",
    "connection_unknown",
  ],
  waiting_for_input: ["running", "cancelled"],
  waiting_for_approval: ["running", "cancelled", "failed"],
  succeeded: [], // terminal
  failed: ["queued"], // retry (Move F write path)
  cancelled: [], // terminal
  connection_unknown: ["running", "failed", "succeeded"], // resolves on reconcile
};

export const TASK_TERMINAL_STATES: ReadonlySet<TaskState> = new Set([
  "succeeded",
  "cancelled",
]);

// ── Connection state (spec §9.1) ───────────────────────────────────────────
export const CONNECTION_TRANSITIONS: Record<ConnectionState, ConnectionState[]> =
  {
    online: ["reconnecting", "offline", "state_unknown"],
    reconnecting: ["online", "offline", "state_unknown"],
    offline: ["reconnecting", "online"],
    state_unknown: ["online", "reconnecting", "offline"],
  };

export function isLegalTaskTransition(from: TaskState, to: TaskState): boolean {
  return TASK_TRANSITIONS[from]?.includes(to) ?? false;
}
