"""
A27 — Work/Case read model (pure projections over the A25/A26 substrate).

This module is PURE: every function takes already-fetched DB rows (dicts/lists)
and returns a plain JSON-able shape. It never opens a DB connection — the control
API fetches rows via db helpers and passes them in (mirrors session_timeline
discipline). That keeps the projection unit-testable and free of N+1 surprises.

Honesty-first (WORK_CONTROL_SUBSTRATE_MILESTONE.md authority rules):
  * State is derived ONLY from authoritative fields — flow_runs.status/current_stage,
    flow_links, flow_events, and the direct parent_flow_run_id column. NEVER from
    timestamps, last-task adjacency, or transcript prose.
  * Missing/absent relationships render as empty/`unknown`, never inferred.
  * current_stage is the mutable summary; flow_events is the audit trail. This
    module reads them for DISPLAY only — nothing here drives execution.
"""

from typing import Any, Dict, List, Optional


# Attention buckets for the mobile default screen (operations inbox).
BUCKETS = ("needs_decision", "blocked", "review", "active", "closed", "unknown")

# Terminal / special case-status values (authoritative flow_runs.status).
_CLOSED_STATUSES = {"closed", "superseded", "done", "complete", "completed"}
_BLOCKED_STATUSES = {"blocked", "rework", "rework_requested"}
_REVIEW_STATUSES = {"review", "in_review", "review_requested"}
_DECISION_STATUSES = {"needs_decision", "awaiting_operator", "awaiting_approval"}
_REVIEW_STAGES = {"plan_review", "impl_review"}

_CASE_SUMMARY_FIELDS = (
    "flow_run_id",
    "task_id",
    "objective_lock",
    "current_stage",
    "status",
    "created_at",
    "updated_at",
    "parent_flow_run_id",
    "dispatched_by",
    "dispatch_file",
)

_ENTITY_SECTIONS = {
    "task": "tasks",
    "session": "sessions",
    "approval": "approvals",
    "artifact": "artifacts",
    "job": "jobs",
    "flow": "flows",
}


def case_bucket(flow_row: Dict[str, Any]) -> str:
    """Derive the attention bucket from AUTHORITATIVE status/stage only.

    Never guesses "closed": a case with no status and no stage is `unknown`, and
    a case with a stage but no terminal status is `active` (in-flight), not done.
    """
    status = (flow_row.get("status") or "").strip().lower()
    stage = (flow_row.get("current_stage") or "").strip().lower()
    if status in _CLOSED_STATUSES:
        return "closed"
    if status in _BLOCKED_STATUSES:
        return "blocked"
    if status in _DECISION_STATUSES:
        return "needs_decision"
    if status in _REVIEW_STATUSES or stage in _REVIEW_STAGES:
        return "review"
    if stage or status:
        return "active"
    return "unknown"


def _case_summary(flow_row: Dict[str, Any]) -> Dict[str, Any]:
    summary = {k: flow_row.get(k) for k in _CASE_SUMMARY_FIELDS}
    summary["bucket"] = case_bucket(flow_row)
    return summary


def build_work_list(flow_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Project flow_runs rows into Work/Case summaries + bucket tallies.

    Small by design — no per-case link/event queries (those belong to the detail
    endpoint). `bucket_counts` gives the inbox its section sizes.
    """
    cases = [_case_summary(r) for r in flow_rows]
    counts = {b: 0 for b in BUCKETS}
    for c in cases:
        counts[c["bucket"]] = counts.get(c["bucket"], 0) + 1
    return {"cases": cases, "bucket_counts": counts, "total": len(cases)}


def _link_view(link_row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "entity_type": link_row.get("entity_type"),
        "entity_id": link_row.get("entity_id"),
        "role": link_row.get("role"),
        "created_by": link_row.get("created_by"),
        "created_at": link_row.get("created_at"),
        "metadata_json": link_row.get("metadata_json"),
    }


def build_case_ledger(link_rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group authoritative links into ledger sections by entity type.

    Sections are always present (empty list when unlinked) so the UI shows an
    explicit "none linked" instead of inferring from other signals.
    """
    ledger: Dict[str, List[Dict[str, Any]]] = {
        "tasks": [], "sessions": [], "approvals": [],
        "artifacts": [], "jobs": [], "flows": [], "other": [],
    }
    for link in link_rows or []:
        section = _ENTITY_SECTIONS.get(link.get("entity_type"), "other")
        ledger[section].append(_link_view(link))
    return ledger


def build_case_detail(
    flow_row: Dict[str, Any],
    link_rows: List[Dict[str, Any]],
    event_count: int,
    parent_row: Optional[Dict[str, Any]] = None,
    child_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Full case detail: summary + full record + grouped ledger + lineage.

    `parent` is the resolved parent case summary (or None → root). `children`
    are resolved child-case summaries. Coverage flags make partial data explicit.
    """
    ledger = build_case_ledger(link_rows)
    children = [_case_summary(r) for r in (child_rows or [])]
    parent = _case_summary(parent_row) if parent_row else None
    linked_total = sum(len(v) for v in ledger.values())
    return {
        "case": _case_summary(flow_row),
        "record": flow_row,  # full row as-is; NULLs serialize as JSON null
        "ledger": ledger,
        "parent": parent,
        "children": children,
        "counts": {
            "links": linked_total,
            "events": event_count,
            "children": len(children),
        },
        "coverage": {
            "has_links": linked_total > 0,
            "has_events": event_count > 0,
            "has_parent": parent is not None,
            "is_root": flow_row.get("parent_flow_run_id") is None,
        },
    }


def _event_view(event_row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": event_row.get("id"),
        "event_type": event_row.get("event_type"),
        "actor": event_row.get("actor"),
        "from_state": event_row.get("from_state"),
        "to_state": event_row.get("to_state"),
        "entity_type": event_row.get("entity_type"),
        "entity_id": event_row.get("entity_id"),
        "payload_json": event_row.get("payload_json"),
        "created_at": event_row.get("created_at"),
    }


def build_case_timeline(
    flow_run_id: str,
    event_rows: List[Dict[str, Any]],
    link_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The case audit trail: append-only flow_events in order + linked evidence
    references. Evidence is a pointer list (entity ids) — bulk content stays in
    the existing per-entity surfaces (task results, session timelines)."""
    events = [_event_view(e) for e in (event_rows or [])]
    evidence = [_link_view(l) for l in (link_rows or [])]
    return {
        "flow_run_id": flow_run_id,
        "events": events,
        "evidence": evidence,
        "event_count": len(events),
    }


# Authoritative session roles a link may carry (flow_links.role vocab for a
# session entity). Anything else collapses to the generic "session".
_SESSION_ROLES = ("manager", "worker", "reviewer", "evidence")


def _session_role(role: Optional[str]) -> str:
    r = (role or "").strip().lower()
    return r if r in _SESSION_ROLES else "session"


def build_session_affiliations(
    link_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Project session→case links (db.list_session_case_links rows) into an
    authoritative affiliation index for the Sessions surface.

    Honesty-first: a session appears here ONLY if the substrate links it to a
    case; absent sessions are simply not present (the UI then renders Standalone,
    never inferred). A session linked by more than one case resolves to the FIRST
    (oldest link) deterministically — we never fabricate a "primary" the substrate
    did not assert. Covers the WHOLE substrate (no per-case fanout, no cap).

    The case's raw ``objective_lock`` rides along so the FRONTEND derives the
    display title with the SAME ``caseTitle`` logic it uses for the Work list and
    detail — one source of truth, so a case never shows two different titles.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for row in link_rows or []:
        sid = row.get("session_id")
        if not sid or sid in seen:
            continue
        seen[sid] = {
            "session_id": sid,
            "flow_run_id": row.get("flow_run_id"),
            "role": _session_role(row.get("role")),
            "objective_lock": row.get("objective_lock"),
            "case_status": row.get("status"),
        }
    affiliations = list(seen.values())
    return {"affiliations": affiliations, "total": len(affiliations)}


def build_case_graph(
    flow_run_id: str,
    flow_row: Dict[str, Any],
    parent_row: Optional[Dict[str, Any]] = None,
    child_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compact lineage graph (navigation, NOT an editable canvas): the case, its
    parent, and its direct children, with parent→child edges from AUTHORITATIVE
    lineage (parent_flow_run_id / child_flow links)."""
    def _node(row: Dict[str, Any], rel: str) -> Dict[str, Any]:
        return {
            "flow_run_id": row.get("flow_run_id"),
            "rel": rel,  # self | parent | child
            "current_stage": row.get("current_stage"),
            "status": row.get("status"),
            "bucket": case_bucket(row),
            "objective_lock": row.get("objective_lock"),
        }

    nodes: List[Dict[str, Any]] = [_node(flow_row, "self")]
    edges: List[Dict[str, Any]] = []
    if parent_row:
        nodes.append(_node(parent_row, "parent"))
        edges.append({"from": parent_row.get("flow_run_id"), "to": flow_run_id,
                      "role": "child_flow"})
    for child in (child_rows or []):
        nodes.append(_node(child, "child"))
        edges.append({"from": flow_run_id, "to": child.get("flow_run_id"),
                      "role": "child_flow"})
    return {"flow_run_id": flow_run_id, "nodes": nodes, "edges": edges}
