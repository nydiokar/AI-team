#!/usr/bin/env python3
"""
End-to-end watcher smoke test (Windows-friendly, real mode)
"""
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime

# Add project root (which contains the `src` package) to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.orchestrator import TaskOrchestrator
from src.core.task_parser import TaskParser


async def run_e2e_watcher_test():
    """Create a temp .task.md, start orchestrator, wait for artifacts, stop."""
    orchestrator = TaskOrchestrator()

    # Check Claude availability; skip if not available to keep test stable
    await orchestrator._check_component_status()
    if not orchestrator.component_status.get("claude_available", False):
        print("SKIP: Claude CLI not available; e2e watcher test skipped.")
        return True

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
- orchestrator/src/orchestrator.py

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
    logging.basicConfig(
        level=getattr(logging, app_config.system.log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(Path(app_config.system.logs_dir) / "orchestrator.log")
        ]
    )

    # Start orchestrator
    await orchestrator.start()

    # Wait up to 60s for artifacts
    results_dir = Path(orchestrator.file_watcher.watch_directory).parent / "results"
    summaries_dir = Path(orchestrator.file_watcher.watch_directory).parent / "summaries"

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
        print("OK: Artifacts created for E2E watcher smoke test")
        return True
    else:
        print("FAIL: Artifacts not found within timeout")
        return False


if __name__ == "__main__":
    success = asyncio.run(run_e2e_watcher_test())
    sys.exit(0 if success else 1)


