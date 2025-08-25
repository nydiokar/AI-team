#!/usr/bin/env python3
"""
Tests for results index maintenance and compact context loading
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import sys

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.orchestrator import TaskOrchestrator
from src.core.interfaces import TaskResult
from config import config


def test_results_index_and_compact_context(tmp_path: Path):
    # Arrange: use a unique task id and ensure clean results dir
    task_id = f"test_ctx_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    results_dir = Path(config.system.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    orch = TaskOrchestrator()

    # Minimal successful result with parsed_output content for summary
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

    # Act: write artifacts (writes results/{task_id}.json and updates index.json)
    orch._write_artifacts(task_id, result)

    # Assert: artifact exists
    artifact_path = results_dir / f"{task_id}.json"
    assert artifact_path.exists(), "artifact JSON should be written"

    # Assert: index.json maps task_id to artifact path
    index_path = results_dir / "index.json"
    assert index_path.exists(), "index.json should be created/updated"
    idx = json.loads(index_path.read_text(encoding="utf-8"))
    assert idx.get(task_id) == str(artifact_path)

    # Assert: compact context loads expected fields with caps
    ctx = orch.load_compact_context(task_id)
    assert isinstance(ctx, dict)
    assert ctx.get("constraints", {}).get("prior_success") is True
    assert ctx.get("files_modified") == ["src/orchestrator.py"]
    assert ctx.get("summary", "").startswith("Short summary for testing.")


