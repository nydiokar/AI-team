/**
 * Canonical domain objects — UI-0 contract.
 *
 * These are the FRONTEND domain shapes the app binds to. They are NOT the raw
 * backend payloads — those pass through the transport adapters (../transport)
 * which translate snake_case backend rows into these. Backend-specific payloads
 * must not leak into components (spec §11.1).
 *
 * Each object is tagged with its gap-doc status (docs/FRONTEND_BACKEND_GAP.md §2).
 * ⛔-DROPPED objects (ToolExecution) are OMITTED entirely — see the note at the
 * bottom.
 */
import type {
  SessionLifecycle,
  SessionOpState,
  TaskState,
  TargetHealth,
} from "./status";

// ── Target (machine) ─ ✅ PRESENT (gap-doc §2) ─────────────────────────────
// Derived from `/api/nodes` rows + the dashboard's derived `live` flag. We use
// the live flag + heartbeat age, NOT the stale `status` column (gap-doc §2 note).

/** One active task as reported in a worker's live_state heartbeat. */
export interface ActiveTaskDetail {
  taskId: string;
  backend: string;
  /** Simplified mesh action label (run_oneoff, run_session, …). */
  action: string;
  phase: string;
  /** ISO start timestamp; null when worker didn't capture it. */
  startedAt: string | null;
}

export interface Target {
  /** node_id (PK in `nodes`). */
  id: string;
  /** Derived label from `live` + heartbeat age — NOT the stored status column. */
  health: TargetHealth;
  /** True when heartbeat_age_sec <= node_heartbeat_timeout_sec (backend-derived). */
  live: boolean;
  /** Seconds since last heartbeat; null when never seen / unparseable. */
  heartbeatAgeSec: number | null;
  /** Backend names this node can run (e.g. ["claude","codex"]). */
  backends: string[];
  tailscaleIp: string;
  maxConcurrent: number;
  /** Slot usage from the latest live_state heartbeat; null when not yet reported. */
  slotsUsed: number | null;
  slotsTotal: number | null;
  /** Active tasks from the latest live_state heartbeat (empty when idle). */
  activeTasks: ActiveTaskDetail[];
}

// ── Workspace ─ 🟡 PARTIAL (gap-doc §2) ────────────────────────────────────
// A path on a target, NOT a first-class object — there is no browse/enumerate
// API. We model only what `session.repo_path` carries.
export interface Workspace {
  /** Filesystem path / repo root. */
  path: string;
  /** Owning target id (= session.machine_id). */
  targetId: string;
}

// ── Session ─ 🟡 PARTIAL (gap-doc §3) ──────────────────────────────────────
// Split lifecycle from operational state (the conflation the backend doesn't).
export interface Session {
  id: string;
  /** Raw backend name (claude|codex|opencode…). Rendering is the surface's job. */
  backend: string;
  workspace: Workspace;
  /** Native backend session id (resume key); null when not yet captured. */
  backendSessionId: string | null;
  /** lifecycle ≠ operational state (spec §3.3 / acceptance #4). */
  lifecycle: SessionLifecycle;
  opState: SessionOpState;
  /** True when opState needs a human (waiting_for_input/approval, failed). */
  needsAttention: boolean;
  model: string | null;
  effort: string | null;
  /** The backend's default model — shown when `model` is null. */
  defaultModel: string | null;
  lastTaskId: string | null;
  /** Short human summary of the last turn (last_result_summary || last_summary). */
  lastSummary: string;
  lastFilesModified: string[];
  originChannel: string;
  originKind: string;
  updatedAt: string;
}

// ── Task ─ 🟡 PARTIAL / ❌ MISSING lifecycle (gap-doc §4) ───────────────────
// Today's backend Task is a one-shot (pending→processing→completed/failed). The
// 9-state supervised lifecycle is Move G′ (not built). `progressPct` is OMITTED
// — `task.progress` is ⛔ DROPPED (atomic turn, no mid-turn progress). Use
// state + elapsed time instead.
export interface Task {
  id: string;
  /** Parent session; null for run_oneoff tasks (mesh_tasks.session_id nullable). */
  sessionId: string | null;
  /** Backend name that ran / will run it. */
  backend: string;
  /** Owning target; null until dispatch (mesh_tasks.machine_id). */
  targetId: string | null;
  state: TaskState;
  /** Short objective / latest meaningful line; from summary or action. */
  objective: string;
  createdAt: string;
  updatedAt: string;
  completedAt: string | null;
  /** results/{task_id}.json pointer when present (no listing API yet — §6 🟡). */
  artifactPath: string | null;
  error: string | null;
}

// ── Message ─ ❌ MISSING as events (gap-doc §6) ────────────────────────────
// The backend returns whole-turn results, not message.created/delta/completed
// events. `message.delta` (token streaming) is ⛔ DROPPED for v1. A Message here
// is a WHOLE turn (no partial text). Used by fixtures only in UI-1.
export interface Message {
  id: string;
  sessionId: string;
  role: "user" | "assistant";
  /** Complete text — never a partial delta (streaming is post-v1). */
  text: string;
  createdAt: string;
}

// ── Approval ─ 🟡 PARTIAL / ❌ MISSING object (gap-doc §5) ──────────────────
// `approval.requested/granted` events are emitted (M4) but inert — no object,
// no queue, no consumer. This is the contract shape; live data needs Move H.
export interface ApprovalRequest {
  id: string;
  sessionId: string;
  taskId: string | null;
  targetId: string | null;
  /** Human description of the proposed consequential action. */
  action: string;
  affectedFiles: string[];
  risk: "low" | "medium" | "high";
  reversible: boolean;
  /** Whether the last-known state is stale (spec §7.7 stale warning). */
  stale: boolean;
  expiresAt: string | null;
  createdAt: string;
}

// ── Artifact ─ 🟡 PARTIAL (gap-doc §2/§6) ──────────────────────────────────
// Artifacts exist on disk (results/{task_id}.json, last_artifact_path) but
// there's no listing API / typed object. This is the target shape; UI-4 binds it.
export interface Artifact {
  id: string;
  taskId: string | null;
  sessionId: string | null;
  kind: "patch" | "diff" | "report" | "file" | "result" | "archive" | "image";
  /** Backend path pointer (results/{task_id}.json …). */
  path: string;
  /** Optional human label. */
  name: string;
  createdAt: string;
}

// ── RemoteFile ─ 🟡 PARTIAL (gap-doc §6) ───────────────────────────────────
// from TaskResult.files_modified (not an event today). Target shape for UI-4.
export interface RemoteFile {
  path: string;
  sessionId: string | null;
  change: "added" | "modified" | "deleted";
}

export type SessionActivityKind =
  | "task_state"
  | "worker_state"
  | "turn_event"
  | "artifact"
  | "file_change"
  | "job_state"
  | "approval"
  | "recovery"
  | "system_notice"
  | string;

export interface SessionActivityItem {
  id: string;
  kind: SessionActivityKind;
  source: string;
  durability: "durable" | "diagnostic" | string;
  timestamp: string;
  sessionId: string | null;
  taskId: string | null;
  turnId: string | null;
  jobId: string | null;
  nodeId: string | null;
  backend: string | null;
  status: string | null;
  confidence: "high" | "medium" | "low" | string;
  staleness: "fresh" | "stale" | "unknown" | string;
  summary: string;
  detail: Record<string, unknown>;
  rawRefs: Record<string, string | number | boolean | null>;
}

export interface ContextFill {
  contextUsedRatio: number | null;
  contextWindowTokens: number | null;
  contextRemainingTokens: number | null;
  contextWindowSource: "known" | "unknown";
  reason?: string;
}

export interface SessionActivityTimeline {
  items: SessionActivityItem[];
  nextCursor: string | null;
  generatedAt: string;
  coverage: Record<string, string>;
  contextFill: ContextFill;
}

/**
 * ⛔ DROPPED — NOT modeled here, by design (gap-doc §2 "Tool execution"):
 *   - ToolExecution  → a backend turn is atomic/black-box; the agent's own UI
 *     owns tool granularity. Replaced by SystemNotice (turn-level job events).
 *   - Session `archived` → folded into `closed`.
 *   - per-session connection_unknown → lives on Target health.
 *   - Task `progressPct` → use state + elapsed time.
 * Do not re-add these without a backend move that produces them.
 */
