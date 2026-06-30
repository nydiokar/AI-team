import json
from datetime import datetime
from unittest.mock import patch

from src.control.db import MeshDB
from src.core.interfaces import (
    ExecutionResult,
    Session,
    SessionStatus,
    Task,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from src.orchestrator import TaskOrchestrator
from src.worker.agent import _usage_from_execution_result


CLAUDE_USAGE_NDJSON = json.dumps(
    {
        "type": "assistant",
        "message": {
            "usage": {
                "input_tokens": 3,
                "cache_creation_input_tokens": 27,
                "cache_read_input_tokens": 14338,
                "output_tokens": 8,
            }
        },
    }
)


def _usage():
    return {
        "input_tokens": 3,
        "cached_input_tokens": 14365,
        "output_tokens": 8,
        "reasoning_output_tokens": 0,
    }


def _task(task_id: str, session_id: str = "sess_usage") -> Task:
    now = datetime.now().isoformat()
    return Task(
        id=task_id,
        type=TaskType.FIX,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING,
        created=now,
        title="usage test",
        target_files=[],
        prompt="say exactly: canary one",
        success_criteria=[],
        context="",
        metadata={"session_id": session_id},
    )


def test_worker_execution_result_includes_usage_from_raw_stdout():
    result = ExecutionResult(
        success=True,
        output="canary one",
        raw_stdout=CLAUDE_USAGE_NDJSON,
        backend_session_id="claude-session",
    )

    assert _usage_from_execution_result(result) == _usage()


def test_task_server_persists_payload_usage_into_mesh_task(tmp_path):
    from config import config as cfg
    import src.control.db as db_mod

    cfg.mesh.db_path = str(tmp_path / "mesh_task_server_usage.db")
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old is not None:
        old.close()

    try:
        from src.control.task_server import ExecutionResultPayload, submit_result

        db = db_mod.get_db()
        assert db is not None
        db.enqueue_task(
            task_id="task_server_usage",
            session_id=None,
            machine_id=None,
            backend="claude",
            action="resume_session",
            payload={"prompt": "say exactly: canary one"},
        )
        assert db.claim_task("task_server_usage", "worker-a")

        resp = submit_result(
            "task_server_usage",
            ExecutionResultPayload(
                node_id="worker-a",
                success=True,
                output="canary one",
                usage=_usage(),
            ),
        )

        assert resp["status"] == "accepted"
        row = db.get_task("task_server_usage")
        assert json.loads(row["usage_json"]) == _usage()
        assert json.loads(row["result"])["usage"] == _usage()
    finally:
        db_mod._db_instance = None
        if old is not None:
            old.close()
        db_mod._db_instance = old


def test_mesh_complete_task_prefers_structured_usage_when_stdout_has_no_ndjson(tmp_path):
    db = MeshDB(str(tmp_path / "mesh_orchestrator_usage.db"))
    task = _task("task_orch_usage")
    db.enqueue_task(
        task_id=task.id,
        session_id=None,
        machine_id=None,
        backend="claude",
        action="resume_session",
        payload={"prompt": task.prompt},
    )
    assert db.claim_task(task.id, "local")

    from src.core.interfaces import TaskResult

    result = TaskResult(
        task_id=task.id,
        success=True,
        output="canary one",
        errors=[],
        files_modified=[],
        execution_time=0.01,
        timestamp=datetime.now().isoformat(),
        raw_stdout="canary one",
        usage=_usage(),
    )

    class Store:
        def get(self, _session_id):
            return Session(
                session_id="sess_usage",
                backend="claude",
                repo_path="/tmp",
                status=SessionStatus.IDLE,
                created_at=datetime.now().isoformat(),
                updated_at=datetime.now().isoformat(),
            )

    class MinimalOrchestrator:
        session_store = Store()
        _session_reply_text = TaskOrchestrator._session_reply_text
        _short_failure_reason = TaskOrchestrator._short_failure_reason

    bound = TaskOrchestrator._mesh_complete_task.__get__(MinimalOrchestrator(), MinimalOrchestrator)
    with patch("src.control.db.get_db", return_value=db):
        bound(task, result, artifact_path=None)

    row = db.get_task(task.id)
    assert json.loads(row["usage_json"]) == _usage()
