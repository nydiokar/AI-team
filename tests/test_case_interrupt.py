"""[A53] Kill path — ``TaskOrchestrator.interrupt_case``.

Drives the genuine orchestrator method against a real MeshDB with a duck-typed
``self`` (mirrors test_case_continuation): cancels the Case's in-flight worker
tasks, records ONE flow.interrupted, marks the Case blocked (resumable, NOT
closed), escalates once, and is idempotent. No paid CLI, no gateway.
"""
import asyncio

import pytest

from src.control import db as db_mod
from src.control.db import MeshDB, _event_payload
from src.orchestrator import TaskOrchestrator


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = MeshDB(str(tmp_path / "mesh.db"))
    # interrupt_case resolves the DB via get_db()'s singleton — point it here.
    monkeypatch.setattr(db_mod, "_db_instance", d, raising=False)
    return d


class _FakeNotifier:
    def __init__(self):
        self.errors = []

    async def notify_error(self, message, **kw):
        self.errors.append(message)


class _KillOrch:
    """Duck-typed self carrying exactly what interrupt_case touches."""

    def __init__(self, *, cancel_returns=True):
        self.notifier = _FakeNotifier()
        self.cancelled = []
        self.emitted = []
        self._cancel_returns = cancel_returns

    def cancel_task(self, tid):
        self.cancelled.append(tid)
        return self._cancel_returns

    def _emit_event(self, name, _a, payload):
        self.emitted.append((name, payload))


def _interrupt(orch, case_id, **kw):
    return asyncio.run(TaskOrchestrator.interrupt_case(orch, case_id, **kw))


def _open_with_workers(db, *task_ids):
    case_id = db.open_case("obj", "mgr-sess", role="manager")
    for t in task_ids:
        # PRODUCTION shape: a dispatched worker task links entity_type='task',
        # role='task', created_by='manager' (see orchestrator._record_flow_link at
        # the join seam). NOT role='worker' — that is the SESSION link's role.
        db.create_flow_link(case_id, "task", t, "task", created_by="manager")
    return case_id


def _interrupted_events(db, case_id):
    return [e for e in db.list_flow_events(case_id) if e["event_type"] == "flow.interrupted"]


# --------------------------------------------------------------------------- #
# Happy path                                                                   #
# --------------------------------------------------------------------------- #

def test_interrupt_cancels_blocks_records_escalates(db):
    case_id = _open_with_workers(db, "t1", "t2")
    orch = _KillOrch()

    res = _interrupt(orch, case_id)

    assert res["ok"] is True
    assert res["already"] is False
    assert set(res["cancelled_tasks"]) == {"t1", "t2"}
    assert set(orch.cancelled) == {"t1", "t2"}
    # blocked (resumable), NOT closed
    assert (db.get_flow_run(case_id) or {}).get("status") == "blocked"
    # exactly one flow.interrupted with the reason + cancelled payload
    evs = _interrupted_events(db, case_id)
    assert len(evs) == 1
    payload = _event_payload(evs[0]) or {}
    assert payload.get("reason") == "operator_kill"
    assert set(payload.get("cancelled_tasks") or []) == {"t1", "t2"}
    # escalated exactly once
    assert len(orch.notifier.errors) == 1


def test_interrupt_idempotent(db):
    case_id = _open_with_workers(db, "t1")
    orch = _KillOrch()

    first = _interrupt(orch, case_id)
    second = _interrupt(orch, case_id)

    assert first["already"] is False and second["already"] is True
    # no duplicate event, no second escalation
    assert len(_interrupted_events(db, case_id)) == 1
    assert len(orch.notifier.errors) == 1
    # still blocked
    assert (db.get_flow_run(case_id) or {}).get("status") == "blocked"


# --------------------------------------------------------------------------- #
# Refusals                                                                     #
# --------------------------------------------------------------------------- #

def test_interrupt_unknown_case(db):
    res = _interrupt(_KillOrch(), "no-such-case")
    assert res == {"ok": False, "reason": "case_not_found"}


def test_interrupt_refuses_terminal_case(db):
    case_id = _open_with_workers(db, "t1")
    db.update_flow_run(case_id, status="closed")
    res = _interrupt(_KillOrch(), case_id)
    assert res["ok"] is False and res["reason"] == "case_closed"
    # a closed Case is never demoted to blocked
    assert (db.get_flow_run(case_id) or {}).get("status") == "closed"


def test_interrupt_custom_reason(db):
    case_id = _open_with_workers(db, "t1")
    orch = _KillOrch()
    _interrupt(orch, case_id, reason="runaway_cost")
    payload = _event_payload(_interrupted_events(db, case_id)[0]) or {}
    assert payload.get("reason") == "runaway_cost"


def test_interrupt_only_cancels_manager_dispatched_workers(db):
    """The Manager's OWN-turn attach (created_by='system') and the root_task must
    NOT be cancelled — only dispatched worker tasks (created_by='manager')."""
    case_id = db.open_case("obj", "mgr-sess", role="manager")
    db.create_flow_link(case_id, "task", "worker1", "task", created_by="manager")
    db.create_flow_link(case_id, "task", "mgr_own_turn", "task", created_by="system")
    db.create_flow_link(case_id, "task", "root", "root_task", created_by="system")
    orch = _KillOrch()

    res = _interrupt(orch, case_id)

    assert res["cancelled_tasks"] == ["worker1"]
    assert orch.cancelled == ["worker1"]


def test_second_kill_different_reason_is_still_idempotent(db):
    """Idempotency spans reasons: a kill with reason B after reason A writes no
    second event and does not re-escalate."""
    case_id = _open_with_workers(db, "t1")
    orch = _KillOrch()
    _interrupt(orch, case_id, reason="operator_kill")
    second = _interrupt(orch, case_id, reason="runaway_cost")
    assert second["already"] is True
    assert len(_interrupted_events(db, case_id)) == 1
    assert len(orch.notifier.errors) == 1
