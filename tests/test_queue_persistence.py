#!/usr/bin/env python3
"""
Test queue persistence by simulating a pending task on restart.
"""
from pathlib import Path
import sys
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import asyncio
import json
import pytest

from src.orchestrator import TaskOrchestrator
from config import config
from src.core.interfaces import TaskResult


@pytest.mark.asyncio
async def test_resume_pending_task(tmp_path, monkeypatch):
    # Use a temp tasks dir to avoid interfering with real tasks
    tasks_dir = tmp_path / "tasks"
    results_dir = tmp_path / "results"
    summaries_dir = tmp_path / "summaries"
    logs_dir = tmp_path / "logs"
    for d in (tasks_dir, results_dir, summaries_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Monkeypatch config paths
    monkeypatch.setattr(config.system, "tasks_dir", str(tasks_dir), raising=False)
    monkeypatch.setattr(config.system, "results_dir", str(results_dir), raising=False)
    monkeypatch.setattr(config.system, "summaries_dir", str(summaries_dir), raising=False)
    monkeypatch.setattr(config.system, "logs_dir", str(logs_dir), raising=False)

    # Create a pending task file
    task_id = "resume_smoke"
    task_file = tasks_dir / f"{task_id}.task.md"
    task_file.write_text(
        f"""---\nid: {task_id}\ntype: summarize\npriority: low\ncreated: {datetime.now().isoformat()}\n---\n\n# Resume Test\n\n**Target Files:**\n- src/orchestrator.py\n\n**Prompt:**\nSummarize.\n\n**Success Criteria:**\n- [ ] Summary present\n\n**Context:**\nQueue persistence test.\n""",
        encoding="utf-8",
    )

    # Pre-write state.json indicating this file is pending
    state_path = logs_dir / "state.json"
    state_path.write_text(json.dumps({"pending_files": [str(task_file)], "updated": datetime.now().isoformat()}), encoding="utf-8")

    orch = TaskOrchestrator()

    # Stub execute_task to succeed fast so we don't require Claude
    async def _fake_execute_task(task):
        return TaskResult(
            task_id=task.id,
            success=True,
            output="OK",
            errors=[],
            files_modified=[],
            execution_time=0.01,
            timestamp=datetime.now().isoformat(),
            raw_stdout="",
            raw_stderr="",
            parsed_output={"content": "ok"},
            return_code=0,
        )
    monkeypatch.setattr(orch.claude_bridge, "execute_task", _fake_execute_task)

    await orch.start()

    # Wait briefly for processing
    for _ in range(30):
        await asyncio.sleep(0.2)
        if (results_dir / f"{task_id}.json").exists() and (summaries_dir / f"{task_id}_summary.txt").exists():
            break

    await orch.stop()

    # Assert artifacts exist and state has been cleared
    assert (results_dir / f"{task_id}.json").exists()
    assert (summaries_dir / f"{task_id}_summary.txt").exists()
    # state.json should have empty pending or not include this file
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert str(task_file) not in set(data.get("pending_files", []))


