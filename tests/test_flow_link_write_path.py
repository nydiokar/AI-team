"""
A26 — Flow link/event write path tests (shadow, flag-guarded).

When a flow is created the orchestrator records authoritative case relationships
and an append-only audit trail, WITHOUT changing execution:

  * flag ON  ⇒ flow.created event + root_task link at flow creation; a child
    dispatch adds a child_flow link + task.dispatched event on the PARENT flow
    (CONSUMING A26a's stamped edge — no second stamping hook); stage transitions
    append flow.stage_changed events.
  * flag OFF ⇒ NO flow_links/flow_events writes at all (byte-identical to A19/A22).

Non-negotiable safety: a forced link/event write failure is swallowed and can
NEVER raise into the dispatch path.

Helpers run as real *bound* methods on a bare orchestrator (``__new__``), with a
real temp MeshDB wired in as get_db() — mirrors tests/test_dispatch_lineage.py.
"""

import types

import pytest

from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _orch() -> TaskOrchestrator:
    return TaskOrchestrator.__new__(TaskOrchestrator)


def _task(task_id, metadata=None):
    return types.SimpleNamespace(id=task_id, metadata=metadata)


# [A36] Under flag-ON admission an ordinary turn no longer mints a Case — only a
# dispatched/managed task births a flow_run. A managed-root marker drives the
# birth path these link/event tests exercise (a lineage-stamped child is the
# other birth path, covered explicitly by the child-dispatch test).
def _managed_task(task_id, metadata=None):
    meta = dict(metadata or {})
    meta[TaskOrchestrator._MANAGED_CASE_META_KEY] = True
    return _task(task_id, meta)


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("HARNESS_FLOW_DRIVE", raising=False)


def _patch_db(monkeypatch, db):
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)


# ---------------------------------------------------------------------------
# (1) ON: flow creation records flow.created + root_task link
# ---------------------------------------------------------------------------

def test_on_flow_creation_records_event_and_root_link(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    fid = orch._record_flow_run_start(_managed_task("task-root"))

    events = db.list_flow_events(fid)
    assert [e["event_type"] for e in events] == ["flow.created"]
    assert events[0]["entity_id"] == "task-root"

    links = db.list_flow_links(flow_run_id=fid)
    assert len(links) == 1
    assert (links[0]["entity_type"], links[0]["entity_id"], links[0]["role"]) == (
        "task", "task-root", "root_task",
    )


# ---------------------------------------------------------------------------
# (2) ON: a child dispatch records child_flow link + task.dispatched on PARENT
# ---------------------------------------------------------------------------

def test_on_child_dispatch_records_child_flow_link_on_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    # Parent flow already exists with its id stashed (as A26a would).
    parent_fid = db.create_flow_run("task-parent", "intent")
    parent = _task("task-parent", {TaskOrchestrator._FLOW_RUN_META_KEY: parent_fid})

    # Parent dispatches a child: A26a stamps lineage, then the child is recorded.
    child = _task("task-child")
    orch._stamp_child_dispatch_lineage(child, parent, dispatch_file="AGENT_99.md")
    child_fid = orch._record_flow_run_start(child)

    # Authoritative child_flow link lives on the PARENT, pointing at the child flow.
    child_links = db.list_flow_links(flow_run_id=parent_fid, role="child_flow")
    assert len(child_links) == 1
    assert child_links[0]["entity_type"] == "flow"
    assert child_links[0]["entity_id"] == child_fid
    assert child_links[0]["created_by"] == "task-parent"  # dispatched_by

    # Audit event on the parent.
    disp = [e for e in db.list_flow_events(parent_fid) if e["event_type"] == "task.dispatched"]
    assert len(disp) == 1
    assert disp[0]["entity_id"] == child_fid

    # Convenience index (A26a) still set on the child row — both, coherently.
    assert db.get_flow_run(child_fid)["parent_flow_run_id"] == parent_fid
    # Reverse recovery works via the authoritative ledger too.
    rev = db.list_flow_links(entity_type="flow", entity_id=child_fid, role="child_flow")
    assert rev and rev[0]["flow_run_id"] == parent_fid


# ---------------------------------------------------------------------------
# (3) ON: stage transitions append flow.stage_changed events
# ---------------------------------------------------------------------------

def test_on_stage_transition_appends_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    task = _managed_task("task-seq")
    orch._record_flow_run_start(task)  # flow.created
    orch._flow_stage_transition(task, "execution")
    orch._flow_stage_transition(task, "closure")

    fid = task.metadata[TaskOrchestrator._FLOW_RUN_META_KEY]
    types_seq = [e["event_type"] for e in db.list_flow_events(fid)]
    assert types_seq == ["flow.created", "flow.stage_changed", "flow.stage_changed"]
    changed = [e for e in db.list_flow_events(fid) if e["event_type"] == "flow.stage_changed"]
    assert [e["to_state"] for e in changed] == ["execution", "closure"]


# ---------------------------------------------------------------------------
# (4) OFF: no link/event writes at all (byte-identical)
# ---------------------------------------------------------------------------

def test_off_writes_no_links_or_events(tmp_path, monkeypatch):
    # flag unset by fixture
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    fid = orch._record_flow_run_start(_task("task-off"))
    orch._flow_stage_transition(_task("task-off", {TaskOrchestrator._FLOW_RUN_META_KEY: fid}),
                                "execution")

    assert db.list_flow_links(flow_run_id=fid) == []
    assert db.list_flow_events(fid) == []
    # The legacy flow_runs record is unchanged (A19 dispatch_start).
    assert db.get_flow_run(fid)["current_stage"] == "dispatch_start"


# ---------------------------------------------------------------------------
# (5) Fault isolation: a forced link/event write failure never raises
# ---------------------------------------------------------------------------

def test_link_write_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("link boom")

    monkeypatch.setattr(db, "create_flow_link", boom)
    monkeypatch.setattr(db, "append_flow_event", boom)
    _patch_db(monkeypatch, db)
    orch = _orch()

    # The flow row is still created and the id returned; the broken substrate
    # writes are swallowed — dispatch is unaffected.
    fid = orch._record_flow_run_start(_managed_task("task-boom"))
    assert fid is not None
    assert db.get_flow_run(fid) is not None
