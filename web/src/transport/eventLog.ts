/**
 * Event → log-line projection (UI-5). A PURE flattening of the canonical
 * `StampedEvent` (dotted GatewayEvent + raw key + wall-clock) into a single
 * `LogLine` the activity feed renders.
 *
 * This is NOT a new event vocabulary — it consumes the already-canonical
 * GatewayEvent union (../domain/events) the app's one SSE stream already produces.
 * `system.notice` (the operational-presence channel) carries its own severity +
 * text, so we pass those through; the few TYPED variants (task.state_changed,
 * run.cancelled, approval.resolved, target.*) get a stable one-liner + severity
 * here so nothing in the stream renders as a blank row.
 */
import type { StampedEvent } from "../hooks/useEventStream";
import type { SystemNotice } from "../domain/events";

export type LogSeverity = SystemNotice["severity"]; // info | success | warning | error

export interface LogLine {
  /** Stable identity = the source raw event key (already deduped upstream). */
  id: string;
  at: string;
  severity: LogSeverity;
  /** Short machine-ish kind for the row label (e.g. "mesh_dispatch", "task"). */
  kind: string;
  /** Human one-liner. */
  text: string;
  sessionId: string | null;
  taskId: string | null;
}

export function toLogLine(stamped: StampedEvent): LogLine {
  const { event, at, rawKey } = stamped;
  const base = { id: rawKey, at, sessionId: null, taskId: null } as const;

  switch (event.type) {
    case "system.notice": {
      const n = event.notice;
      return {
        ...base,
        severity: n.severity,
        kind: n.kind,
        text: n.text,
        sessionId: n.sessionId,
        taskId: n.taskId,
      };
    }
    case "task.state_changed":
      return {
        ...base,
        severity: event.state === "failed" ? "error" : "info",
        kind: "task",
        text: `task ${event.state.replace(/_/g, " ")}`,
        sessionId: event.sessionId ?? null,
        taskId: event.taskId,
      };
    case "run.cancelled":
      return {
        ...base,
        severity: "warning",
        kind: "run",
        text: "run cancelled",
        sessionId: event.sessionId ?? null,
        taskId: event.taskId ?? event.runId,
      };
    case "approval.resolved":
      return {
        ...base,
        severity: event.decision === "granted" || event.decision === "approved" ? "success" : "warning",
        kind: "approval",
        text: `approval ${event.decision}`,
        sessionId: event.sessionId ?? null,
        taskId: event.taskId ?? null,
      };
    case "target.connected":
      return { ...base, severity: "success", kind: "target", text: `target ${event.targetId} connected` };
    case "target.disconnected":
      return { ...base, severity: "warning", kind: "target", text: `target ${event.targetId} disconnected` };
    case "session.closed":
      return { ...base, severity: "info", kind: "session", text: "session closed", sessionId: event.sessionId };
    case "session.reopened":
      return { ...base, severity: "info", kind: "session", text: "session reopened", sessionId: event.sessionId };
    default:
      // Any other typed variant the stream may carry (session.created/updated,
      // message.*, task.created, approval.required, artifact.created, file.changed,
      // connection.state_changed): render a stable, non-blank row keyed on its type.
      return { ...base, severity: "info", kind: event.type.split(".")[0], text: event.type.replace(/\./g, " ") };
  }
}

export function toLogLines(events: StampedEvent[]): LogLine[] {
  return events.map(toLogLine);
}
