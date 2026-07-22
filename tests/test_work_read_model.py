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
    build_case_roster,
    _is_agent_spawn,
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
    # 'cancelled' is terminal (matches db._CLOSED_STATUSES) even with a stale stage.
    assert case_bucket(_flow("a", status="cancelled")) == "closed"
    assert case_bucket(_flow("a", status="cancelled", current_stage="closure")) == "closed"
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


# --- A29 adversarial authority fixtures ------------------------------------

def test_rework_status_buckets_as_blocked():
    # A29 terminal-failure seam writes status='blocked'; rework variants join it.
    assert case_bucket(_flow("a", status="blocked")) == "blocked"
    assert case_bucket(_flow("a", status="rework")) == "blocked"
    assert case_bucket(_flow("a", status="rework_requested")) == "blocked"


def test_terminal_status_wins_over_stage():
    # Authority rule: a terminal case status overrides a stale in-flight stage —
    # a closed case that still carries current_stage='execution' renders closed,
    # not active. (The A29 outcome seam sets status alongside the closure stage.)
    assert case_bucket(_flow("a", status="closed", current_stage="execution")) == "closed"
    assert case_bucket(_flow("a", status="blocked", current_stage="execution")) == "blocked"


def test_conflict_flow_summary_vs_task_link_is_not_silently_resolved():
    # Milestone F1/authority-rule scenario: the flow SUMMARY still says 'active'
    # while a terminal task link exists in the ledger. The read model renders the
    # summary bucket AS-IS (never fabricating 'closed') AND surfaces the task link,
    # so the UI has both authoritative truths — it does not silently override one.
    flow = _flow("case-1", current_stage="execution")  # summary: no terminal status
    links = [
        {"entity_type": "task", "entity_id": "t-done", "role": "root_task",
         "created_by": "system", "created_at": "t1", "metadata_json": None},
    ]
    detail = build_case_detail(flow, links, event_count=1)
    assert detail["case"]["bucket"] == "active"        # summary rendered honestly
    assert detail["ledger"]["tasks"][0]["entity_id"] == "t-done"  # task truth present
    assert detail["coverage"]["has_links"] is True


def test_unlinked_case_renders_empty_not_inferred():
    # No links ⇒ every ledger section explicitly empty; coverage says has_links=False.
    detail = build_case_detail(_flow("bare"), [], event_count=0)
    assert all(detail["ledger"][s] == [] for s in detail["ledger"])
    assert detail["coverage"] == {
        "has_links": False, "has_events": False, "has_parent": False, "is_root": True,
    }


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


# --- [Cockpit] roster projection ------------------------------------------

def _slink(sid, role):
    return {"entity_type": "session", "entity_id": sid, "role": role}


def test_is_agent_spawn_flags_claude_cli_only():
    assert _is_agent_spawn("claude -p --model opus 'do x'") is True
    assert _is_agent_spawn("/usr/bin/claude --print hi") is True
    assert _is_agent_spawn("python train.py --epochs 50") is False
    assert _is_agent_spawn("npm run build") is False
    # 'claude' as a bare word without a CLI flag is not enough to call it a spawn.
    assert _is_agent_spawn("echo claude") is False
    assert _is_agent_spawn(None) is False


def test_build_case_roster_sessions_tokens_and_totals():
    links = [_slink("mgr1", "manager"), _slink("wk1", "worker")]
    session_rows = {
        "mgr1": {"backend": "claude", "status": "idle", "model": "sonnet",
                 "machine_id": "", "updated_at": "t9",
                 "last_result_summary": "reviewed T1"},
        "wk1": {"backend": "claude", "status": "busy", "model": "opus",
                "machine_id": "Horse", "updated_at": "t8", "last_summary": "building"},
    }
    tokens = {
        "mgr1": {"input": 100, "output": 20, "cache_read": 5, "cache_creation": 0, "total": 120},
        "wk1": {"input": 400, "output": 60, "cache_read": 10, "cache_creation": 1, "total": 460},
    }
    turns = {"mgr1": 3, "wk1": 7}
    roster = build_case_roster("case1", links, session_rows, tokens, turns, [])

    assert roster["flow_run_id"] == "case1"
    assert roster["counts"] == {"sessions": 2, "jobs": 0, "running_jobs": 0}
    mgr = next(s for s in roster["sessions"] if s["session_id"] == "mgr1")
    wk = next(s for s in roster["sessions"] if s["session_id"] == "wk1")
    assert mgr["role"] == "manager" and mgr["model"] == "sonnet" and mgr["turn_count"] == 3
    assert mgr["node"] == "__local__" and mgr["last_report"] == "reviewed T1"
    assert wk["role"] == "worker" and wk["node"] == "Horse" and wk["tokens"]["total"] == 460
    # Case token total is the sum across sessions (manager + worker).
    assert roster["token_totals"]["total"] == 580
    assert roster["token_totals"]["input"] == 500


def test_build_case_roster_dedupes_dual_role_session():
    # flow_links unique key includes role, so one session can appear twice on a
    # case (manager + worker). It must render ONCE and its tokens count ONCE.
    links = [_slink("s1", "manager"), _slink("s1", "worker")]
    tokens = {"s1": {"input": 100, "output": 100, "cache_read": 0, "cache_creation": 0, "total": 200}}
    roster = build_case_roster("c", links, {}, tokens, {"s1": 2}, [])
    assert roster["counts"]["sessions"] == 1
    assert [s["session_id"] for s in roster["sessions"]] == ["s1"]
    assert roster["token_totals"]["total"] == 200  # not 400


def test_build_case_roster_missing_session_row_is_honest():
    # A linked session whose row is gone renders present=false, not dropped.
    links = [_slink("ghost", "worker")]
    roster = build_case_roster("c", links, {}, {}, {}, [])
    assert roster["sessions"][0]["present"] is False
    assert roster["sessions"][0]["tokens"]["total"] == 0
    assert roster["sessions"][0]["turn_count"] == 0


def test_build_case_roster_jobs_flags_and_running_count():
    jobs = [
        {"id": "job_a", "label": "IGNITION_1", "command": "claude -p --model opus 'x'",
         "session_id": "mgr1", "node_id": "kanebra", "status": "running",
         "started_at": "t1", "started_epoch": 1000.0, "finished_at": None,
         "exit_code": None, "tail": None, "orphaned": 0},
        {"id": "job_b", "label": "train", "command": "python train.py",
         "session_id": "mgr1", "node_id": "kanebra", "status": "lost",
         "started_at": "t0", "started_epoch": 900.0, "finished_at": None,
         "exit_code": None, "tail": "killed", "orphaned": 1},
    ]
    roster = build_case_roster("c", [], {}, {}, {}, jobs)
    assert roster["counts"] == {"sessions": 0, "jobs": 2, "running_jobs": 1}
    ja = next(j for j in roster["jobs"] if j["job_id"] == "job_a")
    jb = next(j for j in roster["jobs"] if j["job_id"] == "job_b")
    # The claude -p job is flagged as an agent-spawn (the exact misuse to surface).
    assert ja["is_agent_spawn"] is True and ja["status"] == "running"
    # The lost/orphaned training job is surfaced honestly, not an agent-spawn.
    assert jb["is_agent_spawn"] is False and jb["orphaned"] is True and jb["status"] == "lost"
    # Duration is left to the client (epoch passed through, no clock in the pure fn).
    assert ja["started_epoch"] == 1000.0
