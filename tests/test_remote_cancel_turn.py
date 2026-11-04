"""Session cancellation is always a durable, turn-scoped carrier control."""
from __future__ import annotations

import asyncio
import json

import pytest

from src.worker.agent import _execute_task
from tests.test_turn_queue_4b import _pass, _wire
from tests.test_turn_queue_producer1 import _no_cli_spawn, _setup, _submit


@pytest.mark.parametrize("backend_name", ["claude", "codex", "opencode-server"])
def test_remote_session_cancel_targets_claimed_attempt_not_gateway_backend(
        tmp_path, monkeypatch, backend_name):
    db, orchestrator = _setup(tmp_path, monkeypatch)
    _wire(orchestrator)
    # Backend-neutral cancellation reads the committed attempt, never a local pool.
    turn_id = str(_submit(orchestrator, operation_id="cancel-target"))
    _pass(db, orchestrator)
    token = db.claim_turn(turn_id, "worker-a", "worker_daemon", "inc-1")
    db.start_turn(turn_id, token, incarnation_id="inc-1")
    db._conn().execute("UPDATE mesh_tasks SET backend = ? WHERE id = ?", (backend_name, turn_id))
    assert orchestrator.cancel_task(turn_id) is True
    controls = [dict(row) for row in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE action = 'cancel_managed'"
    )]
    assert len(controls) == 1
    control = controls[0]
    assert control["machine_id"] == "worker-a"
    assert json.loads(control["payload"])["target_task_id"] == turn_id
    assert db.get_task(turn_id)["status"] == "running"
    assert db.get_task(turn_id)["cancel_requested_at"] is not None


def test_historical_session_wide_cancel_fails_without_touching_backend():
    calls: list[str] = []

    class Backend:
        def cancel(self, session, turn_uuid):
            calls.append(turn_uuid)
            return True

    out = asyncio.run(_execute_task({
        "id": "old-cancel", "action": "cancel_turn", "backend": "claude",
        "payload": {"session": {"session_id": "s", "backend": "claude"}},
    }, {"claude": Backend()}))
    assert out["success"] is False and out["error_class"] == "invalid_cancellation_target"
    assert calls == []
