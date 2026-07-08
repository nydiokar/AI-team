"""
A27 — pure Work/Case read-model builder tests (no DB, no HTTP).

Proves the projection is honest: buckets derive only from authoritative
status/current_stage (never guessing "closed"), missing links render as empty
ledger sections, and lineage graph edges come from parent/child rows only.
"""

from src.control.work_read_model import (
    BUCKETS,
    case_bucket,
    build_work_list,
    build_case_ledger,
    build_case_detail,
    build_case_timeline,
    build_case_graph,
)


def _flow(fid, **kw):
    base = {
        "flow_run_id": fid, "task_id": None, "objective_lock": None,
        "current_stage": None, "status": None, "created_at": "t0",
        "updated_at": None, "parent_flow_run_id": None, "dispatched_by": None,
        "dispatch_file": None,
    }
    base.update(kw)
    return base


# --- bucket derivation (authoritative fields only) -------------------------

def test_case_bucket_from_status_and_stage():
    assert case_bucket(_flow("a", status="closed")) == "closed"
    assert case_bucket(_flow("a", status="superseded")) == "closed"
    assert case_bucket(_flow("a", status="blocked")) == "blocked"
    assert case_bucket(_flow("a", status="needs_decision")) == "needs_decision"
    assert case_bucket(_flow("a", status="review_requested")) == "review"
    assert case_bucket(_flow("a", current_stage="impl_review")) == "review"
    # A stage but no terminal status = in-flight, NOT closed.
    assert case_bucket(_flow("a", current_stage="execution")) == "active"
    # Nothing known ⇒ unknown, never guessed.
    assert case_bucket(_flow("a")) == "unknown"


def test_bucket_membership():
    for row in (_flow("a"), _flow("b", status="closed"), _flow("c", current_stage="intent")):
        assert case_bucket(row) in BUCKETS


# --- work list -------------------------------------------------------------

def test_build_work_list_counts_buckets():
    rows = [
        _flow("a", current_stage="execution"),     # active
        _flow("b", status="blocked"),              # blocked
        _flow("c", status="closed"),               # closed
        _flow("d"),                                # unknown
    ]
    model = build_work_list(rows)
    assert model["total"] == 4
    assert model["bucket_counts"]["active"] == 1
    assert model["bucket_counts"]["blocked"] == 1
    assert model["bucket_counts"]["closed"] == 1
    assert model["bucket_counts"]["unknown"] == 1
    # Summaries carry the bucket + honest nulls.
    a = next(c for c in model["cases"] if c["flow_run_id"] == "a")
    assert a["bucket"] == "active" and a["status"] is None


# --- ledger grouping -------------------------------------------------------

def test_build_case_ledger_groups_and_keeps_empty_sections():
    links = [
        {"entity_type": "task", "entity_id": "t1", "role": "root_task"},
        {"entity_type": "session", "entity_id": "s1", "role": "worker"},
        {"entity_type": "flow", "entity_id": "f2", "role": "child_flow"},
        {"entity_type": "weird", "entity_id": "x1", "role": "evidence"},
    ]
    ledger = build_case_ledger(links)
    assert [l["entity_id"] for l in ledger["tasks"]] == ["t1"]
    assert [l["entity_id"] for l in ledger["sessions"]] == ["s1"]
    assert [l["entity_id"] for l in ledger["flows"]] == ["f2"]
    assert [l["entity_id"] for l in ledger["other"]] == ["x1"]  # unknown type → other
    # Unlinked sections are present-and-empty, not missing.
    assert ledger["approvals"] == [] and ledger["artifacts"] == [] and ledger["jobs"] == []


def test_build_case_ledger_empty_input():
    ledger = build_case_ledger([])
    assert all(v == [] for v in ledger.values())


# --- detail ----------------------------------------------------------------

def test_build_case_detail_coverage_and_lineage():
    flow = _flow("child", current_stage="execution", parent_flow_run_id="parent")
    parent = _flow("parent", current_stage="closure")
    links = [{"entity_type": "task", "entity_id": "t1", "role": "root_task"}]
    detail = build_case_detail(flow, links, event_count=3, parent_row=parent, child_rows=[])

    assert detail["case"]["flow_run_id"] == "child"
    assert detail["record"] is flow  # full row echoed as-is
    assert detail["parent"]["flow_run_id"] == "parent"
    assert detail["counts"] == {"links": 1, "events": 3, "children": 0}
    assert detail["coverage"] == {
        "has_links": True, "has_events": True, "has_parent": True, "is_root": False,
    }


def test_build_case_detail_root_with_no_links():
    flow = _flow("root", current_stage="intent")
    detail = build_case_detail(flow, [], event_count=0)
    assert detail["parent"] is None
    assert detail["coverage"]["is_root"] is True
    assert detail["coverage"]["has_links"] is False
    assert detail["children"] == []


# --- timeline --------------------------------------------------------------

def test_build_case_timeline_orders_events_and_lists_evidence():
    events = [
        {"id": 1, "event_type": "flow.created", "actor": "system"},
        {"id": 2, "event_type": "flow.stage_changed", "actor": "system", "to_state": "execution"},
    ]
    links = [{"entity_type": "task", "entity_id": "t1", "role": "root_task"}]
    tl = build_case_timeline("case-1", events, links)
    assert [e["event_type"] for e in tl["events"]] == ["flow.created", "flow.stage_changed"]
    assert tl["event_count"] == 2
    assert tl["evidence"][0]["entity_id"] == "t1"


# --- graph -----------------------------------------------------------------

def test_build_case_graph_nodes_and_edges():
    flow = _flow("me", current_stage="execution", parent_flow_run_id="dad")
    parent = _flow("dad", current_stage="closure")
    kids = [_flow("kid1", current_stage="intent"), _flow("kid2", status="blocked")]
    g = build_case_graph("me", flow, parent, kids)

    rels = {n["flow_run_id"]: n["rel"] for n in g["nodes"]}
    assert rels == {"me": "self", "dad": "parent", "kid1": "child", "kid2": "child"}
    assert {"from": "dad", "to": "me", "role": "child_flow"} in g["edges"]
    assert {"from": "me", "to": "kid1", "role": "child_flow"} in g["edges"]
    assert {"from": "me", "to": "kid2", "role": "child_flow"} in g["edges"]
    # Bucket surfaces on graph nodes too (blocked child visible at a glance).
    kid2 = next(n for n in g["nodes"] if n["flow_run_id"] == "kid2")
    assert kid2["bucket"] == "blocked"
