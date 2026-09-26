"""
Tests for remote session turn cancellation.

Regression: when GATEWAY_LOCAL_EXECUTION_ENABLED=false (the Docker controller
split), all sessions are pinned to a remote worker node.  cancel_task() used to
call backend.cancel(session) on the gateway's LOCAL SDK pool, which is empty for
remote sessions — so the Claude CLI process on the worker was never interrupted.

Fix: cancel_task() now also enqueues a cancel_turn control task to the owning
worker node when session.machine_id is set.
"""
from pathlib import Path
import sys
import uuid
from datetime import datetime

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.orchestrator import TaskOrchestrator
from src.core.interfaces import Task, TaskType, TaskPriority, TaskStatus, ExecutionResult
from src.services.session_store import SessionStore
import src.services.session_store as session_store_module


def _make_task(session_id: str, task_id: str | None = None) -> Task:
    return Task(
        id=task_id or f"task_{uuid.uuid4().hex[:8]}",
        type=TaskType.ANALYZE,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING,
        created=datetime.now().isoformat(),
        title="Test remote cancel task",
        target_files=[],
        prompt="Do work",
        success_criteria=[],
        context="",
        metadata={"session_id": session_id, "backend": "claude"},
    )


# ---------------------------------------------------------------------------
# Test 1 — LOCAL session: backend.cancel() is called, NO remote enqueue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_session_cancel_calls_backend_cancel_only(tmp_path, monkeypatch):
    """For a session with no machine_id (local), cancel_task calls backend.cancel
    directly and does NOT enqueue a remote cancel_turn task."""
    sessions_dir = tmp_path / "state" / "sessions"
    bindings_file = tmp_path / "state" / "telegram" / "active_bindings.json"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    bindings_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", sessions_dir, raising=False)
    monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", bindings_file, raising=False)

    orch = TaskOrchestrator()
    store = SessionStore()
    session = store.create("claude", str(tmp_path))
    # Explicitly clear machine_id → local (in-process) session
    session.machine_id = ""
    store.save(session)
    assert not getattr(session, "machine_id", "")

    task = _make_task(session.session_id)
    import asyncio
    orch.active_tasks[task.id] = task
    orch._task_cancel_events[task.id] = asyncio.Event()

    backend_cancel_calls = []
    enqueue_calls = []

    class _FakeBackend:
        def cancel(self, s):
            backend_cancel_calls.append(s.session_id)

    orch._backends["claude"] = _FakeBackend()

    # Patch _enqueue_remote_cancel_turn to track whether it's called
    original_enqueue = orch._enqueue_remote_cancel_turn
    def _fake_enqueue(s):
        enqueue_calls.append(getattr(s, "session_id", ""))
    orch._enqueue_remote_cancel_turn = _fake_enqueue

    result = orch.cancel_task(task.id)

    assert result is True
    assert backend_cancel_calls == [session.session_id], "backend.cancel should be called for local session"
    assert enqueue_calls == [], "No remote enqueue for a session without machine_id"


# ---------------------------------------------------------------------------
# Test 2 — REMOTE session: both backend.cancel() (no-op locally) AND
#          _enqueue_remote_cancel_turn are called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remote_session_cancel_enqueues_cancel_turn(tmp_path, monkeypatch):
    """For a session pinned to a remote worker (machine_id set), cancel_task must
    ALSO enqueue a cancel_turn control task so the worker can interrupt the Claude
    CLI process.  This is the regression case: previously only backend.cancel()
    was called, which is a no-op for remote sessions."""
    sessions_dir = tmp_path / "state" / "sessions"
    bindings_file = tmp_path / "state" / "telegram" / "active_bindings.json"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    bindings_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", sessions_dir, raising=False)
    monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", bindings_file, raising=False)

    orch = TaskOrchestrator()
    store = SessionStore()
    session = store.create("claude", str(tmp_path))
    # Simulate a mesh-pinned session
    session.machine_id = "kanebra-worker"
    store.save(session)

    task = _make_task(session.session_id)
    import asyncio
    orch.active_tasks[task.id] = task
    orch._task_cancel_events[task.id] = asyncio.Event()

    backend_cancel_calls = []
    enqueue_calls = []

    class _FakeBackend:
        def cancel(self, s):
            # This is the gateway-side call — for remote sessions the SDK pool
            # won't have the session, so this is effectively a no-op in production.
            backend_cancel_calls.append(s.session_id)

    orch._backends["claude"] = _FakeBackend()

    def _fake_enqueue(s):
        enqueue_calls.append(getattr(s, "session_id", ""))
    orch._enqueue_remote_cancel_turn = _fake_enqueue

    result = orch.cancel_task(task.id)

    assert result is True
    assert backend_cancel_calls == [session.session_id], "backend.cancel still called (local no-op)"
    assert enqueue_calls == [session.session_id], (
        "cancel_turn must be enqueued to the remote worker for mesh-pinned sessions"
    )


# ---------------------------------------------------------------------------
# Test 3 — CODEX remote session: cancel_codex path (NOT cancel_turn)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remote_codex_session_does_not_enqueue_cancel_turn(tmp_path, monkeypatch):
    """Codex sessions have their own cancel_codex mechanism via _dispatch_to_node.
    cancel_task must NOT also enqueue cancel_turn for codex — that would be double-
    cancel with two different protocols."""
    sessions_dir = tmp_path / "state" / "sessions"
    bindings_file = tmp_path / "state" / "telegram" / "active_bindings.json"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    bindings_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", sessions_dir, raising=False)
    monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", bindings_file, raising=False)

    orch = TaskOrchestrator()
    store = SessionStore()
    session = store.create("codex", str(tmp_path))
    session.machine_id = "kanebra-worker"
    store.save(session)

    task = _make_task(session.session_id)
    task.metadata = {"session_id": session.session_id, "backend": "codex"}
    import asyncio
    orch.active_tasks[task.id] = task
    orch._task_cancel_events[task.id] = asyncio.Event()

    enqueue_calls = []

    class _FakeCodexBackend:
        def cancel(self, s):
            pass

    orch._backends["codex"] = _FakeCodexBackend()

    def _fake_enqueue(s):
        enqueue_calls.append(getattr(s, "session_id", ""))
    orch._enqueue_remote_cancel_turn = _fake_enqueue

    result = orch.cancel_task(task.id)

    assert result is True
    assert enqueue_calls == [], "cancel_turn must NOT be enqueued for codex sessions"


# ---------------------------------------------------------------------------
# Test 4 — _enqueue_remote_cancel_turn: no-op when DB unavailable
# ---------------------------------------------------------------------------

def test_enqueue_remote_cancel_turn_no_db_is_silent(tmp_path, monkeypatch):
    """If the mesh DB is unavailable, _enqueue_remote_cancel_turn logs a warning
    but does not raise — cancel_task must never fail because of a DB error."""
    sessions_dir = tmp_path / "state" / "sessions"
    bindings_file = tmp_path / "state" / "telegram" / "active_bindings.json"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    bindings_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", sessions_dir, raising=False)
    monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", bindings_file, raising=False)

    orch = TaskOrchestrator()
    store = SessionStore()
    session = store.create("claude", str(tmp_path))
    session.machine_id = "kanebra-worker"
    store.save(session)

    import src.control.db as db_module
    monkeypatch.setattr(db_module, "get_db", lambda: None)

    # Should not raise
    orch._enqueue_remote_cancel_turn(session)
