/**
 * RawSessionView → canonical Session. This is where the backend's ONE flat
 * status enum is split into lifecycle + operational state (gap-doc §3,
 * acceptance #4 — the two must not be conflated).
 */
import type { Session } from "../domain/models";
import type { SessionLifecycle, SessionOpState } from "../domain/status";
import type { RawSessionView } from "./rawApi";

/**
 * lifecycle: open vs closed only (archived ⛔ dropped). `cancelled` is a turn
 * outcome, not a session lifecycle: a stopped task must leave the session
 * resumable, while `closed` remains the only explicit close state.
 */
export function deriveLifecycle(raw: RawSessionView): SessionLifecycle {
  return raw.status === "closed" ? "closed" : "open";
}

/** Backend SessionStatus → operational state (gap-doc §3 table). */
export function deriveOpState(raw: RawSessionView): SessionOpState {
  switch (raw.status) {
    case "busy":
      return "running";
    case "awaiting_input":
      return "waiting_for_input";
    case "error":
      return "failed_attention";
    case "idle":
    case "closed":
    case "cancelled":
    default:
      // closed/cancelled have no live op-state; report idle. Only `closed`
      // carries lifecycle truth. waiting_for_approval is never derivable today
      // — it arrives with Move H, set by the approval adapter, not from status.
      return "idle";
  }
}

/** Attention = anything a human must look at (drives "Needs attention" group). */
export function deriveNeedsAttention(op: SessionOpState): boolean {
  return (
    op === "waiting_for_input" ||
    op === "waiting_for_approval" ||
    op === "failed_attention"
  );
}

export function toSession(raw: RawSessionView): Session {
  const lifecycle = deriveLifecycle(raw);
  const opState = deriveOpState(raw);
  return {
    id: raw.session_id,
    backend: raw.backend,
    workspace: { path: raw.repo_path, targetId: raw.machine_id },
    backendSessionId: raw.backend_session_id || null,
    lifecycle,
    opState,
    needsAttention: lifecycle === "open" && deriveNeedsAttention(opState),
    model: raw.model ?? null,
    defaultModel: raw.default_model ?? null,
    lastTaskId: raw.last_task_id || null,
    lastSummary: raw.last_summary ?? "",
    lastFilesModified: raw.last_files_modified ?? [],
    originChannel: raw.origin_channel,
    originKind: raw.origin_kind,
    updatedAt: raw.updated_at,
  };
}

export function toSessions(raws: RawSessionView[]): Session[] {
  return raws.map(toSession);
}
