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

/**
 * In flight (a carrier holds it): the session is working. `pending` is NOT —
 * an activated head waits for its carrier to claim it (a pinned carrier that
 * went offline keeps it), so it reads Starting on its card, never "running".
 */
const TURN_IN_FLIGHT_STATUSES: ReadonlySet<string> = new Set([
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

/**
 * Who asked — server-derived source, never client-claimed (D5). Always names a
 * principal AND, when the turn is not a plain operator instruction, what kind of
 * turn it is, so the operator never sees a bare "System" / "Continuation" with no
 * context. Shapes:
 *   human/operator instruction      → "You"
 *   human/operator compaction       → "You · Compaction"
 *   agent instruction (+session id) → "Agent 1f9bce3f"
 *   system continuation             → "System · Continuation"
 */
export function turnSourceLabel(turn: {
  turn_source: string | null;
  turn_kind: string | null;
  sender_session_id: string | null;
}): string {
  const who = turnPrincipalLabel(turn);
  const kind = turn.turn_kind;
  if (kind && kind !== "instruction") return `${who} · ${humanize(kind)}`;
  return who;
}

/** The principal only (no turn-kind qualifier) — used where space is tightest. */
export function turnPrincipalLabel(turn: {
  turn_source: string | null;
  sender_session_id: string | null;
}): string {
  const src = (turn.turn_source ?? "").trim();
  if (src === "agent") {
    return turn.sender_session_id ? `Agent ${turn.sender_session_id.slice(0, 8)}` : "Agent";
  }
  if (src === "human" || src === "operator") return "You";
  if (src === "system" || src === "") return "System";
  return humanize(src);
}

/**
 * The distinct senders behind a set of turns, for the collapsed one-line
 * indicator (D3): dedupes by principal, keeps first-seen order, and caps the
 * visible names, summarising the rest as "+N". Empty list ⇒ "".
 */
export function senderSummary(
  turns: readonly {
    turn_source: string | null;
    sender_session_id: string | null;
  }[],
  max = 2,
): string {
  const seen: string[] = [];
  for (const t of turns) {
    const who = turnPrincipalLabel(t);
    if (!seen.includes(who)) seen.push(who);
  }
  if (seen.length === 0) return "";
  if (seen.length <= max) return seen.join(", ");
  return `${seen.slice(0, max).join(", ")} +${seen.length - max}`;
}

/** First 1–2 words of a preview, for a super-thin row (D4). Never the full text. */
export function previewWords(preview: string | null | undefined, words = 2): string {
  const parts = (preview ?? "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "";
  const head = parts.slice(0, words).join(" ");
  return parts.length > words ? `${head}…` : head;
}

/** Ids of the turns that are genuinely STILL WAITING (D1/D6). An active or
 *  finished turn is NOT waiting — it belongs to the chat, not the queue. */
export function waitingTurnIds(
  turns: readonly { id: string; status: string }[],
): string[] {
  return turns.filter((t) => t.status === "queued").map((t) => t.id);
}

/** A legal operator resolution for a held/stuck turn (F1). `requiresAck` mirrors
 *  the backend contract (`turn_requests.py`): a never-started `claimed` turn is
 *  token-fenced and only requeue-able (no acknowledgement); a started/held turn
 *  can only be failed/cancelled and must be acknowledged as uncertain. */
export type RecoveryDecision = "requeue" | "cancelled" | "failed";

export interface RecoveryOption {
  decision: RecoveryDecision;
  label: string;
  description: string;
  tone: "safe" | "neutral" | "danger";
  requiresAck: boolean;
}

/**
 * The legal resolutions for a turn's status, in operator-preference order
 * (safest first). Mirrors `src/control/routes/turn_requests.py`:
 *   claimed (never started)        → requeue only (no acknowledgement)
 *   running / recovery_required    → cancelled | failed (acknowledgement required)
 * Any other status ⇒ no resolution is offered.
 */
export function recoveryOptions(status: string): RecoveryOption[] {
  if (status === "claimed") {
    return [
      {
        decision: "requeue",
        label: "Requeue",
        description: "Never started — release it back to the queue to run again (safe).",
        tone: "safe",
        requiresAck: false,
      },
    ];
  }
  if (status === "running" || status === "recovery_required") {
    return [
      {
        decision: "cancelled",
        label: "Cancel",
        description: "Drop it — no result recorded.",
        tone: "neutral",
        requiresAck: true,
      },
      {
        decision: "failed",
        label: "Mark failed",
        description: "Mark it failed — it will not re-run.",
        tone: "danger",
        requiresAck: true,
      },
    ];
  }
  return [];
}

const BLOCKED_REASON_LABEL: Record<string, string> = {
  managed_result_oversize: "Result too large — held for operator review",
  manager_rebound: "Case moved to another Manager",
  case_manager_rebound: "Case moved to another Manager",
  prepare_failed: "Could not prepare — retrying",
  operator_stop: "Stopped by operator",
  carrier_offline: "Carrier offline — waits for it to return",
  legacy_work_draining: "Waiting for a pre-cutover turn to finish — starts shortly after",
};

/** [A82 Stage 8a] Queue-level label for finished turns whose post-commit
 *  effects (reply notification / history / telemetry) ended `failed`. */
export function effectsFailedLabel(count: number | null | undefined): string | null {
  const n = Number(count) || 0;
  if (n <= 0) return null;
  return n === 1
    ? "1 finished turn: reply delivery failed — check the transcript"
    : `${n} finished turns: reply delivery failed — check the transcript`;
}

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
