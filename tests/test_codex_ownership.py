"""Gateway-owned Codex queue and durable-claim invariants."""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from src.backends.codex_ownership import CodexOwnership
from src.core.interfaces import Task, TaskPriority, TaskStatus, TaskType
from src.core.session_task_queue import SessionTaskQueue


def _task(name: str, session_id: str) -> Task:
    return Task(
        name,
        TaskType.ANALYZE,
        TaskPriority.MEDIUM,
        TaskStatus.PENDING,
        "",
        name,
        [],
        name,
        [],
        "",
        {"session_id": session_id},
    )


@pytest.mark.asyncio
async def test_same_session_fifo_leaves_unrelated_session_runnable() -> None:
    queue = SessionTaskQueue(4, lambda item: item.metadata["session_id"])
    first, second, third, unrelated = [
        _task(name, session_id)
        for name, session_id in (
            ("first", "a"),
            ("second", "a"),
            ("third", "a"),
            ("other", "b"),
        )
    ]
    for item in (first, second, third, unrelated):
        queue.put_nowait(item)
    assert await queue.get() is first
    assert await queue.get() is unrelated
    queue.task_done(unrelated)
    waiting = asyncio.create_task(queue.get())
    await asyncio.sleep(0)
    assert not waiting.done()
    queue.task_done(first)
    assert await asyncio.wait_for(waiting, 1) is second
    queue.task_done(second)
    assert await queue.get() is third
    queue.task_done(third)
    await queue.join()


def test_cross_process_claim_and_crash_fail_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    script = (
        "from src.backends.codex_ownership import CodexOwnership; "
        "o=CodexOwnership(); o.acquire('session','exact-thread'); print('owned')"
    )
    child = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "owned"
    with pytest.raises(RuntimeError, match="codex_thread_busy"):
        CodexOwnership().acquire("session", "")
    with pytest.raises(RuntimeError, match="codex_thread_busy"):
        CodexOwnership().acquire("alias", "exact-thread")
    other = CodexOwnership()
    assert other.acquire("other", "different-thread") == "different-thread"
    other.release()


def test_cross_process_distinct_sessions_share_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    workspace = str(tmp_path / "workspace")
    owner = CodexOwnership()
    owner.acquire("first-session", "first-thread", workspace)
    script = (
        "from src.backends.codex_ownership import CodexOwnership; "
        f"CodexOwnership().acquire('second-session', 'second-thread', {workspace!r})"
    )
    try:
        competing = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
        )
        assert competing.returncode == 0, competing.stderr
    finally:
        owner.release()


def test_live_cross_process_thread_alias_is_excluded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    owner = CodexOwnership()
    owner.acquire("session", "exact-thread")
    script = (
        "from src.backends.codex_ownership import CodexOwnership; "
        "CodexOwnership().acquire('alias','exact-thread')"
    )
    try:
        competing = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
        )
        assert competing.returncode != 0
        assert "codex_thread_busy" in competing.stderr
    finally:
        owner.release()
    assert CodexOwnership().acquire("session", "") == "exact-thread"
