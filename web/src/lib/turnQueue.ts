/**
 * [A82 Stage 6] Managed session turn queue — the pure read-model rules the UI
 * shares (design §9). Queue cards are a read model SEPARATE from historical
 * exchanges: a waiting request is never shown as consumed, the same id moves
 * Waiting → Starting → Working → (terminal: the transcript owns it), and an
 * uncertain execution reads "Recovery required".
 *
 * Status sets mirror backend `src/control/turn_queue.py` (ACTIVE_SLOT_STATUSES,
 * OPEN_STATUSES, TERMINAL_STATUSES) — keep them in sync.
 */
import type { RawSessionTurnQueue, RawTranscriptTurn } from "../transport/rawApi";
import type { SessionTurnQueue } from "../domain/models";
import { SAFETY_NET_MS } from "./refreshPolicy";

/** Query key (packet §3.11): distinct from telemetry `["session-turns", id]`. */
export function sessionTurnQueueKey(sessionId: string): readonly ["session-turn-queue", string] {
  return ["session-turn-queue", sessionId] as const;
}

/** UI fallback is the shared A81 safety net — never a new 3 s chat poll. */
export const TURN_QUEUE_REFETCH_MS = SAFETY_NET_MS;

/** Hold the session's single active slot (recovery_required is NOT terminal). */
export const TURN_ACTIVE_STATUSES: ReadonlySet<string> = new Set([
  "pending",
  "claimed",
  "running",
  "recovery_required",
]);

/** Every non-terminal managed state. */
export const TURN_OPEN_STATUSES: ReadonlySet<string> = new Set([
  "queued",
  ...TURN_ACTIVE_STATUSES,
]);

/** Terminal outcomes; `withdrawn` never ran. */
export const TURN_TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "cancelled",
  "failed_node_offline",
  "withdrawn",
]);

/** In flight (start authorized or about to be): the session is working. */
const TURN_IN_FLIGHT_STATUSES: ReadonlySet<string> = new Set([
  "pending",
  "claimed",
  "running",
]);

export type TurnCardLabel =
  | "Waiting"
  | "Starting"
  | "Working"
  | "Recovery required"
  | "Finished";

/**
 * Conservative card label. `pending`/`claimed` are NOT evidence the model saw
 * the prompt ("Starting"); only start authorization (`running`) reads
 * "Working".
 */
export function turnCardLabel(status: string): TurnCardLabel {
  if (status === "queued") return "Waiting";
  if (status === "pending" || status === "claimed") return "Starting";
  if (status === "running") return "Working";
  if (status === "recovery_required") return "Recovery required";
  return "Finished";
}

/** Only a still-waiting human instruction is operator-editable/withdrawable. */
export function isEditableTurn(turn: {
  status: string;
  turn_source: string | null;
  turn_kind: string | null;
}): boolean {
  return (
    turn.status === "queued" &&
    (turn.turn_source === "human" || turn.turn_source === "operator") &&
    turn.turn_kind === "instruction"
  );
}

/** Who asked — server-derived source, never client-claimed. */
export function turnSourceLabel(turn: {
  turn_source: string | null;
  turn_kind: string | null;
  sender_session_id: string | null;
}): string {
  if (turn.turn_source === "agent") {
    return turn.sender_session_id ? `Agent ${turn.sender_session_id.slice(0, 8)}` : "Agent";
  }
  if (turn.turn_source === "human" || turn.turn_source === "operator") {
    return turn.turn_kind === "compaction" ? "Compaction" : "You";
  }
  if (turn.turn_kind && turn.turn_kind !== "instruction") return humanize(turn.turn_kind);
  return "System";
}

const BLOCKED_REASON_LABEL: Record<string, string> = {
  managed_result_oversize: "Result too large — held for operator review",
  manager_rebound: "Case moved to another Manager",
  case_manager_rebound: "Case moved to another Manager",
  prepare_failed: "Could not prepare — retrying",
  operator_stop: "Stopped by operator",
};

/** Bounded, human label for a blocked/recovery reason (raw detail kept as title). */
export function blockedReasonLabel(reason: string | null | undefined): string | null {
  const raw = (reason ?? "").trim();
  if (!raw) return null;
  const key = raw.split(/[:\s]/, 1)[0];
  return BLOCKED_REASON_LABEL[key] ?? humanize(key);
}

/** Raw overlay → domain shape (null ⇒ unenrolled: legacy status semantics). */
export function toSessionTurnQueue(
  raw: RawSessionTurnQueue | null | undefined,
): SessionTurnQueue | null {
  if (!raw) return null;
  return {
    queued: Number(raw.queued) || 0,
    activeTurnId: raw.active_turn_id ?? null,
    activeStatus: raw.active_status ?? null,
    paused: Boolean(raw.paused),
    hold: raw.hold ?? null,
  };
}

/**
 * Operational state of an ENROLLED session from the ledger overlay, or null
 * to fall back to the persisted status. Persisted queued count never marks a
 * session busy; only an active slot holder does. A held (recovery) turn needs
 * a human.
 */
export function queueOpState(
  queue: SessionTurnQueue | null,
): "running" | "failed_attention" | null {
  if (!queue?.activeStatus) return null;
  if (queue.activeStatus === "recovery_required") return "failed_attention";
  if (TURN_IN_FLIGHT_STATUSES.has(queue.activeStatus)) return "running";
  return null;
}

/**
 * Transcript exchanges whose turn id a queue card currently owns. A card owns
 * an id while the turn is open AND the transcript has no finished exchange for
 * it (pending/terminal dedup under reload/SSE races: the finished exchange
 * wins, the card yields).
 */
export function transcriptFinishedIds(turns: readonly RawTranscriptTurn[]): Set<string> {
  const out = new Set<string>();
  for (const t of turns) {
    if (t.result || (t.status && TURN_TERMINAL_STATUSES.has(t.status))) out.add(t.task_id);
  }
  return out;
}

export function queueOwnedIds(
  queueTurnIds: readonly string[],
  finished: ReadonlySet<string>,
): Set<string> {
  return new Set(queueTurnIds.filter((id) => !finished.has(id)));
}

function humanize(s: string): string {
  const t = s.replace(/_/g, " ").trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : t;
}
