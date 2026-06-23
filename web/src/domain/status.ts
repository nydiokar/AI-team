/**
 * Canonical status enums — UI-0 contract.
 *
 * Gap-doc reconciliation (docs/FRONTEND_BACKEND_GAP.md §3, §4):
 * the backend has ONE flat `SessionStatus` enum that conflates lifecycle and
 * operational state. The UI spec (§3.3) requires them SEPARATE — acceptance
 * criterion #4: "lifecycle and operational state are not conflated."
 *
 * So we split into two axes here and derive both from the single backend status
 * (see ../transport/sessionAdapter.ts). ⛔-DROPPED members are intentionally
 * absent — see the per-member notes.
 */

// ── Session lifecycle ──────────────────────────────────────────────────────
// ✅ PRESENT — derivable from SessionView.is_active (gap-doc §3).
//   `archived` ⛔ DROPPED: `closed` already means "ended, out of working list,
//   still readable/resumable"; a second hiding tier earns nothing at this scale.
export type SessionLifecycle = "open" | "closed";

// ── Session operational state ──────────────────────────────────────────────
// Derived from backend SessionStatus (idle|busy|awaiting_input|error|
// cancelled|closed) — gap-doc §3 mapping.
//   `connection_unknown` ⛔ DROPPED: connection liveness is a property of the
//   node/target (heartbeat), surfaced on the System screen — not a per-session
//   state. `waiting_for_approval` is 🟡 PARTIAL — gated on backend Move H
//   (approvals are emitted but inert today); it is kept in the type because the
//   contract must name it, but no live session reports it until H lands.
export type SessionOpState =
  | "idle" //              ✅ from SessionStatus.IDLE
  | "running" //           ✅ from SessionStatus.BUSY (renamed in adapter)
  | "waiting_for_input" // ✅ from SessionStatus.AWAITING_INPUT (needs_input)
  | "waiting_for_approval" // 🟡 PARTIAL — gated on Move H
  | "failed_attention"; //  🟡 from SessionStatus.ERROR (no review distinction yet)

// ── Task lifecycle ─────────────────────────────────────────────────────────
// UI spec §3.4 names 9 states. Backend `TaskStatus` has 4
// (pending/processing/completed/failed) and the mesh `mesh_tasks.status` column
// has its own (pending|claimed|completed|failed|failed_node_offline).
//   ❌ MISSING 5 of the 9 states — the supervised lifecycle is backend Move G′,
//   not built. We name the full 9 in the CONTRACT (UI-0 is the contract) but
//   FIXTURES only use states a current backend turn can actually reach until G′
//   lands.
//   `connection_unknown` here mirrors the spec's task list (it is a TASK-level
//   staleness, distinct from the ⛔-dropped per-SESSION connection_unknown).
export type TaskState =
  | "queued" //            ❌ MISSING (G′)
  | "dispatching" //       ❌ MISSING (G′)  ~ mesh "claimed"
  | "running" //           ✅ ~ processing / claimed
  | "waiting_for_input" // ❌ MISSING (G′)
  | "waiting_for_approval" // ❌ MISSING (G′ + H)
  | "succeeded" //         ✅ ~ completed
  | "failed" //            ✅ ~ failed / failed_node_offline
  | "cancelled" //         🟡 PARTIAL (cancel event exists, not a task state)
  | "connection_unknown"; // ❌ MISSING (G′) — task-level staleness

// ── Connection state (transport-level, spec §9.1) ──────────────────────────
// ✅ buildable now: poll health gives online/offline; reconnecting/state_unknown
// are client-derived. NOT a per-session field.
export type ConnectionState =
  | "online"
  | "reconnecting"
  | "offline"
  | "state_unknown";

// ── Target/node health (spec §3.1) ─────────────────────────────────────────
// ✅ PRESENT but states DIFFER from the spec's 4-state enum: backend gives a
// derived `live` bool + `heartbeat_age_sec` (gap-doc §2). We expose the boolean
// truth and a derived label; we do NOT invent a `degraded` the backend can't
// substantiate.
export type TargetHealth = "online" | "offline" | "unknown";

// ── Command delivery state (spec §9.2) ─────────────────────────────────────
// ❌ MISSING backend support (no idempotency/ack) — these are CLIENT states for
// optimistic mutations, real in UI-2 (Move F). Named now so the contract is
// complete; nothing emits them in UI-0/UI-1 (read-only scope).
export type CommandDeliveryState =
  | "draft"
  | "sending"
  | "acknowledged"
  | "queued"
  | "rejected"
  | "delivery_unknown";
