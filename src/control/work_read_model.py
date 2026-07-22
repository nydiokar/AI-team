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
_CLOSED_STATUSES = {"closed", "cancelled", "superseded", "done", "complete", "completed"}
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
    row deterministically — and because ``db.list_session_case_links`` is ordered
    newest-link-first, that FIRST row is the session's MOST RECENT case ("what is
    it on now?"). We never fabricate a "primary" the substrate did not assert.
    Covers the WHOLE substrate (no per-case fanout, no cap).

    [A37] Post-A36 this "most recent" is honest, not a shatter mask: a session now
    holds ONE session link per Case (not one per turn), so "what is it on now?"
    reflects real Case membership. The durable ``sessions.current_case_id`` is the
    authoritative current-Case pointer; this projection is the Sessions-surface
    view over the link ledger (current + history).

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


def _truncate(text: Optional[str], limit: int) -> Optional[str]:
    if not text:
        return None
    t = str(text).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _is_agent_spawn(command: Optional[str]) -> bool:
    """Heuristic: does this watched-job command invoke an agent CLI (the exact
    misuse we want visible — a Manager shelling out `claude -p …` as a worker)?
    Flags the `claude` binary invoked with a print/model flag. Best-effort only,
    labeled as such in the UI; never authoritative."""
    if not command:
        return False
    c = command.lower()
    if "claude" not in c:
        return False
    return any(tok in c for tok in (" -p", "--print", "--model", "claude -", "/claude "))


def _roster_session(
    link_row: Dict[str, Any],
    session_row: Optional[Dict[str, Any]],
    tokens: Optional[Dict[str, int]],
    turn_count: int,
) -> Dict[str, Any]:
    s = session_row or {}
    tok = tokens or {}
    return {
        "session_id": link_row.get("entity_id"),
        "role": _session_role(link_row.get("role")),
        "present": session_row is not None,  # False ⇒ linked session row is gone
        "backend": s.get("backend"),
        "status": s.get("status"),
        "model": s.get("model"),
        "node": s.get("machine_id") or "__local__",
        "last_activity": s.get("updated_at"),
        "last_report": _truncate(
            s.get("last_result_summary") or s.get("last_summary"), 200
        ),
        "turn_count": turn_count,
        "tokens": {
            "input": tok.get("input", 0),
            "output": tok.get("output", 0),
            "cache_read": tok.get("cache_read", 0),
            "cache_creation": tok.get("cache_creation", 0),
            "total": tok.get("total", 0),
        },
    }


def _roster_job(job_row: Dict[str, Any]) -> Dict[str, Any]:
    command = job_row.get("command")
    status = (job_row.get("status") or "").strip().lower()
    return {
        "job_id": job_row.get("id"),
        "label": job_row.get("label"),
        "command_summary": _truncate(command, 140),
        "session_id": job_row.get("session_id"),
        "node": job_row.get("node_id"),
        "status": status or "unknown",
        # Duration is derived on the client from started_epoch vs. now — the read
        # model stays pure (no clock) and tz-safe (epoch, not a string timestamp).
        "started_at": job_row.get("started_at"),
        "started_epoch": job_row.get("started_epoch"),
        "finished_at": job_row.get("finished_at"),
        "exit_code": job_row.get("exit_code"),
        "tail": _truncate(job_row.get("tail"), 200),
        "orphaned": bool(job_row.get("orphaned")),
        # A running job flagged lost/failed by the worker daemon, or orphaned, is
        # the honest "is a script stuck / did a worker die (e.g. quota)?" signal —
        # read from worker-maintained state, never probed from this read path.
        "is_agent_spawn": _is_agent_spawn(command),
    }


def build_case_roster(
    flow_run_id: str,
    session_link_rows: List[Dict[str, Any]],
    session_rows_by_id: Dict[str, Dict[str, Any]],
    token_totals: Dict[str, Dict[str, int]],
    turn_counts: Dict[str, int],
    job_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """[Cockpit] The operational "who is doing what right now" projection for a Case.

    PURE — the control API fetches the session links, the session rows, the batched
    token/turn aggregates, and the jobs-for-those-sessions, then passes them in. The
    roster is the live HEAD of the Case; the flow_events timeline remains the spine.

    Honesty-first: a linked session whose row is gone renders present=false rather
    than being dropped; token/turn totals default to 0 when unrecorded; job
    orphaned/lost states come from worker-maintained job status, never a live probe.
    """
    sessions: List[Dict[str, Any]] = []
    seen_sids: set = set()
    for link in session_link_rows or []:
        sid = link.get("entity_id")
        # The flow_links unique key is (case, entity_type, entity_id, ROLE), so the
        # same session CAN appear twice on a case under two roles. Dedupe by
        # session id (keep first) so a session is one roster row and its tokens are
        # summed into the case total once, not doubled.
        if sid is not None and sid in seen_sids:
            continue
        if sid is not None:
            seen_sids.add(sid)
        sessions.append(
            _roster_session(
                link,
                session_rows_by_id.get(sid),
                token_totals.get(sid),
                turn_counts.get(sid, 0),
            )
        )
    jobs = [_roster_job(j) for j in (job_rows or [])]
    running_jobs = sum(1 for j in jobs if j["status"] == "running")
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}
    for s in sessions:
        for k in totals:
            totals[k] += s["tokens"].get(k, 0)
    return {
        "flow_run_id": flow_run_id,
        "sessions": sessions,
        "jobs": jobs,
        "counts": {
            "sessions": len(sessions),
            "jobs": len(jobs),
            "running_jobs": running_jobs,
        },
        "token_totals": totals,
    }


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
