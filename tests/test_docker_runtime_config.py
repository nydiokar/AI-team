"""Tests for Docker-safe runtime configuration boundaries."""
from __future__ import annotations

import pytest

from config import config
from src.orchestrator import HarnessAdmissionBlocked, TaskOrchestrator, resolve_control_api_hosts


def test_control_api_bind_host_overrides_external_host_policy() -> None:
    assert resolve_control_api_hosts("", "100.1.2.3", "0.0.0.0") == ["0.0.0.0"]


@pytest.mark.asyncio
async def test_controller_without_local_execution_rejects_unpinned_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.system, "local_execution_enabled", False, raising=False)
    orchestrator = TaskOrchestrator()
    task = orchestrator._make_task("Inspect the repository.", source="test")

    with pytest.raises(HarnessAdmissionBlocked, match="local_execution_disabled"):
        await orchestrator._enqueue_task(task)

    assert task.id not in orchestrator.active_tasks
