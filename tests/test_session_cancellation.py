from datetime import datetime
from pathlib import Path
import sys
import uuid
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.orchestrator import TaskOrchestrator
from src.core.interfaces import Task, TaskType, TaskPriority, TaskStatus, ExecutionResult
from src.core.session_store import SessionStore
import src.core.session_store as session_store_module
from config import config


def _make_task(session_id: str) -> Task:
    return Task(
        id=f"timeout_{uuid.uuid4().hex[:8]}",
        type=TaskType.ANALYZE,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING,
        created=datetime.now().isoformat(),
        title="Timeout session task",
        target_files=[],
        prompt="Do work",
        success_criteria=[],
        context="",
        metadata={"session_id": session_id, "timeout_sec": 1},
    )


@pytest.mark.asyncio
async def test_session_timeout_calls_backend_cancel(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "state" / "sessions"
    bindings_file = tmp_path / "state" / "telegram" / "active_bindings.json"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    bindings_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", sessions_dir, raising=False)
    monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", bindings_file, raising=False)

    orch = TaskOrchestrator()
    store = SessionStore()
    session = store.create("codex", str(tmp_path))

    cancelled = []
    release = threading.Event()

    class _FakeBackend:
        def create_session(self, session_obj):
            release.wait(timeout=2.0)
            return ExecutionResult(success=True, output="late", execution_time=0.0)

        def resume_session(self, session_obj, message):
            release.wait(timeout=2.0)
            return ExecutionResult(success=True, output="late", execution_time=0.0)

        def run_oneoff(self, cwd, message):
            return ExecutionResult(success=True, output="noop", execution_time=0.0)

        def cancel(self, session_obj):
            cancelled.append(session_obj.session_id)
            release.set()

        def close(self, session_obj):
            return None

    orch._backends["codex"] = _FakeBackend()

    start = time.time()
    result = await orch.process_task(_make_task(session.session_id))
    elapsed = time.time() - start

    assert result.success is False
    assert "timeout after 1s" in result.errors[0]
    assert cancelled == [session.session_id]
    assert elapsed < 2.0
