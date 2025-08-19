#!/usr/bin/env python3
"""
Simulate transient error classification: feed a fake TaskResult into classifier.
This avoids needing to hit the actual CLI.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.orchestrator import TaskOrchestrator
from src.core.interfaces import TaskResult


def main():
    orch = TaskOrchestrator()
    # Simulate a failure that looks transient
    tr = TaskResult(
        task_id="retry_test",
        success=False,
        output="",
        errors=["HTTP 429 Too Many Requests"],
        files_modified=[],
        execution_time=0.1,
        timestamp="",
        raw_stdout="",
        raw_stderr="Rate limit exceeded. Please retry later.",
        parsed_output=None,
        return_code=1,
    )
    cls = orch._classify_error(tr)
    print(f"class={cls}")
    return 0 if cls == "transient" else 1


if __name__ == "__main__":
    sys.exit(main())


