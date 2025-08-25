#!/usr/bin/env python3
"""
Golden test: TaskParser deterministically parses a minimal task file.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.core.task_parser import TaskParser
from src.core.interfaces import TaskType, TaskPriority


def test_parser_minimal_golden(tmp_path: Path):
    content = """---
id: golden_test
type: analyze
priority: medium
created: 2025-01-01T00:00:00Z
---

# Title Here

**Target Files:**
- src/orchestrator.py

**Prompt:**
Analyze orchestrator.

**Success Criteria:**
- [ ] Done

**Context:**
None
"""
    p = tmp_path / "golden.task.md"
    p.write_text(content, encoding="utf-8")

    parser = TaskParser()
    t = parser.parse_task_file(str(p))

    assert t.id == "golden_test"
    assert t.type == TaskType.ANALYZE
    assert t.priority == TaskPriority.MEDIUM
    assert t.title == "Title Here"
    assert t.target_files == ["src/orchestrator.py"]
    assert "Analyze orchestrator." in t.prompt
    assert t.success_criteria == ["Done"]

