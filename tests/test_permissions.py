#!/usr/bin/env python3
"""
Unit tests for least-privilege tool permissions in ClaudeBridge
"""
from pathlib import Path
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.bridges.claude_bridge import ClaudeBridge
from src.core.interfaces import TaskType


def test_permissions_fix_analyze():
    bridge = ClaudeBridge()
    expected = {"Read", "Edit", "MultiEdit", "LS", "Grep", "Glob", "Bash"}
    assert set(bridge._get_allowed_tools_for_task(TaskType.FIX)) == expected
    assert set(bridge._get_allowed_tools_for_task(TaskType.ANALYZE)) == expected


def test_permissions_review_summarize():
    bridge = ClaudeBridge()
    expected = {"Read", "LS", "Grep", "Glob"}
    assert set(bridge._get_allowed_tools_for_task(TaskType.CODE_REVIEW)) == expected
    assert set(bridge._get_allowed_tools_for_task(TaskType.SUMMARIZE)) == expected


