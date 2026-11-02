import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from config import config
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus, Task, TaskPriority, TaskStatus, TaskType
from src.orchestrator import TaskOrchestrator
from tests.stage8a_legacy import claim_pre_cutover, enqueue_pre_cutover


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
async def test_reattached_claimed_task_is_not_failed_by_pickup_timeout(tmp_path, monkeypatch):
    db = MeshDB(str(tmp_path / "mesh.db"))
    session = _session("sess_claimed_timeout")
    task = _task("task_claimed_timeout", session.session_id)
    saves = []

    db.upsert_session(session)
    enqueue_pre_cutover(
        db,
        task_id=task.id,
        session_id=session.session_id,
        machine_id=session.machine_id,
        backend="claude",
        action="resume_session",
        payload={"prompt": task.prompt, "task_id": task.id},
    )
    assert claim_pre_cutover(db, task.id, session.machine_id)

    class MinimalOrchestrator:
        running = True
        _recover_completed_session = TaskOrchestrator._recover_completed_session

        def __init__(self):
            self.session_store = self
            self.notifier = SimpleNamespace(
                notify_task_outcome=AsyncMock(), notify_error=AsyncMock(),
            )

        def save(self, saved_session):
            saves.append(saved_session.backend_session_id)

        def _write_session_summary(self, *args):
            pass

        def _append_session_event(self, *args):
            pass

        def _emit_event(self, *args):
            pass

        def _emit_task_finished(self, *args, **kwargs):
            pass

    sleep_calls = {"count": 0}
    real_sleep = asyncio.sleep

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
        await real_sleep(0)

    monkeypatch.setattr(config.mesh, "oneoff_queue_timeout_sec", 0)

    orch = MinimalOrchestrator()
    bound = TaskOrchestrator._reattach_remote_task.__get__(orch, type(orch))
    with patch("src.control.db.get_db", return_value=db):
        with patch("asyncio.sleep", complete_on_sleep):
            await bound(session, db.get_task(task.id))

    result = orch.notifier.notify_task_outcome.await_args.args[1]
    assert result.success is True
    assert result.output == "finished after claim"
    assert session.backend_session_id == "claude-after"
    assert "claude-after" in saves
    assert db.get_task(task.id)["status"] == "completed"
    orch.notifier.notify_error.assert_not_awaited()
    db.close()
