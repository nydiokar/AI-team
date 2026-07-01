"""
Tests for results index maintenance and compact context loading.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from config import config
from src.control.db import get_db
from src.core.interfaces import TaskResult
from src.orchestrator import TaskOrchestrator


def test_compact_context_prefers_db_task_ledger(tmp_path: Path) -> None:
    old_results_dir: str = config.system.results_dir
    config.system.results_dir = str(tmp_path)
    try:
        task_id = "test_ctx_db"
        db = get_db()
        assert db is not None
        db.enqueue_task(
            task_id=task_id,
            session_id=None,
            machine_id=None,
            backend="claude",
            action="run_oneoff",
            payload={"prompt": "Use the DB prompt", "task_id": task_id, "metadata": {}},
        )
        db.complete_task(
            task_id,
            {
                "success": True,
                "output": "artifact-shaped output",
                "errors": [],
                "files_modified": ["fallback.py"],
                "return_code": 0,
            },
        )
        db.enrich_task(
            task_id,
            prompt="Canonical prompt",
            reply_text="Canonical reply summary",
            parsed_output={"content": "Parsed summary"},
            files_modified=["src/orchestrator.py"],
            usage={"input_tokens": 12, "output_tokens": 4},
            return_code=0,
        )

        ctx = TaskOrchestrator().load_compact_context(task_id)

        assert ctx["source"] == "db"
        assert ctx["task_id"] == task_id
        assert ctx["prompt"] == "Canonical prompt"
        assert ctx["summary"] == "Canonical reply summary"
        assert ctx["constraints"]["prior_success"] is True
        assert ctx["constraints"]["status"] == "completed"
        assert ctx["files_modified"] == ["src/orchestrator.py"]
        assert ctx["usage"] == {"input_tokens": 12, "output_tokens": 4}
    finally:
        config.system.results_dir = old_results_dir


def test_results_index_and_artifact_compact_context_fallback(tmp_path: Path) -> None:
    old_results_dir: str = config.system.results_dir
    config.system.results_dir = str(tmp_path)
    try:
        task_id = f"test_ctx_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        results_dir = Path(config.system.results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)

        orch = TaskOrchestrator()
        result = TaskResult(
            task_id=task_id,
            success=True,
            output="OK",
            errors=[],
            files_modified=["src/orchestrator.py"],
            execution_time=0.01,
            timestamp=datetime.now().isoformat(),
            raw_stdout="",
            raw_stderr="",
            parsed_output={"content": "Short summary for testing."},
            return_code=0,
        )

        orch._write_artifacts(task_id, result)

        artifact_path = results_dir / f"{task_id}.json"
        assert artifact_path.exists(), "artifact JSON should be written"

        index_path = results_dir / "index.json"
        assert index_path.exists(), "index.json should be created/updated"
        idx = json.loads(index_path.read_text(encoding="utf-8"))
        assert idx.get(task_id) == str(artifact_path)

        ctx = orch.load_compact_context(task_id)
        assert ctx["source"] == "artifact"
        assert ctx["constraints"]["prior_success"] is True
        assert ctx["files_modified"] == ["src/orchestrator.py"]
        assert ctx["summary"].startswith("Short summary for testing.")
    finally:
        config.system.results_dir = old_results_dir
