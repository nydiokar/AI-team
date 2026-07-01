/**
 * Canonical event adapter — the snake→dotted translation layer (spec §11.1,
 * gap-doc §6). This is the "mandatory and non-trivial" piece: a real
 * translation (rename + collapse scattered transitions + route operational
 * "job" events to SystemNotice), NOT a pass-through.
 *
 * Input:  RawEvent (backend `event` snake_case name + correlation ids).
 * Output: GatewayEvent | null  (null = deliberately swallowed, e.g. heartbeat).
 *
 * Three buckets (gap-doc §6 table):
 *   1. RENAME      — direct 1:1 to a dotted type (task_created → task.created).
 *   2. COLLAPSE    — several backend names fold into one typed transition
 *                    (task_received/timeout/cancelled/retry → task.state_changed).
 *   3. OPERATIONAL — "job" events with no UI home become system.notice cards;
 *                    this is the cheap presence signal that REPLACES the dropped
 *                    tool-events / task.progress (gap-doc §6 note).
 *
 * ⛔ The backend emits NO tool.* and NO task.progress, so there is nothing to map
 *    to them — the canonical union doesn't even contain them.
 */
import type { GatewayEvent, SystemNotice } from "../domain/events";
import type { TaskState } from "../domain/status";
import type { RawEvent } from "./rawApi";

let _noticeSeq = 0;
function noticeId(ev: RawEvent): string {
  return `notice-${ev.timestamp ?? "t"}-${_noticeSeq++}`;
}

// ── bucket 2: which backend names collapse into a task.state_changed, and to
//    what TaskState. (gap-doc §6: "task_received/timeout/cancelled/retry".)
const TASK_STATE_TRANSITIONS: Record<string, TaskState> = {
  task_received: "running",
  task_claimed: "dispatching",
  validated: "running",
  timeout: "failed",
  task_timeout: "failed",
  retry: "queued",
  cancelled: "cancelled",
  run_cancelled: "cancelled",
};

// ── bucket 3: operational "job" events → SystemNotice severity. Anything in
//    here is a presence signal, rendered as a timeline card (gap-doc §6 note).
const OPERATIONAL_SEVERITY: Record<string, SystemNotice["severity"]> = {
  mesh_dispatch: "info",
  mesh_result: "info",
  mesh_routing_failed: "error",
  worker_pool_scaled: "info",
  throttled: "warning",
  dropped_low_priority: "warning",
  summarized: "info",
  security_violation: "error",
  task_claimed: "info",
  mesh_degraded: "warning",
  mesh_restored: "success",
};

// Events deliberately SWALLOWED (too noisy / pure infra) → return null.
const SWALLOW = new Set(["heartbeat"]);

function severityToText(ev: RawEvent): string {
  // Human one-liner. Kept terse; the raw kind is preserved on the notice.
  const where = ev.node_id ? ` @${ev.node_id}` : "";
  return `${ev.event.replace(/_/g, " ")}${where}`;
}

function toNotice(ev: RawEvent): GatewayEvent {
  const severity = OPERATIONAL_SEVERITY[ev.event] ?? "info";
  const notice: SystemNotice = {
    id: noticeId(ev),
    sessionId: (ev.session_id as string) ?? null,
    taskId: (ev.task_id as string) ?? null,
    kind: ev.event,
    text: severityToText(ev),
    severity,
    timestamp: ev.timestamp,
  };
  return { type: "system.notice", notice };
}

/**
 * Translate a single backend event. Returns null when the event is
 * intentionally dropped (heartbeat) or carries no usable correlation.
 */
export function adaptEvent(ev: RawEvent): GatewayEvent | null {
  const name = ev.event;
  if (!name || SWALLOW.has(name)) return null;

  // bucket 1 — direct renames -----------------------------------------------
  switch (name) {
    case "task_created":
      // We don't reconstruct a full Task from the event (the read API owns the
      // object); emit a state transition + a notice. A full task.created is
      // synthesised by the store from /api/tasks, not here.
      if (ev.task_id) {
        return { type: "task.state_changed", taskId: String(ev.task_id), state: "queued" };
      }
      return toNotice(ev);
    case "artifacts_written":
      // ✅ rename → artifact.created. The event lacks the full Artifact object;
      // surface as a notice carrying the correlation. (UI-4 binds the real
      // listing API for the typed Artifact.)
      return toNotice({ ...ev, event: "artifact_written" });
    case "approval.requested": // already dotted backend-side (M4)
    case "approval_requested":
      return toNotice({ ...ev, event: "approval_requested" });
    case "approval.granted":
    case "approval_granted":
      return ev.task_id || ev.session_id
        ? {
            type: "approval.resolved",
            approvalId: String(ev.task_id ?? ev.session_id),
            decision: "granted",
          }
        : null;
  }

  // bucket 2 — collapse scattered transitions into one typed change ----------
  const transition = TASK_STATE_TRANSITIONS[name];
  if (transition) {
    if (name === "cancelled" || name === "run_cancelled") {
      const runId = String(ev.task_id ?? ev.session_id ?? "");
      if (runId) return { type: "run.cancelled", runId };
    }
    if (ev.task_id) {
      return { type: "task.state_changed", taskId: String(ev.task_id), state: transition };
    }
    return toNotice(ev);
  }

  // bucket 3 — operational presence → SystemNotice --------------------------
  if (name in OPERATIONAL_SEVERITY) {
    return toNotice(ev);
  }

  // Unknown backend event: don't lose it — surface as an info notice so the
  // timeline never silently drops signal (diagnostics value).
  return toNotice(ev);
}

/** Adapt a batch, dropping the swallowed ones. */
export function adaptEvents(raws: RawEvent[]): GatewayEvent[] {
  const out: GatewayEvent[] = [];
  for (const ev of raws) {
    const adapted = adaptEvent(ev);
    if (adapted) out.push(adapted);
  }
  return out;
}
