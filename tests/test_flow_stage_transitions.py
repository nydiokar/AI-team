"""
A22 — Authoritative stage transitions (shadow) tests.

current_stage becomes a real, written reflection of where a flow is in the §1
loop, driven from the orchestrator loop surface — while NOTHING in execution
reads it. The §11 FLOW_STAGES vocabulary is written in order at each harness
transition (intent → objective_lock → execution → impl_review → closure).

Plus the non-negotiable safety property: a forced stage-write exception is
swallowed — it can NEVER raise into task execution.

Everything here calls the transition helpers on a real bound orchestrator-ish
object; nothing reads current_stage to drive behavior.
"""

import types

import pytest

from src.control.db import MeshDB, FLOW_STAGES
from src.orchestrator import TaskOrchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _bind(db):
    """A minimal object with just enough for the flow-stage helpers to run as
    real *bound* methods (so the metadata key and `_record_flow_stage` all
    resolve through the class)."""
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    return orch


def _task(task_id="task-1", metadata=None):
    return types.SimpleNamespace(id=task_id, metadata=metadata)


# [A36] A birth marker: under the flag-ON admission policy an ordinary turn no
# longer mints a Case — only a dispatched/managed task births a flow_run. These
# stage-machine tests exercise the birth+transition path, so the driving task is
# flagged an explicit managed-Case root.
def _managed_task(task_id="task-1"):
    return _task(task_id, {TaskOrchestrator._MANAGED_CASE_META_KEY: True})


# ---------------------------------------------------------------------------
# (c) ON: initial stage is `intent`, the flow_run_id is stashed, and the
#     transitions advance the stage in §11 order.
# ---------------------------------------------------------------------------

def test_on_start_writes_intent_and_stashes(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    orch = _bind(db)
    task = _managed_task("task-on")
    fid = orch._record_flow_run_start(task)

    assert db.get_flow_run(fid)["current_stage"] == "intent"
    # ON ⇒ the flow_run_id is stashed for later transition points.
    assert task.metadata["__flow_run_id"] == fid


def test_on_stages_advance_in_order(tmp_path, monkeypatch):
    """Drive the full transition sequence the orchestrator loop performs and
    assert current_stage advances through the FLOW_STAGES vocabulary in order."""
    db = _fresh_db(tmp_path)
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    orch = _bind(db)
    task = _managed_task("task-seq")

    observed = []

    # 1) start → intent
    fid = orch._record_flow_run_start(task)
    observed.append(db.get_flow_run(fid)["current_stage"])

    # 2) admitted/queued → objective_lock  (the enqueue-path write, ON branch)
    orch._record_flow_stage(task.metadata["__flow_run_id"], "objective_lock")
    observed.append(db.get_flow_run(fid)["current_stage"])

    # 3) execution begins
    orch._flow_stage_transition(task, "execution")
    observed.append(db.get_flow_run(fid)["current_stage"])

    # 4) result under review
    orch._flow_stage_transition(task, "impl_review")
    observed.append(db.get_flow_run(fid)["current_stage"])

    # 5) closure
    orch._flow_stage_transition(task, "closure")
    observed.append(db.get_flow_run(fid)["current_stage"])

    assert observed == ["intent", "objective_lock", "execution", "impl_review", "closure"]

    # Every observed stage is a member of the canonical vocabulary, and the
    # sequence is strictly increasing in FLOW_STAGES order (monotonic advance).
    idx = [FLOW_STAGES.index(s) for s in observed]
    assert idx == sorted(idx)
    assert idx == [FLOW_STAGES.index(s) for s in
                   ("intent", "objective_lock", "execution", "impl_review", "closure")]


def test_on_transition_stamps_updated_at(tmp_path, monkeypatch):
    """A stage transition stamps updated_at (via update_flow_stage)."""
    db = _fresh_db(tmp_path)
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    orch = _bind(db)
    task = _managed_task("task-ts")
    fid = orch._record_flow_run_start(task)
    assert db.get_flow_run(fid)["updated_at"] is None  # create leaves it NULL

    orch._flow_stage_transition(task, "execution")
    assert db.get_flow_run(fid)["updated_at"] is not None


# ---------------------------------------------------------------------------
# (d) Failure isolation: a forced stage-write exception NEVER raises into task
#     execution — the helper swallows it and returns.
# ---------------------------------------------------------------------------

def test_forced_write_exception_does_not_propagate(monkeypatch):

    class _BoomDB:
        def update_flow_stage(self, *a, **k):
            raise RuntimeError("stage write boom")

    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: _BoomDB())

    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    task = types.SimpleNamespace(id="task-boom", metadata={"__flow_run_id": "fr-1"})

    # MUST NOT raise — a broken stage write can never break task execution.
    orch._flow_stage_transition(task, "execution")


def test_forced_start_exception_returns_none(monkeypatch):

    class _BoomDB:
        def create_flow_run(self, *a, **k):
            raise RuntimeError("create boom")

    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: _BoomDB())

    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    task = _task("task-boom2")

    # Swallowed → returns None, task path unaffected, nothing stashed.
    assert orch._record_flow_run_start(task) is None
    assert task.metadata is None or "__flow_run_id" not in (task.metadata or {})
