"""A103 — carrier nudge at activation, not admission.

Verifies that the managed-turn carrier nudge fires when the TurnScheduler
activates a queued row (queued → pending), NOT at the earlier admission step
where the row is still status='queued' and therefore not yet claimable by any
worker.

Tests:
  A103-01  admission does NOT fire _nudge_worker (row is queued, not claimable)
  A103-02  scheduler activation fires _nudge_worker exactly once with the row's machine_id
  A103-03  nudge failure on activation does NOT break the activation outcome
  A103-04  an A104 inbox-wake admission does NOT nudge; its activation nudges once
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

import src.control.node_inspector as node_inspector_mod
from src.control import turn_admission as ta
from src.control import turn_scheduler as ts
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus

# reuse setup helpers from producer tests (session + orchestrator creation)
from tests.test_turn_queue_producer1 import (  # noqa: F401  (autouse fixtures)
    _flags,
    _no_cli_spawn,
    _register_carrier,
    _sess,
    _setup,
    _submit,
)
from tests.inbox_seed import seed_finished_child
from tests.test_agent_inbox_delivery import (  # noqa: F401  (autouse fixture)
    _case,
    _env,
    _fresh_allowance,
    _tick,
    _wakes,
)
from tests.test_turn_queue_4b import _pass

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Scheduler helpers (mirrors test_turn_queue_scheduler.py)
# ---------------------------------------------------------------------------

class _Prep:
    """Minimal fake prepare: returns a claimable turn for carrier 'worker-a'."""

    async def __call__(self, head, row):
        return ts.PreparedTurn(
            action="resume_session",
            payload={"prompt": row.get("prompt", ""), "task_id": row["id"]},
            machine_id="worker-a",
        )


def _run_scheduler_pass(db: MeshDB) -> ts.SchedulerPassResult:
    return asyncio.run(
        ts.run_scheduler_pass(db, _Prep(), allowance=ta.SharedWaitingAllowance())
    )


# A103-01 ------------------------------------------------------------------- #

def test_A103_01_admission_does_not_nudge_carrier(tmp_path, monkeypatch):
    """Admitting a managed turn must NOT fire _nudge_worker.

    The row is still status='queued' at admission — no worker can claim it yet,
    so a nudge would be wasted and the woken worker re-enters its backoff.
    """
    db, o = _setup(tmp_path, monkeypatch)

    nudge_mock = AsyncMock()
    monkeypatch.setattr(node_inspector_mod, "_nudge_worker", nudge_mock)

    _submit(o, operation_id="nudge-a103-01")

    nudge_mock.assert_not_called()


# A103-02 ------------------------------------------------------------------- #

def test_A103_02_activation_nudges_carrier_machine_id(tmp_path, monkeypatch):
    """Scheduler activation (queued → pending) fires _nudge_worker exactly once
    and targets the row's carrier machine_id.
    """
    db, o = _setup(tmp_path, monkeypatch)

    nudge_mock = AsyncMock()
    monkeypatch.setattr(node_inspector_mod, "_nudge_worker", nudge_mock)

    _submit(o, operation_id="nudge-a103-02")

    # Row is queued — nudge must NOT have fired yet.
    nudge_mock.assert_not_called()

    # Run the scheduler pass: queued → pending.
    result = _run_scheduler_pass(db)
    assert result.activated == 1, (
        f"expected 1 activation, got {result.activated}"
    )

    # Now nudge must have fired exactly once targeting the carrier.
    nudge_mock.assert_called_once()
    call_args = nudge_mock.call_args
    assert call_args[0][0] == "worker-a", (
        f"nudge must target the session's carrier node_id, got: {call_args[0][0]!r}"
    )
    assert call_args[0][1] is db, "nudge must receive the db handle"


# A103-03 ------------------------------------------------------------------- #

def test_A103_03_nudge_failure_does_not_break_activation(tmp_path, monkeypatch):
    """A nudge-path exception on activation must not affect the activation outcome.

    The row becomes 'pending' (claimable) regardless of whether the nudge HTTP
    call succeeds or raises.
    """
    db, o = _setup(tmp_path, monkeypatch)

    # Non-async replacement forces asyncio.create_task to raise TypeError,
    # exercising the except-swallow branch in the activation path.
    monkeypatch.setattr(node_inspector_mod, "_nudge_worker", lambda node_id, db: "not-a-coro")

    _submit(o, operation_id="nudge-a103-03")

    result = _run_scheduler_pass(db)
    assert result.activated == 1, (
        f"activation must succeed even when the nudge path raises, got {result.activated}"
    )

    # The row must now be status='pending' (claimable).
    rows = [
        dict(r) for r in db._conn().execute(
            "SELECT status FROM mesh_tasks WHERE queue_protocol = 1"
        ).fetchall()
    ]
    assert rows == [{"status": "pending"}], (
        f"row must be pending after activation, got {rows}"
    )


# A103-04 ------------------------------------------------------------------- #

def test_A103_04_inbox_wake_admission_does_not_nudge_activation_does(tmp_path, monkeypatch):
    """The A104 inbox-wake branch of managed producer admission must not nudge
    either (8c5596c7 re-added it there); the activation nudge still fires once.
    """
    db, o = _env(tmp_path, monkeypatch)
    nudge_mock = AsyncMock()
    monkeypatch.setattr(node_inspector_mod, "_nudge_worker", nudge_mock)
    seed_finished_child(db, _case(db), "task_w1", requester="sess-1")

    _tick(o)
    (wake,) = _wakes(db)
    assert wake["status"] == "queued"
    nudge_mock.assert_not_called()

    assert _pass(db, o).activated == 1
    nudge_mock.assert_called_once()
    assert nudge_mock.call_args[0][0] == "worker-a"
