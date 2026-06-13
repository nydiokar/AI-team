from datetime import datetime
from unittest.mock import patch

import pytest

from config import config
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus, Task, TaskPriority, TaskStatus, TaskType
from src.orchestrator import TaskOrchestrator


def _session(session_id: str) -> Session:
    now = datetime.now().isoformat()
    return Session(
        session_id=session_id,
        backend="claude",
        repo_path="/tmp/testrepo",
        status=SessionStatus.BUSY,
        created_at=now,
        updated_at=now,
        machine_id="remote-worker-01",
        backend_session_id="claude-before",
    )


def _task(task_id: str, session_id: str) -> Task:
    now = datetime.now().isoformat()
    return Task(
        id=task_id,
        type=TaskType.FIX,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING,
        created=now,
        title="test task",
        target_files=[],
        prompt="hello",
        success_criteria=[],
        context="",
        metadata={"session_id": session_id},
    )


@pytest.mark.asyncio
async def test_claimed_remote_task_is_not_failed_by_pickup_timeout(tmp_path, monkeypatch):
    db = MeshDB(str(tmp_path / "mesh.db"))
    session = _session("sess_claimed_timeout")
    task = _task("task_claimed_timeout", session.session_id)
    saves = []

    db.enqueue_task(
        task_id=task.id,
        session_id=session.session_id,
        machine_id=session.machine_id,
        backend="claude",
        action="resume_session",
        payload={"prompt": task.prompt, "task_id": task.id},
    )
    assert db.claim_task(task.id, session.machine_id)

    class MinimalOrchestrator:
        _task_cancel_events = {}

        def __init__(self):
            self.session_store = self

        def save(self, saved_session):
            saves.append(saved_session.backend_session_id)

        def _resolve_task_backend(self, _task):
            return "claude"

    sleep_calls = {"count": 0}

    async def complete_on_sleep(_delay):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            db.complete_task(
                task.id,
                {
                    "success": True,
                    "output": "finished after claim",
                    "errors": [],
                    "files_modified": [],
                    "execution_time": 601.0,
                    "timestamp": datetime.now().isoformat(),
                    "return_code": 0,
                    "backend_session_id": "claude-after",
                },
            )

    monkeypatch.setattr(config.mesh, "oneoff_queue_timeout_sec", 0)
    monkeypatch.setattr("asyncio.sleep", complete_on_sleep)

    orch = MinimalOrchestrator()
    bound = TaskOrchestrator._dispatch_to_node.__get__(orch, type(orch))
    with patch("src.control.db.get_db", return_value=db):
        result = await bound(task, session, node=None)

    assert result.success is True
    assert result.output == "finished after claim"
    assert session.backend_session_id == "claude-after"
    assert "claude-after" in saves
    assert db.get_task(task.id)["status"] == "completed"
