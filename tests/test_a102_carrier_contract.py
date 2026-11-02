"""A102: carrier-owned control reaches the single natural backend interface."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.control.turn_queue import LegacyExecutionRetiredError, ManagedTurnOwnership
from src.core.interfaces import ExecutionResult
from src.core.turn_liveness import ProgressClock, TurnControl, TurnLimits
from src.orchestrator import TaskOrchestrator
from src.worker.agent import WorkerAgent, _execute_task


def _row(action: str, native: str = "native") -> dict:
    return {
        "id": "task", "session_id": "session", "backend": "claude", "action": action,
        "payload": {"prompt": "prompt", "session": {
            "session_id": "session", "backend": "claude", "repo_path": "/repo",
            "backend_session_id": native,
        }},
    }


def _ownership() -> ManagedTurnOwnership:
    return ManagedTurnOwnership(
        task_id="task", session_id="session", node_id="node",
        claim_token="token", turn_uuid="uuid",
    )


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TurnControl, str]] = []

    def create_session(self, session, *, turn, **kwargs) -> ExecutionResult:
        self.calls.append(("create", turn, session.last_user_message))
        return ExecutionResult(True, "created")

    def resume_session(self, session, prompt, *, turn, **kwargs) -> ExecutionResult:
        self.calls.append(("resume", turn, prompt))
        return ExecutionResult(True, "resumed")

    def compact_session(self, session, *, turn, **kwargs) -> ExecutionResult:
        self.calls.append(("compact", turn, ""))
        return ExecutionResult(True, "compacted")


@pytest.mark.parametrize("action,native,operation", [
    ("create_session", "", "create"), ("resume_session", "", "create"),
    ("resume_session", "native", "resume"), ("compact_session", "native", "compact"),
])
def test_claimed_turn_uses_natural_operation_and_carrier_control(action, native, operation):
    backend = _Backend()
    ownership = _ownership()
    on_process = lambda identity: None
    out = asyncio.run(_execute_task(
        _row(action, native), {"claude": backend},
        ownership=ownership, on_process=on_process,
    ))
    assert out["success"]
    kind, turn, prompt = backend.calls[0]
    assert kind == operation and turn.turn_uuid == ownership.turn_uuid
    assert turn.ownership is ownership and turn.on_process is on_process
    assert prompt == ("" if operation == "compact" else "prompt")


@pytest.mark.parametrize("action", ["create_session", "resume_session", "compact_session"])
def test_session_turn_without_ownership_is_structurally_refused(action):
    backend = _Backend()
    out = asyncio.run(_execute_task(_row(action), {"claude": backend}))
    assert out["success"] is False and out["error_class"] == "managed_unsupported"
    assert backend.calls == []


def test_gateway_session_execution_cannot_bypass_queue():
    orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
    task = SimpleNamespace(metadata={"session_id": "session"})
    with pytest.raises(LegacyExecutionRetiredError):
        asyncio.run(orchestrator._process_task(task))


@pytest.mark.parametrize("target", [None, "", "x" * 257])
def test_old_cancel_without_bounded_turn_identity_fails_visibly(target):
    row = _row("cancel_turn")
    row["payload"]["turn_uuid"] = target
    backend = _Backend()
    out = asyncio.run(_execute_task(row, {"claude": backend}))
    assert out["success"] is False
    assert out["error_class"] == "invalid_cancellation_target"
    assert backend.calls == []


def test_turn_scoped_cancel_is_delivered_without_a_work_slot():
    calls: list[tuple[str, str]] = []
    backend = SimpleNamespace(cancel=lambda session, turn_uuid: calls.append(
        (session.session_id, turn_uuid)
    ) or True)
    row = _row("cancel_turn")
    row["payload"]["turn_uuid"] = "target"
    out = asyncio.run(_execute_task(row, {"claude": backend}))
    assert out["success"] is True and calls == [("session", "target")]


def test_worker_advertises_only_installed_registered_adapters():
    worker = WorkerAgent.__new__(WorkerAgent)
    worker.cfg = SimpleNamespace(backends=["claude", "codex", "opencode", "opencode-server"])
    worker._backends = {"claude": _Backend(), "opencode": _Backend(), "opencode-server": _Backend()}
    assert worker._managed_backends() == ["claude", "opencode-server"]


def test_carrier_control_survives_31_minutes_of_native_progress(monkeypatch):
    now = [0.0]
    control = TurnControl(
        turn_uuid="uuid", limits=TurnLimits(stall_sec=10, hard_cap_sec=7200),
        ownership=_ownership(), progress=ProgressClock(now=lambda: now[0]),
    )
    monkeypatch.setattr("src.core.turn_liveness.turn_control", lambda *a, **k: control)

    class Progressing(_Backend):
        def resume_session(self, session, prompt, *, turn, **kwargs):
            assert turn is control
            for _ in range(372):
                now[0] += 5
                assert turn.expired() == ""
                turn.touch()
            assert now[0] == 1860
            now[0] += 11
            assert turn.expired() == "stalled"
            return ExecutionResult(True, "completed after progressing for 31 minutes")

    out = asyncio.run(_execute_task(
        _row("resume_session"), {"claude": Progressing()}, ownership=control.ownership,
    ))
    assert out["success"] is True
