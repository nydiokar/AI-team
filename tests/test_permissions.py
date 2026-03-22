#!/usr/bin/env python3
"""Unit tests for current ClaudeBridge tool permission behavior."""

import pytest

from src.bridges.claude_bridge import ClaudeBridge
from config import config
from src.core.interfaces import Task, TaskPriority, TaskStatus, TaskType


def test_default_allowed_tools_are_single_safe_set():
    bridge = ClaudeBridge()
    expected = {"Read", "Edit", "MultiEdit", "LS", "Grep", "Glob", "Bash"}
    assert set(bridge._get_allowed_tools()) == expected


@pytest.mark.asyncio
async def test_guarded_write_mode_restricts_command_tools(monkeypatch):
    monkeypatch.setenv("GUARDED_WRITE", "true")
    config.reload_from_env()
    bridge = ClaudeBridge()
    captured = {}

    async def fake_execute(command, target_files, cwd_override=None, stdin_input=None):
        captured["command"] = command
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bridge, "_execute_command", fake_execute)
    monkeypatch.setattr(bridge, "_detect_file_changes_from_git", lambda cwd: [])

    task = Task(
        id="t1",
        type=TaskType.FIX,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING,
        created="2026-03-22T00:00:00",
        title="guarded write test",
        target_files=[],
        prompt="test",
        success_criteria=[],
        context="",
        metadata={},
    )

    await bridge.execute_task(task)

    allowed_index = captured["command"].index("--allowedTools")
    allowed_tools = set(captured["command"][allowed_index + 1].split(","))
    assert allowed_tools == {"Read", "LS", "Grep", "Glob"}
    monkeypatch.delenv("GUARDED_WRITE", raising=False)
    config.reload_from_env()


