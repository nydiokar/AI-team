#!/usr/bin/env python3
"""
Unit tests for error recovery tiering (transient vs fatal) and retry policy.
Prefers real LLAMA via Ollama if available; otherwise uses a lightweight stub.
"""
from datetime import datetime
from pathlib import Path
import sys

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from src.orchestrator import TaskOrchestrator
from src.bridges.llama_mediator import LlamaMediator
from src.core.interfaces import Task, TaskType, TaskPriority, TaskStatus, TaskResult


def _make_task(task_id: str = "retry_test") -> Task:
    return Task(
        id=task_id,
        type=TaskType.ANALYZE,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING,
        created=datetime.now().isoformat(),
        title="Test",
        target_files=[],
        prompt="",
        success_criteria=[],
        context="",
        metadata={}
    )


@pytest.mark.asyncio
async def test_retry_on_transient_then_success(monkeypatch):
    orch = TaskOrchestrator()

    # Prefer real LLAMA if Ollama is available and model installed; fallback to stub
    lm = LlamaMediator()
    if lm.ollama_available and lm.client and lm.model_installed:
        orch.llama_mediator = lm
    else:
        class _DummyLlama:
            def parse_task(self, _content: str):
                return {"type": "analyze", "target_files": [], "main_request": "", "priority": "medium", "title": "Test"}
            def create_claude_prompt(self, _parsed):
                return "prompt"
            def summarize_result(self, _result: TaskResult, _task: Task) -> str:
                return "summary"
        orch.llama_mediator = _DummyLlama()

    calls: list[int] = []

    def fake_run_oneoff(cwd: str, message: str):
        calls.append(1)
        if len(calls) == 1:
            # First call: transient failure
            return orch._backends["claude"]._parse(  # type: ignore[attr-defined]
                stdout="",
                stderr="Rate limit exceeded. Please retry later.",
                returncode=1,
                elapsed=0.01,
                known_session_id="",
            )
        # Second call: success
        return orch._backends["claude"]._parse(  # type: ignore[attr-defined]
            stdout="OK",
            stderr="",
            returncode=0,
            elapsed=0.01,
            known_session_id="",
        )

    monkeypatch.setattr(orch._backends["claude"], "run_oneoff", fake_run_oneoff)

    task = _make_task()
    result = await orch.process_task(task)

    # It should have retried once and eventually succeeded
    assert result.success is True
    assert getattr(result, "retries", 0) == 1


@pytest.mark.asyncio
async def test_no_retry_on_fatal(monkeypatch):
    orch = TaskOrchestrator()

    # Prefer real LLAMA if available; fallback to stub
    lm = LlamaMediator()
    if lm.ollama_available and lm.client and lm.model_installed:
        orch.llama_mediator = lm
    else:
        class _DummyLlama:
            def parse_task(self, _content: str):
                return {"type": "analyze", "target_files": [], "main_request": "", "priority": "medium", "title": "Test"}
            def create_claude_prompt(self, _parsed):
                return "prompt"
            def summarize_result(self, _result: TaskResult, _task: Task) -> str:
                return "summary"
        orch.llama_mediator = _DummyLlama()

    def fake_run_oneoff(cwd: str, message: str):
        # Fatal failure (no transient markers)
        return orch._backends["claude"]._parse(  # type: ignore[attr-defined]
            stdout="",
            stderr="Compilation failed",
            returncode=1,
            elapsed=0.01,
            known_session_id="",
        )

    monkeypatch.setattr(orch._backends["claude"], "run_oneoff", fake_run_oneoff)

    task = _make_task("fatal_test")
    result = await orch.process_task(task)

    # Should not retry, and report failure
    assert result.success is False
    assert getattr(result, "retries", 0) == 0
