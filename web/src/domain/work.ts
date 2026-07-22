/**
 * Canonical Work / Case domain objects — A28 mobile Work surface.
 *
 * These are the FRONTEND shapes bound by the Work tab. They are NOT the raw
 * backend payloads (../transport/rawApi RawCase*) — those pass through
 * ../transport/workAdapter which translates the A27 read model into these.
 *
 * Honesty-first contract (WORK_CONTROL_SUBSTRATE_MILESTONE.md authority rules):
 *   * Every field here is derived ONLY from authoritative substrate data
 *     (flow_runs.status/current_stage, flow_links, flow_events, direct lineage).
 *   * Missing relationships surface as empty arrays / `null`, never inferred.
 *   * The one derivation this layer performs is DISPLAY formatting (a short
 *     title from the case's own objective text). That is presentation of the
 *     case's own data, not an inferred relationship between entities.
 */

// The six authoritative attention buckets (server-side work_read_model.BUCKETS).
export type WorkBucket =
  | "needs_decision"
  | "blocked"
  | "review"
  | "active"
  | "closed"
  | "unknown";

// A session's authoritative role within a case, from the flow_links.role vocab
// (db.FLOW_LINK_ROLES). `session` is the generic fallback when a link exists
// without a specific manager/worker/reviewer role.
export type CaseSessionRole =
  | "manager"
  | "worker"
  | "reviewer"
  | "evidence"
  | "session";

// ── Case summary (list row / lineage node) ─────────────────────────────────
export interface CaseSummary {
  flowRunId: string;
  taskId: string | null;
  /** Short display title derived from the case's own objective_lock text. */
  title: string;
  /** The raw objective text as stored (kept for the detail view / no loss). */
  objectiveLock: string | null;
  currentStage: string | null;
  status: string | null;
  bucket: WorkBucket;
  createdAt: string | null;
  updatedAt: string | null;
  parentFlowRunId: string | null;
  dispatchedBy: string | null;
  dispatchFile: string | null;
}

// ── Authoritative link (one ledger entry) ──────────────────────────────────
export interface CaseLink {
  entityType: string | null;
  entityId: string | null;
  role: string | null;
  createdBy: string | null;
  createdAt: string | null;
}

// Grouped ledger — every section present (empty ⇒ "none linked", explicit).
export interface CaseLedger {
  tasks: CaseLink[];
  sessions: CaseLink[];
  approvals: CaseLink[];
  artifacts: CaseLink[];
  jobs: CaseLink[];
  flows: CaseLink[];
  other: CaseLink[];
}

export interface CaseCoverage {
  hasLinks: boolean;
  hasEvents: boolean;
  hasParent: boolean;
  isRoot: boolean;
}

// ── Full case detail ───────────────────────────────────────────────────────
export interface CaseDetail {
  summary: CaseSummary;
  ledger: CaseLedger;
  parent: CaseSummary | null;
  children: CaseSummary[];
  counts: { links: number; events: number; children: number };
  coverage: CaseCoverage;
}

// ── Audit event (case timeline) ────────────────────────────────────────────
export interface CaseEvent {
  id: string;
  eventType: string | null;
  actor: string | null;
  fromState: string | null;
  toState: string | null;
  entityType: string | null;
  entityId: string | null;
  createdAt: string | null;
}

export interface CaseTimeline {
  flowRunId: string;
  events: CaseEvent[];
  evidence: CaseLink[];
  eventCount: number;
}

// ── Compact lineage graph (navigation, not an editable canvas) ─────────────
export interface CaseGraphNode {
  flowRunId: string;
  rel: "self" | "parent" | "child";
  title: string;
  currentStage: string | null;
  status: string | null;
  bucket: WorkBucket;
}

export interface CaseGraphEdge {
  from: string | null;
  to: string | null;
  role: string | null;
}

export interface CaseGraph {
  flowRunId: string;
  nodes: CaseGraphNode[];
  edges: CaseGraphEdge[];
}

// ── Work list (inbox) ──────────────────────────────────────────────────────
export interface WorkList {
  cases: CaseSummary[];
  bucketCounts: Record<WorkBucket, number>;
  total: number;
}

// ── Session affiliation (Sessions screen labels) ───────────────────────────
// A session's authoritative membership in a case, sourced ONLY from that case's
// flow_links ledger. If the substrate does not link a session, it has NO
// affiliation and renders as standalone — never inferred from task adjacency.
export interface SessionAffiliation {
  sessionId: string;
  flowRunId: string;
  role: CaseSessionRole;
  caseTitle: string;
  /** Authoritative status of the affiliated case (from flow_runs.status). Lets
   *  the label read a closed case as history, so an idle session is not shown as
   *  if it were on active work. `null` when the case has no status yet. */
  caseStatus: string | null;
}

// ── Case roster (Cockpit) ──────────────────────────────────────────────────
// The live operational view of a case: who is working (sessions) and what
// scripts are running (jobs). Answers the operator's "how many workers, on what,
// for how many tokens, and are any scripts stuck/orphaned?" — the thing the
// static ledger never surfaced.
export interface RosterTokens {
  input: number;
  output: number;
  cacheRead: number;
  cacheCreation: number;
  total: number;
}

export interface RosterSession {
  sessionId: string;
  role: CaseSessionRole;
  /** False ⇒ the case links this session but its row is gone (rendered honestly,
   *  not dropped). */
  present: boolean;
  backend: string | null;
  status: string | null;
  model: string | null;
  node: string | null;
  lastActivity: string | null;
  lastReport: string | null;
  turnCount: number;
  tokens: RosterTokens;
}

export interface RosterJob {
  jobId: string;
  label: string | null;
  commandSummary: string | null;
  sessionId: string | null;
  node: string | null;
  /** running | done | failed | lost | unknown (worker-maintained; never probed). */
  status: string;
  startedAt: string | null;
  /** Epoch seconds — the client derives a live duration from this (read model is
   *  clock-free/tz-safe). */
  startedEpoch: number | null;
  finishedAt: string | null;
  exitCode: number | null;
  tail: string | null;
  orphaned: boolean;
  /** Heuristic: this watched job invokes an agent CLI (`claude -p …`) — the exact
   *  off-substrate spawn the cockpit exists to make visible. */
  isAgentSpawn: boolean;
}

export interface CaseRoster {
  flowRunId: string;
  sessions: RosterSession[];
  jobs: RosterJob[];
  counts: { sessions: number; jobs: number; runningJobs: number };
  tokenTotals: RosterTokens;
}
