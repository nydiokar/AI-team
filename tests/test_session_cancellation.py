"""Gateway stateless cancellation reaches the exact native turn before returning."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime

import pytest

from config import config
from src.core.interfaces import ExecutionResult, Task, TaskPriority, TaskStatus, TaskType
from src.orchestrator import TaskOrchestrator


def _task(task_id: str, *, timeout_sec: int) -> Task:
    return Task(
        id=task_id, type=TaskType.ANALYZE, priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING, created=datetime.now().isoformat(),
        title="Stateless work", target_files=[], prompt="Do work",
        success_criteria=[], context="",
        metadata={"backend": "codex", "timeout_sec": timeout_sec},
    )


class _BlockingBackend:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.ended = threading.Event()
        self.turn_uuid = ""
        self.cancelled: list[tuple[str, str]] = []

    def run_oneoff(self, cwd, message, *, turn, **kwargs) -> ExecutionResult:
        self.turn_uuid = turn.turn_uuid
        self.started.set()
        try:
            assert self.release.wait(4), "native turn was never cancelled"
            return ExecutionResult(False, "", errors=["cancelled"], error_class="cancelled")
        finally:
            self.ended.set()

    def cancel(self, session, turn_uuid: str) -> bool:
        self.cancelled.append((session.session_id, turn_uuid))
        assert turn_uuid == self.turn_uuid
        self.release.set()
        return True


@pytest.mark.asyncio
async def test_oneoff_timeout_cancels_exact_turn_and_waits_for_native_exit(monkeypatch):
    monkeypatch.setattr(config.system, "local_execution_enabled", True)
    orchestrator = TaskOrchestrator()
    backend = _BlockingBackend()
    orchestrator._backends["codex"] = backend
    task = _task("timeout", timeout_sec=1)
    try:
        result = await asyncio.wait_for(orchestrator.process_task(task), timeout=3)
        assert not result.success
        assert any("timed out" in e.lower() or "timeout" in e.lower() for e in result.errors)
        assert backend.cancelled == [("oneoff-timeout-1", "timeout-1")]
        assert backend.ended.is_set()
    finally:
        backend.release.set()


@pytest.mark.asyncio
async def test_cancel_task_consumes_event_before_cancelling_wrapper(monkeypatch):
    monkeypatch.setattr(config.system, "local_execution_enabled", True)
    orchestrator = TaskOrchestrator()
    backend = _BlockingBackend()
    orchestrator._backends["codex"] = backend
    task = _task("operator-cancel", timeout_sec=0)
    orchestrator.active_tasks[task.id] = task
    running = asyncio.create_task(orchestrator.process_task(task))
    try:
        assert await asyncio.to_thread(backend.started.wait, 2)
        assert orchestrator.cancel_task(task.id) is True
        result = await asyncio.wait_for(asyncio.shield(running), timeout=2)
        assert not result.success and result.errors == ["cancelled"]
        assert backend.cancelled == [("oneoff-operator-cancel-1", "operator-cancel-1")]
        assert backend.ended.is_set()
    finally:
        backend.release.set()
        if not running.done():
            await running
