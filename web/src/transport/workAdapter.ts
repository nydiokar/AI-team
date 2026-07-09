/**
 * Raw A27 Work read model → canonical ../domain/work types.
 *
 * The ONLY derivation here is DISPLAY formatting: a short human title from the
 * case's own `objective_lock` text (see `caseTitle`). Buckets, ledger grouping,
 * lineage and coverage are copied verbatim from the authoritative server model
 * — this layer never infers a relationship the API did not provide.
 */
import type {
  RawCaseSummary,
  RawWorkListResponse,
  RawFlowLink,
  RawCaseLedger,
  RawCaseDetailResponse,
  RawFlowEvent,
  RawCaseTimelineResponse,
  RawGraphNode,
  RawCaseGraphResponse,
  RawSessionAffiliationsResponse,
  RawWorkBucket,
} from "./rawApi";
import type {
  CaseSummary,
  CaseLink,
  CaseLedger,
  CaseDetail,
  CaseEvent,
  CaseTimeline,
  CaseGraph,
  CaseGraphNode,
  WorkList,
  WorkBucket,
  CaseSessionRole,
  SessionAffiliation,
} from "../domain/work";

const BUCKETS: WorkBucket[] = [
  "needs_decision",
  "blocked",
  "review",
  "active",
  "closed",
  "unknown",
];

/** Short id form for a fallback title ("case a1b2c3d4"). */
function shortId(id: string): string {
  const tail = id.replace(/^[a-z_]+[-_]?/i, "");
  return tail.length >= 6 ? tail.slice(0, 8) : id.slice(0, 12);
}

/**
 * Derive a short display title from a case's OWN objective text. The dispatch
 * objective_lock is often an XML `<objective_lock>` block; we prefer the human
 * `<real_objective>` / `<task_name>`, else the first non-empty prose line, and
 * always fall back to a short flow id so a title is never empty or a giant blob.
 *
 * This is pure presentation of the case's own data — not an inferred link.
 */
export function caseTitle(
  objectiveLock: string | null | undefined,
  flowRunId: string,
): string {
  const raw = (objectiveLock ?? "").trim();
  if (!raw) return `case ${shortId(flowRunId)}`;

  const pick = (tag: string): string | null => {
    const m = raw.match(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`, "i"));
    if (!m) return null;
    const inner = m[1].replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    return inner || null;
  };

  const chosen =
    pick("real_objective") ||
    pick("interpreted_task") ||
    pick("task_name") ||
    // No known tag: strip any markup and take the first meaningful line.
    raw
      .replace(/<[^>]+>/g, " ")
      .split(/\n+/)
      .map((l) => l.trim())
      .find((l) => l.length > 0) ||
    `case ${shortId(flowRunId)}`;

  return chosen.length > 120 ? `${chosen.slice(0, 117)}…` : chosen;
}

function normBucket(b: RawWorkBucket | null | undefined): WorkBucket {
  return b && (BUCKETS as string[]).includes(b) ? (b as WorkBucket) : "unknown";
}

export function toCaseSummary(raw: RawCaseSummary): CaseSummary {
  return {
    flowRunId: raw.flow_run_id,
    taskId: raw.task_id ?? null,
    title: caseTitle(raw.objective_lock, raw.flow_run_id),
    objectiveLock: raw.objective_lock ?? null,
    currentStage: raw.current_stage ?? null,
    status: raw.status ?? null,
    bucket: normBucket(raw.bucket),
    createdAt: raw.created_at ?? null,
    updatedAt: raw.updated_at ?? null,
    parentFlowRunId: raw.parent_flow_run_id ?? null,
    dispatchedBy: raw.dispatched_by ?? null,
    dispatchFile: raw.dispatch_file ?? null,
  };
}

export function toWorkList(raw: RawWorkListResponse): WorkList {
  const counts = {} as Record<WorkBucket, number>;
  for (const b of BUCKETS) counts[b] = raw.bucket_counts?.[b] ?? 0;
  return {
    cases: (raw.cases ?? []).map(toCaseSummary),
    bucketCounts: counts,
    total: raw.total ?? (raw.cases?.length ?? 0),
  };
}

function toLink(raw: RawFlowLink): CaseLink {
  return {
    entityType: raw.entity_type ?? null,
    entityId: raw.entity_id ?? null,
    role: raw.role ?? null,
    createdBy: raw.created_by ?? null,
    createdAt: raw.created_at ?? null,
  };
}

const LEDGER_SECTIONS: (keyof CaseLedger)[] = [
  "tasks",
  "sessions",
  "approvals",
  "artifacts",
  "jobs",
  "flows",
  "other",
];

export function toLedger(raw: RawCaseLedger | undefined | null): CaseLedger {
  const ledger = {} as CaseLedger;
  for (const section of LEDGER_SECTIONS) {
    ledger[section] = (raw?.[section] ?? []).map(toLink);
  }
  return ledger;
}

export function toCaseDetail(raw: RawCaseDetailResponse): CaseDetail {
  return {
    summary: toCaseSummary(raw.case),
    ledger: toLedger(raw.ledger),
    parent: raw.parent ? toCaseSummary(raw.parent) : null,
    children: (raw.children ?? []).map(toCaseSummary),
    counts: {
      links: raw.counts?.links ?? 0,
      events: raw.counts?.events ?? 0,
      children: raw.counts?.children ?? 0,
    },
    coverage: {
      hasLinks: Boolean(raw.coverage?.has_links),
      hasEvents: Boolean(raw.coverage?.has_events),
      hasParent: Boolean(raw.coverage?.has_parent),
      isRoot: Boolean(raw.coverage?.is_root),
    },
  };
}

function toEvent(raw: RawFlowEvent): CaseEvent {
  return {
    id: String(raw.id ?? `${raw.event_type ?? "event"}-${raw.created_at ?? ""}`),
    eventType: raw.event_type ?? null,
    actor: raw.actor ?? null,
    fromState: raw.from_state ?? null,
    toState: raw.to_state ?? null,
    entityType: raw.entity_type ?? null,
    entityId: raw.entity_id ?? null,
    createdAt: raw.created_at ?? null,
  };
}

export function toCaseTimeline(raw: RawCaseTimelineResponse): CaseTimeline {
  return {
    flowRunId: raw.flow_run_id,
    events: (raw.events ?? []).map(toEvent),
    evidence: (raw.evidence ?? []).map(toLink),
    eventCount: raw.event_count ?? (raw.events?.length ?? 0),
  };
}

function toGraphNode(raw: RawGraphNode): CaseGraphNode {
  const id = raw.flow_run_id ?? "";
  return {
    flowRunId: id,
    rel: raw.rel,
    title: caseTitle(raw.objective_lock, id),
    currentStage: raw.current_stage ?? null,
    status: raw.status ?? null,
    bucket: normBucket(raw.bucket),
  };
}

export function toCaseGraph(raw: RawCaseGraphResponse): CaseGraph {
  return {
    flowRunId: raw.flow_run_id,
    nodes: (raw.nodes ?? []).map(toGraphNode),
    edges: (raw.edges ?? []).map((e) => ({
      from: e.from ?? null,
      to: e.to ?? null,
      role: e.role ?? null,
    })),
  };
}

// ── Session affiliation index ──────────────────────────────────────────────
// Given resolved case details, build the authoritative map from a session id to
// its role within a case, sourced ONLY from each case's `ledger.sessions`. A
// session absent from every ledger has NO entry here (renders as standalone).
const KNOWN_SESSION_ROLES: CaseSessionRole[] = [
  "manager",
  "worker",
  "reviewer",
  "evidence",
  "session",
];

export function normalizeSessionRole(role: string | null): CaseSessionRole {
  const r = (role ?? "").trim().toLowerCase();
  return (KNOWN_SESSION_ROLES as string[]).includes(r)
    ? (r as CaseSessionRole)
    : "session";
}

/**
 * Build the session→case affiliation Map from the authoritative whole-substrate
 * index (GET /api/work/affiliations/sessions). One entry per session, keyed by
 * session id. A row missing its flow_run_id is dropped (an affiliation with no
 * case to link to is not renderable) — never fabricated. The server already
 * deduplicates by session, but we keep the first defensively.
 */
export function toSessionAffiliationIndex(
  raw: RawSessionAffiliationsResponse,
): Map<string, SessionAffiliation> {
  const map = new Map<string, SessionAffiliation>();
  for (const a of raw.affiliations ?? []) {
    const sid = a.session_id;
    if (!sid || !a.flow_run_id || map.has(sid)) continue;
    map.set(sid, {
      sessionId: sid,
      flowRunId: a.flow_run_id,
      role: normalizeSessionRole(a.role),
      // Same derivation as the Work list/detail — one source of truth for titles.
      caseTitle: caseTitle(a.objective_lock, a.flow_run_id),
      // Authoritative case status, copied verbatim — the label derives closed vs
      // active from it (never inferred).
      caseStatus: a.case_status ?? null,
    });
  }
  return map;
}
