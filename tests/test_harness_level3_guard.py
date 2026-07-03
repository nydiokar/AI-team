"""
Tests for the task-harness Level-3 auto-pickup guard
(`TaskOrchestrator._harness_level3_allows_autopickup`, spec
docs/Task_harness_workflow.md §14).

Boundary under test:
- guard OFF (flag unset) => always allow, regardless of level (legacy behavior)
- guard ON:
    - `harness_level` absent           => allow (byte-identical legacy)
    - `harness_level: 2` (or ≤ 2)      => allow (auto-enqueue)
    - `harness_level: 3` no approval    => BLOCK
    - `harness_level: 3` approved: true => allow
    - unparseable level                 => allow (don't invent a block)

Pure function test: the guard is a @staticmethod, so no orchestrator instance,
no DB, no backend, no paid CLI.
"""
from __future__ import annotations

import asyncio

import pytest

from src.core.interfaces import Task, TaskType, TaskPriority, TaskStatus
from src.orchestrator import TaskOrchestrator, HarnessAdmissionBlocked

GUARD = TaskOrchestrator._harness_level3_allows_autopickup


def _task(metadata: dict | None = None) -> Task:
    return Task(
        id="task_x",
        type=TaskType.FIX,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING,
        created="2026-07-03T00:00:00",
        title="t",
        target_files=[],
        prompt="do the thing",
        success_criteria=[],
        context="",
        metadata=metadata or {},
    )


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setenv("HARNESS_LEVEL3_GUARD", "1")


@pytest.fixture
def guard_off(monkeypatch):
    monkeypatch.delenv("HARNESS_LEVEL3_GUARD", raising=False)


# --- guard OFF: legacy behavior, level ignored -----------------------------

def test_off_level3_still_allowed(guard_off):
    assert GUARD(_task({"harness_level": 3})) is True


def test_off_level3_unapproved_still_allowed(guard_off):
    assert GUARD(_task({"harness_level": 3, "approved": False})) is True


# --- guard ON --------------------------------------------------------------

def test_on_field_absent_allowed(guard_on):
    # Byte-identical legacy behavior when the field is absent.
    assert GUARD(_task({})) is True


@pytest.mark.parametrize("level", [0, 1, 2, "2"])
def test_on_level_le_2_allowed(guard_on, level):
    assert GUARD(_task({"harness_level": level})) is True


@pytest.mark.parametrize("level", [3, "3"])
def test_on_level3_unapproved_blocked(guard_on, level):
    assert GUARD(_task({"harness_level": level})) is False


def test_on_level3_approved_true_allowed(guard_on):
    assert GUARD(_task({"harness_level": 3, "approved": True})) is True


def test_on_level3_approved_string_allowed(guard_on):
    assert GUARD(_task({"harness_level": 3, "approved": "true"})) is True


def test_on_level3_approved_false_blocked(guard_on):
    assert GUARD(_task({"harness_level": 3, "approved": False})) is False


def test_on_unparseable_level_allowed(guard_on):
    # A garbage level must not invent a block.
    assert GUARD(_task({"harness_level": "high"})) is True


@pytest.mark.parametrize("flag", ["", "0", "false", "off", "no"])
def test_falsey_flag_values_leave_guard_off(monkeypatch, flag):
    monkeypatch.setenv("HARNESS_LEVEL3_GUARD", flag)
    assert GUARD(_task({"harness_level": 3})) is True


# ---------------------------------------------------------------------------
# Hot-path admission gate in `_enqueue_task` — the choke point every ingestion
# lane (submit_instruction/Telegram/Web + .task.md) passes through. This is the
# follow-up (build-review B1): the gate must fire on the MAIN door, not just the
# .task.md lane. A blocked task must raise HarnessAdmissionBlocked and leave NO
# side effect (not queued, not in active_tasks); an allowed task enqueues and
# returns its id, byte-identical to before.
# ---------------------------------------------------------------------------

def _enqueue(orch: TaskOrchestrator, task: Task):
    return asyncio.run(orch._enqueue_task(task))


@pytest.fixture
def orch():
    # A bare orchestrator is enough: _enqueue_task only needs task_queue,
    # active_tasks, and the event/telemetry emitters, all set in __init__.
    return TaskOrchestrator()


def test_enqueue_blocks_level3_unapproved_when_flag_on(orch, guard_on):
    task = _task({"harness_level": 3})
    with pytest.raises(HarnessAdmissionBlocked) as exc:
        _enqueue(orch, task)
    assert exc.value.task_id == task.id
    assert exc.value.reason == "harness_level3_needs_approval"
    # No side effect leaked past the gate.
    assert task.id not in orch.active_tasks
    assert orch.task_queue.qsize() == 0


def test_enqueue_allows_level3_approved_when_flag_on(orch, guard_on):
    task = _task({"harness_level": 3, "approved": True})
    returned = _enqueue(orch, task)
    assert returned == task.id
    assert task.id in orch.active_tasks
    assert orch.task_queue.qsize() == 1


@pytest.mark.parametrize("meta", [{}, {"harness_level": 2}, {"harness_level": 1}])
def test_enqueue_allows_non_level3_when_flag_on(orch, guard_on, meta):
    task = _task(meta)
    assert _enqueue(orch, task) == task.id
    assert task.id in orch.active_tasks


def test_enqueue_flag_off_allows_level3_unapproved(orch, guard_off):
    # Byte-identical legacy behavior: with the flag off, a Level-3 task enqueues.
    task = _task({"harness_level": 3})
    assert _enqueue(orch, task) == task.id
    assert task.id in orch.active_tasks
    assert orch.task_queue.qsize() == 1
