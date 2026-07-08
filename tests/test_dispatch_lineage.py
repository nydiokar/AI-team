"""
M2 — Dispatch lineage wiring tests.

Migration 22 (A21) already added the lineage columns
(parent_flow_run_id, dispatched_by, dispatch_file) to flow_runs. M2 WIRES the
orchestrator so that when a parent flow/task dispatches a child task, the child's
flow_runs row records the parent linkage — making child→parent recoverable with
no new schema, via ``db.list_child_flow_runs``.

Hard invariants proved here:
  1. Flag ON  ⇒ a stamped child's flow_runs row carries parent_flow_run_id /
     dispatched_by / dispatch_file.
  2. Flag OFF ⇒ stamping is a no-op and the columns stay NULL; the create call
     is byte-identical to the A19 legacy path (current_stage == "dispatch_start").
  3. Reverse-lookup parent→children works (oldest-first, dispatch order).
  4. A forced DB write failure in the dispatch-record path NEVER raises — it is
     swallowed and returns None (a telemetry write can never break a real task).

SHADOW: nothing in these paths reads the lineage columns to drive execution.
The orchestrator methods under test use only class attributes + the flag, so a
bare instance (``__new__``) is sufficient — no backend, no queue, no paid CLI.
"""

import pytest

import src.control.db as db_module
from src.control.db import MeshDB
from src.core.interfaces import Task, TaskType, TaskPriority, TaskStatus
from src.orchestrator import TaskOrchestrator


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def _orch() -> TaskOrchestrator:
    """A bare orchestrator: the lineage methods use only class attrs + the flag,
    so we skip the heavy __init__ (no backends, queue, or config needed)."""
    return TaskOrchestrator.__new__(TaskOrchestrator)


def _task(task_id: str = "task_child", metadata: dict | None = None) -> Task:
    return Task(
        id=task_id,
        type=TaskType.FIX,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING,
        created="2026-07-08T00:00:00",
        title="t",
        target_files=[],
        prompt="do the thing",
        success_criteria=[],
        context="",
        metadata=metadata if metadata is not None else {},
    )


@pytest.fixture
def db(tmp_path, monkeypatch) -> MeshDB:
    """A real temp MeshDB, wired in as the orchestrator's get_db() singleton."""
    d = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_module, "get_db", lambda: d)
    return d


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv("HARNESS_FLOW_DRIVE", raising=False)


# --------------------------------------------------------------------------- #
# (1) Flag ON: child flow_runs row records the parent linkage
# --------------------------------------------------------------------------- #

def test_flag_on_child_row_gets_lineage(db, flag_on):
    """A parent that already has a flow_run (its id stashed under
    _FLOW_RUN_META_KEY) dispatches a child; the child's row records all three
    lineage columns. This mirrors submit_instruction's stamp→record sequence."""
    orch = _orch()

    # Parent already recorded: create its flow_run and stash the id like the
    # real dispatch path does.
    parent_fr = db.create_flow_run("task_parent", "intent")
    parent = _task("task_parent", {TaskOrchestrator._FLOW_RUN_META_KEY: parent_fr})

    child = _task("task_child")
    orch._stamp_child_dispatch_lineage(
        child, parent, dispatch_file=".ai/dispatch/AGENT_99_DEMO.md"
    )

    child_fr = orch._record_flow_run_start(child)
    assert child_fr is not None

    row = db.get_flow_run(child_fr)
    assert row["parent_flow_run_id"] == parent_fr
    assert row["dispatched_by"] == "task_parent"          # defaulted to parent id
    assert row["dispatch_file"] == ".ai/dispatch/AGENT_99_DEMO.md"
    # ON path writes the §11 initial stage, not the legacy value.
    assert row["current_stage"] == "intent"


def test_flag_on_explicit_dispatched_by_wins(db, flag_on):
    """An explicit dispatched_by (e.g. 'watched_job:<id>') is recorded verbatim,
    even without a parent_task — parent_flow_run_id then stays NULL."""
    orch = _orch()
    child = _task("task_child")
    orch._stamp_child_dispatch_lineage(child, None, dispatched_by="watched_job:job_7")

    child_fr = orch._record_flow_run_start(child)
    row = db.get_flow_run(child_fr)
    assert row["dispatched_by"] == "watched_job:job_7"
    assert row["parent_flow_run_id"] is None
    assert row["dispatch_file"] is None


# --------------------------------------------------------------------------- #
# (2) Flag OFF: stamping is a no-op; columns stay NULL; byte-identical create
# --------------------------------------------------------------------------- #

def test_flag_off_stamp_is_noop(db, flag_off):
    """With the flag OFF, stamping must NOT mutate the child's metadata."""
    orch = _orch()
    parent = _task("task_parent", {TaskOrchestrator._FLOW_RUN_META_KEY: "parent-fr-1"})
    child = _task("task_child", {"user_key": "keep"})

    before = dict(child.metadata)
    orch._stamp_child_dispatch_lineage(child, parent, dispatch_file="x.md")
    assert child.metadata == before, "OFF path must leave child metadata untouched"


def test_flag_off_columns_stay_null_and_byte_identical(db, flag_off):
    """OFF path: even a child that somehow carries lineage keys records NOTHING
    into the lineage columns, and the row is the A19 legacy shape
    (current_stage == 'dispatch_start')."""
    orch = _orch()
    # A child carrying lineage keys anyway — the OFF record path must ignore them.
    child = _task("task_child", {
        TaskOrchestrator._PARENT_FLOW_RUN_META_KEY: "parent-fr-1",
        TaskOrchestrator._DISPATCHED_BY_META_KEY: "task_parent",
        TaskOrchestrator._DISPATCH_FILE_META_KEY: "x.md",
    })

    child_fr = orch._record_flow_run_start(child)
    row = db.get_flow_run(child_fr)
    assert row["parent_flow_run_id"] is None
    assert row["dispatched_by"] is None
    assert row["dispatch_file"] is None
    # Byte-identical to A19: legacy stage, and the flow_run_id is NOT stashed on
    # the task metadata (that only happens on the ON/drive path).
    assert row["current_stage"] == "dispatch_start"
    assert TaskOrchestrator._FLOW_RUN_META_KEY not in child.metadata


def test_lineage_fields_empty_for_unstamped_task():
    """An unstamped task yields no lineage kwargs ⇒ NULL columns."""
    orch = _orch()
    assert orch._dispatch_lineage_fields(_task()) == {}


# --------------------------------------------------------------------------- #
# (3) Reverse lookup: parent → children
# --------------------------------------------------------------------------- #

def test_reverse_lookup_parent_to_children(db, flag_on):
    """Given a parent flow_run, list the child flows it dispatched, in order."""
    orch = _orch()
    parent_fr = db.create_flow_run("task_parent", "intent")
    parent = _task("task_parent", {TaskOrchestrator._FLOW_RUN_META_KEY: parent_fr})

    child_frs = []
    for i in range(3):
        c = _task(f"task_child_{i}")
        orch._stamp_child_dispatch_lineage(c, parent)
        child_frs.append(orch._record_flow_run_start(c))

    # An unrelated flow with a different parent must NOT appear.
    other_parent = db.create_flow_run("task_other", "intent")
    stray = _task("task_stray")
    orch._stamp_child_dispatch_lineage(
        stray, _task("task_other", {TaskOrchestrator._FLOW_RUN_META_KEY: other_parent})
    )
    orch._record_flow_run_start(stray)

    children = db.list_child_flow_runs(parent_fr)
    # Correctness: exactly this parent's children, no strays.
    assert {c["flow_run_id"] for c in children} == set(child_frs)
    assert all(c["parent_flow_run_id"] == parent_fr for c in children)
    # Ordering contract: oldest-first (SQL ORDER BY created_at ASC), asserted
    # without relying on distinct-microsecond timestamps.
    created = [c["created_at"] for c in children]
    assert created == sorted(created)


def test_reverse_lookup_empty_when_no_children(db):
    assert db.list_child_flow_runs("nonexistent-parent") == []


# --------------------------------------------------------------------------- #
# (4) A forced DB write failure never raises into the dispatch path
# --------------------------------------------------------------------------- #

def test_db_write_failure_does_not_raise(db, flag_on, monkeypatch):
    """If create_flow_run blows up, _record_flow_run_start swallows it and
    returns None — a telemetry write can NEVER break a real dispatch."""
    orch = _orch()

    def boom(*a, **k):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "create_flow_run", boom)

    child = _task("task_child")
    orch._stamp_child_dispatch_lineage(child, None, dispatched_by="watched_job:x")

    # Must not raise.
    assert orch._record_flow_run_start(child) is None


def test_stamp_never_raises_on_bad_input(flag_on):
    """The stamp helper is best-effort: a pathological child object cannot make
    it raise into the dispatch path."""
    orch = _orch()

    class Bad:
        id = "bad"

        @property
        def metadata(self):
            raise RuntimeError("no metadata for you")

    # Should log-and-return, not raise.
    orch._stamp_child_dispatch_lineage(Bad(), None, dispatched_by="x")
