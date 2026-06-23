/**
 * Timeline fixtures — a session's chronological event stream (spec §2.4). Built
 * as a heterogeneous list of typed timeline items the UI-1 SessionTimeline
 * renders. Whole-message only (no deltas — streaming is post-v1). Operational
 * "job" events render as SystemNotice (the replacement for ⛔ tool executions).
 */
import type { Message, ApprovalRequest, Artifact } from "../domain/models";
import type { SystemNotice } from "../domain/events";
import type { TaskState } from "../domain/status";

export type TimelineItem =
  | { kind: "message"; at: string; message: Message }
  | { kind: "task_state"; at: string; taskId: string; state: TaskState; objective: string }
  | { kind: "notice"; at: string; notice: SystemNotice }
  | { kind: "approval"; at: string; approval: ApprovalRequest }
  | { kind: "artifact"; at: string; artifact: Artifact }
  | { kind: "error"; at: string; text: string };

const SID = "sess_gateway_ui";

export const timelineFixture: TimelineItem[] = [
  {
    kind: "message",
    at: "2026-06-22T10:40:45Z",
    message: {
      id: "m1",
      sessionId: SID,
      role: "user",
      text: "Refactor the event adapter into a real snake→dotted translation layer.",
      createdAt: "2026-06-22T10:40:45Z",
    },
  },
  {
    kind: "task_state",
    at: "2026-06-22T10:40:51Z",
    taskId: "task_a1",
    state: "running",
    objective: "Refactor the event adapter into a translation layer",
  },
  {
    kind: "notice",
    at: "2026-06-22T10:40:52Z",
    notice: {
      id: "n1",
      sessionId: SID,
      taskId: "task_a1",
      kind: "mesh_dispatch",
      text: "mesh dispatch @main-pc",
      severity: "info",
      timestamp: "2026-06-22T10:40:52Z",
    },
  },
  {
    kind: "notice",
    at: "2026-06-22T10:41:30Z",
    notice: {
      id: "n2",
      sessionId: SID,
      taskId: "task_a1",
      kind: "validated",
      text: "validated",
      severity: "success",
      timestamp: "2026-06-22T10:41:30Z",
    },
  },
  {
    kind: "message",
    at: "2026-06-22T10:42:00Z",
    message: {
      id: "m2",
      sessionId: SID,
      role: "assistant",
      text: "Done. The adapter now has three buckets: rename, collapse, and operational→SystemNotice. Heartbeats are swallowed.",
      createdAt: "2026-06-22T10:42:00Z",
    },
  },
  {
    kind: "approval",
    at: "2026-06-22T10:43:00Z",
    approval: {
      id: "appr_1",
      sessionId: SID,
      taskId: "task_a1",
      targetId: "main-pc",
      action: "Apply patch: refactor event adapter (7 files)",
      affectedFiles: ["web/src/transport/eventAdapter.ts"],
      risk: "medium",
      reversible: true,
      stale: false,
      expiresAt: "2026-06-22T11:00:00Z",
      createdAt: "2026-06-22T10:43:00Z",
    },
  },
];
