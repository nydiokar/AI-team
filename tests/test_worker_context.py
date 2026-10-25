from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from scripts.worker_context import WorkerNode, gateway_url, local_workers, summarize


def test_string_heartbeat_reports_actual_tasks_even_when_slot_count_is_zero() -> None:
    node = WorkerNode(node_id="kanebra", live=True, heartbeat_age_sec=2, live_state=json.dumps({
        "active_tasks": ["task-1"], "slots_used": 0, "build_sha": "abc123",
        "active_task_details": {"task-1": {"task_id": "task-1", "backend": "codex", "phase": "running"}},
    }))
    context = summarize(node, [])
    assert context.activity == "busy"
    assert context.tasks[0].backend == "codex"
    assert context.build_sha == "abc123"


@pytest.mark.parametrize("live,age,state,expected", [
    (True, 2, {"active_tasks": [], "slots_used": 0}, "idle"),
    (True, 2, {"active_tasks": [], "slots_used": 1}, "busy"),
    (True, 61, {"active_tasks": [], "slots_used": 0}, "unknown"),
    (False, 2, {"active_tasks": [], "slots_used": 0}, "unknown"),
    (True, None, {"active_tasks": [], "slots_used": 0}, "unknown"),
    (True, 2, None, "unknown"),
])
def test_missing_stale_or_offline_state_never_proves_idle(live, age, state, expected) -> None:
    assert summarize(WorkerNode(node_id="node", live=live, heartbeat_age_sec=age, live_state=state), []).activity == expected


def test_invalid_heartbeat_cannot_report_idle() -> None:
    with pytest.raises(ValueError):
        summarize(WorkerNode(node_id="node", live=True, heartbeat_age_sec=2, live_state='{}'), [])


def test_pm2_output_is_filtered_and_detects_the_hosting_worker(monkeypatch, tmp_path) -> None:
    from scripts import worker_context
    monkeypatch.setenv("PM2_HOME", str(tmp_path))
    (tmp_path / "pm2.pid").write_text(str(os.getpid()))
    row = {"pid": 123, "pm2_env": {"name": "worker", "pm_cwd": "/repo", "pm_exec_path": "/repo/worker_main.py", "WORKER_NODE_ID": "node", "WORKER_TOKEN": "do-not-leak"}}
    monkeypatch.setattr(worker_context.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps([row])))
    monkeypatch.setattr(worker_context.psutil, "Process", lambda pid=None: SimpleNamespace(parents=lambda: [SimpleNamespace(pid=123)], children=lambda **k: [SimpleNamespace(pid=999, cmdline=lambda: ["/bin/claude", "auth", "login"])]))
    workers = local_workers("node")
    assert workers[0].hosts_current_agent is True
    assert workers[0].active_login_pids == [999]
    assert "do-not-leak" not in workers[0].model_dump_json()
    assert local_workers("other") == []


def test_controller_url_uses_dashboard_port_and_ipv6_safely() -> None:
    assert gateway_url({"CONTROLLER_URL": "http://[fd00::1]:9002"}) == "http://[fd00::1]:9003"
    assert gateway_url({"DASHBOARD_URL": "https://gateway.example/"}) == "https://gateway.example"


def test_context_probe_does_not_start_a_pm2_daemon(monkeypatch, tmp_path) -> None:
    from scripts import worker_context
    monkeypatch.setenv("PM2_HOME", str(tmp_path))
    def unexpected(*args, **kwargs):
        raise AssertionError("must not start PM2 without an existing daemon")
    monkeypatch.setattr(worker_context.subprocess, "run", unexpected)
    assert local_workers("node") == []
