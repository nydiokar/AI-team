#!/usr/bin/env python3
"""
End-to-end watcher smoke test (Windows-friendly, real mode)
"""
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime

# Add `orchestrator` (parent of `src`) to sys.path so `src` is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.orchestrator import TaskOrchestrator
from src.core.task_parser import TaskParser
from src.core.interfaces import TaskResult


async def run_e2e_watcher_test():
    """Create a temp .task.md, start orchestrator, wait for artifacts, stop."""
    orchestrator = TaskOrchestrator()

    # Check Claude availability; if not available, stub execute_task so we still produce events
    await orchestrator._check_component_status()
    if not orchestrator.component_status.get("claude_available", False):
        async def _fake_execute_task(task):
            # Minimal successful result to drive pipeline and events
            return TaskResult(
                task_id=task.id,
                success=True,
                output="OK",
                errors=[],
                files_modified=[],
                execution_time=0.05,
                timestamp=datetime.now().isoformat(),
                raw_stdout="",
                raw_stderr="",
                parsed_output={"content": "ok"},
                return_code=0,
            )
        orchestrator.claude_bridge.execute_task = _fake_execute_task  # type: ignore

    # Prepare temp task file (read-only)
    tasks_dir = Path(orchestrator.file_watcher.watch_directory)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_id = f"e2e_smoke_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    task_file = tasks_dir / f"{task_id}.task.md"

    task_content = f"""---
id: {task_id}
type: summarize
priority: low
created: {datetime.now().isoformat()}
---

# E2E Watcher Smoke

**Target Files:**
- src/orchestrator.py

**Prompt:**
Summarize the orchestrator file. Do not write any changes.

**Success Criteria:**
- [ ] No writes performed
- [ ] Summary present

**Context:**
Watcher E2E smoke test.
"""
    task_file.write_text(task_content, encoding="utf-8")

    # Ensure logging is configured for tests to capture events
    import logging
    from config import config as app_config
    # Ensure logs directory exists for FileHandler
    logs_dir = Path(app_config.system.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, app_config.system.log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(logs_dir / "orchestrator.log")
        ]
    )

    # Start orchestrator
    await orchestrator.start()

    # Wait up to 60s for artifacts (use configured directories)
    results_dir = Path(app_config.system.results_dir)
    summaries_dir = Path(app_config.system.summaries_dir)

    ok = False
    for _ in range(60):
        await asyncio.sleep(1)
        result_json = results_dir / f"{task_id}.json"
        summary_txt = summaries_dir / f"{task_id}_summary.txt"
        if result_json.exists() and summary_txt.exists():
            # Basic non-empty check
            if result_json.stat().st_size > 0 and summary_txt.stat().st_size > 0:
                ok = True
                break

    # Stop orchestrator
    await orchestrator.stop()

    if ok:
        # Print summary content for verification
        summary_content = summary_txt.read_text(encoding="utf-8") if summary_txt.exists() else "No summary found"
        print("\nSummary content preview:")
        print("-" * 40)
        print(summary_content[:500] + "..." if len(summary_content) > 500 else summary_content)
        print("-" * 40)
        
        # Stronger assertions: concise human-readable summary (no raw JSON dump) and results JSON has validation
        import json
        data = json.loads(result_json.read_text(encoding="utf-8"))
        assert isinstance(data.get("validation"), (dict, type(None)))
        # Summary should be non-trivial text and not a JSON blob
        assert len(summary_content.strip()) >= 50
        assert "{" not in summary_content[:500] and "}\n" not in summary_content[:500]
        print("OK: Artifacts created and validated for E2E watcher smoke test")
        
        # Cleanup: move processed task to processed/ (or ensure it's already archived)
        processed_dir = tasks_dir / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)
        if task_file.exists():
            # If not auto-archived, move it
            target = processed_dir / f"{task_id}.completed.task.md"
            try:
                task_file.replace(target)
            except Exception:
                pass
        return True
    else:
        print("FAIL: Artifacts not found within timeout")
        return False


def test_e2e_watcher():
    """Pytest test function for E2E watcher smoke test"""
    success = asyncio.run(run_e2e_watcher_test())
    assert success, "E2E watcher test failed"


if __name__ == "__main__":
    success = asyncio.run(run_e2e_watcher_test())
    sys.exit(0 if success else 1)


