/**
 * RawSessionView → canonical Session. This is where the backend's ONE flat
 * status enum is split into lifecycle + operational state (gap-doc §3,
 * acceptance #4 — the two must not be conflated).
 */
import type { Session } from "../domain/models";
import type { SessionLifecycle, SessionOpState } from "../domain/status";
import type { RawSessionView } from "./rawApi";

/**
 * lifecycle: open vs closed only (archived ⛔ dropped). `is_active` already
 * folds error/cancelled/closed into "not active" backend-side, but lifecycle is
 * specifically about closed — error/cancelled are still "open but failed".
 */
export function deriveLifecycle(raw: RawSessionView): SessionLifecycle {
  return raw.status === "closed" || raw.status === "cancelled"
    ? "closed"
    : "open";
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
      // closed/cancelled have no live op-state; report idle (lifecycle carries
      // the "closed" truth). waiting_for_approval is never derivable today — it
      // arrives with Move H, set by the approval adapter, not from status.
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
    lifecycle,
    opState,
    needsAttention: lifecycle === "open" && deriveNeedsAttention(opState),
    model: raw.model ?? null,
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
