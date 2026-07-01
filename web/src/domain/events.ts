/**
 * Canonical gateway events — UI-0 contract (spec §11.2).
 *
 * The frontend consumes ONE dotted, typed event vocabulary. The backend emits
 * ~25 snake_case OPERATIONAL events that barely overlap in meaning (gap-doc §6).
 * The translation snake→dotted is a REAL adapter (../transport/eventAdapter.ts),
 * not a pass-through.
 *
 * ⛔-DROPPED events are OMITTED from this union by design (gap-doc §6):
 *   - `task.progress`            — atomic turn, no mid-turn progress.
 *   - `tool.requested/started/completed/failed` — black-box backend, no tool events.
 * `message.delta` (token streaming) is named but marked POST-V1 — the CLI
 * backends are blocking/one-shot, so it never fires until a deliberate streaming
 * build. UI-1 renders whole-message only.
 */
import type {
  Session,
  Message,
  Task,
  ApprovalRequest,
  Artifact,
  RemoteFile,
} from "./models";
import type { TaskState, ConnectionState } from "./status";

// A "SystemNotice" is the operational channel for infrastructure/health events
// that have no session home. Routine turn lifecycle stays in typed task/run
// events so System does not become a session progress feed.
export interface SystemNotice {
  id: string;
  sessionId: string | null;
  taskId: string | null;
  /** The raw backend event name, kept for diagnostics (e.g. "mesh_dispatch"). */
  kind: string;
  /** Human one-liner derived from the backend event. */
  text: string;
  /** Severity drives the card color (spec §8.2 semantic roles). */
  severity: "info" | "success" | "warning" | "error";
  timestamp: string;
}

export type GatewayEvent =
  // ── targets ─ 🟡 PARTIAL: derived from node heartbeat liveness (gap-doc §6).
  | { type: "target.connected"; targetId: string }
  | { type: "target.disconnected"; targetId: string }
  // ── sessions ─ 🟡 derive from /api/sessions diff (no per-event source).
  | { type: "session.created"; session: Session }
  | { type: "session.updated"; session: Session }
  | { type: "session.closed"; sessionId: string }
  | { type: "session.reopened"; sessionId: string }
  // ── messages ─ ❌ MISSING (whole-turn result, no message events). delta=post-v1.
  | { type: "message.created"; message: Message }
  /** @postV1 token streaming — never fires in v1 (blocking CLI backend). */
  | { type: "message.delta"; messageId: string; text: string }
  | { type: "message.completed"; messageId: string }
  // ── tasks ─ ✅ task.created (rename); 🟡 state_changed (collapses scattered
  //    backend transitions task_received/timeout/cancelled/retry into one).
  | { type: "task.created"; task: Task }
  | { type: "task.state_changed"; taskId: string; state: TaskState }
  // ⛔ task.progress + tool.* OMITTED — see header.
  // ── approvals ─ 🟡 PARTIAL: emitted (already dotted!) but inert (Move H).
  | { type: "approval.required"; approval: ApprovalRequest }
  | { type: "approval.resolved"; approvalId: string; decision: string }
  // ── artifacts / files ─ ✅ artifact.created (rename artifacts_written);
  //    🟡 file.changed (from TaskResult.files_modified, not an event today).
  | { type: "artifact.created"; artifact: Artifact }
  | { type: "file.changed"; file: RemoteFile }
  // ── run control ─ ✅ run.cancelled (rename `cancelled`).
  | { type: "run.cancelled"; runId: string }
  // ── connection ─ 🟡 PARTIAL: derived from node heartbeat / transport.
  | { type: "connection.state_changed"; state: ConnectionState }
  // ── operational presence ─ the SystemNotice channel (gap-doc §6 note).
  //    This is NOT in the spec's literal §11.2 list; it is the deliberate
  //    replacement for the dropped tool-events / progress events.
  | { type: "system.notice"; notice: SystemNotice };

export type GatewayEventType = GatewayEvent["type"];
