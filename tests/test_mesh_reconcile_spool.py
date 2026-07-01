import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from config import config
from src.control.db import MeshDB
from src.core.interfaces import Task, TaskPriority, TaskResult, TaskStatus, TaskType
from src.orchestrator import TaskOrchestrator


def _task(task_id: str) -> Task:
    return Task(
        id=task_id,
        type=TaskType.FIX,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.COMPLETED,
        created=datetime.now().isoformat(),
        title="Reconcile task",
        target_files=[],
        prompt="repair the DB mirror",
        success_criteria=[],
        context="",
        metadata={"source": "runtime"},
    )


def _result(task_id: str) -> TaskResult:
    result = TaskResult(
        task_id=task_id,
        success=True,
        output="full answer for DB mirror",
        errors=[],
        files_modified=["src/orchestrator.py"],
        execution_time=0.01,
        timestamp=datetime.now().isoformat(),
        raw_stdout="",
        parsed_output={"content": "full answer for DB mirror"},
        return_code=0,
        usage={"input_tokens": 1, "output_tokens": 2},
    )
    setattr(result, "backend_name", "claude")
    return result


def test_mesh_completion_spools_when_db_unavailable(tmp_path: Path) -> None:
    old_results_dir: str = config.system.results_dir
    config.system.results_dir = str(tmp_path)
    try:
        orch = TaskOrchestrator()
        task = _task("task_reconcile_spool")
        result = _result(task.id)

        with patch("src.control.db.get_db", return_value=None):
            orch._mesh_complete_task(task, result, artifact_path="results/task_reconcile_spool.json")

        spool_path = tmp_path / "reconcile" / f"{task.id}.json"
        assert spool_path.exists()
        payload = json.loads(spool_path.read_text(encoding="utf-8"))
        assert payload["reconciled"] is False
        assert payload["task"]["prompt"] == "repair the DB mirror"
        assert payload["result"]["output"] == "full answer for DB mirror"
    finally:
        config.system.results_dir = old_results_dir


def test_reconcile_spooled_mesh_completion_writes_mesh_task(tmp_path: Path) -> None:
    old_results_dir: str = config.system.results_dir
    config.system.results_dir = str(tmp_path)
    try:
        orch = TaskOrchestrator()
        task = _task("task_reconcile_replay")
        result = _result(task.id)
        db = MeshDB(str(tmp_path / "mesh.db"))

        with patch("src.control.db.get_db", return_value=None):
            orch._mesh_complete_task(task, result, artifact_path="results/task_reconcile_replay.json")

        with patch("src.control.db.get_db", return_value=db):
            stats = orch.reconcile_spooled_mesh_completions()

        assert stats == {"checked": 1, "reconciled": 1, "failed": 0}
        row = db.get_task(task.id)
        assert row is not None
        assert row["status"] == "completed"
        assert row["prompt"] == "repair the DB mirror"
        assert row["reply_text"] == "full answer for DB mirror"
        assert json.loads(row["files_modified_json"]) == ["src/orchestrator.py"]
        assert json.loads(row["usage_json"]) == {"input_tokens": 1, "output_tokens": 2}

        spool_path = tmp_path / "reconcile" / f"{task.id}.json"
        payload = json.loads(spool_path.read_text(encoding="utf-8"))
        assert payload["reconciled"] is True
        assert payload["reconciled_at"]
    finally:
        config.system.results_dir = old_results_dir
