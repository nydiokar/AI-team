"""
Tests for repo-inspection routing (NodeInspector + inspect_ops).

These cover the gateway-canonical guarantee: an inspection runs against the node
that owns the session, never against the gateway host's filesystem. No paid
backend is ever involved — inspect ops only touch git / the filesystem.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from config import config
from src.control.db import MeshDB
from src.control.node_inspector import NodeInspector, InspectError, session_node
from src.control.node_registry import NodeInfo, NodeRegistry
from src.core.interfaces import Session, SessionStatus


def _session(machine_id: str, repo_path: str) -> Session:
    now = datetime.now(tz=timezone.utc).isoformat()
    return Session(
        session_id="sess_inspect_01",
        backend="claude",
        repo_path=repo_path,
        status=SessionStatus.IDLE,
        created_at=now,
        updated_at=now,
        machine_id=machine_id,
    )


def _online_node(node_id: str) -> NodeInfo:
    return NodeInfo.from_dict({
        "node_id": node_id,
        "tailscale_ip": "100.0.0.2",
        "api_port": 9001,
        "capabilities": {"backends": ["claude"], "max_concurrent": 2},
    })


# ---------------------------------------------------------------------------
# session_node — the canonical local/remote predicate
# ---------------------------------------------------------------------------

def test_session_node_local_when_mesh_disabled(monkeypatch):
    monkeypatch.setattr(config.mesh, "enabled", False, raising=False)
    s = _session(machine_id="some-host", repo_path="/tmp/repo")
    assert session_node(s) is None


def test_session_node_local_when_machine_id_not_registered(monkeypatch):
    monkeypatch.setattr(config.mesh, "enabled", True, raising=False)
    reg = NodeRegistry()
    with patch("src.control.node_registry.get_registry", return_value=reg):
        s = _session(machine_id="gateway-hostname", repo_path="/tmp/repo")
        assert session_node(s) is None


def test_session_node_remote_when_registered(monkeypatch):
    monkeypatch.setattr(config.mesh, "enabled", True, raising=False)
    reg = NodeRegistry()
    reg.register(_online_node("worker-1"))
    with patch("src.control.node_registry.get_registry", return_value=reg):
        s = _session(machine_id="worker-1", repo_path="/tmp/repo")
        assert session_node(s) == "worker-1"


# ---------------------------------------------------------------------------
# Local pass-through — runs the op on this host
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_list_dirs_runs_here(monkeypatch, tmp_path):
    monkeypatch.setattr(config.mesh, "enabled", False, raising=False)
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    s = _session(machine_id="", repo_path=str(tmp_path))
    result = await NodeInspector().run(s, "list_dirs", {"sort_by_recent": False})
    assert set(result["dirs"]) == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# Honesty floor — registered-but-offline node must not fall back to local
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_node_raises_instead_of_lying(monkeypatch, tmp_path):
    monkeypatch.setattr(config.mesh, "enabled", True, raising=False)
    reg = NodeRegistry()
    node = _online_node("worker-down")
    reg.register(node)
    node.status = "offline"
    with patch("src.control.node_registry.get_registry", return_value=reg):
        s = _session(machine_id="worker-down", repo_path=str(tmp_path))
        with pytest.raises(InspectError) as ei:
            await NodeInspector().run(s, "git_status")
    assert "offline" in str(ei.value)


# ---------------------------------------------------------------------------
# Remote round-trip — enqueue inspect task, worker-style result, gateway reads
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remote_inspect_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(config.mesh, "enabled", True, raising=False)
    db = MeshDB(str(tmp_path / "mesh.db"))
    reg = NodeRegistry()
    reg.register(_online_node("worker-1"))

    s = _session(machine_id="worker-1", repo_path="/on/worker/repo")

    # Run executor work inline so enqueue-then-poll ordering is deterministic,
    # and simulate the worker completing the task on the first poll.
    async def _inline_to_thread(fn, *a, **k):
        return fn(*a, **k)

    async def complete_on_sleep(_delay):
        rows = db.list_tasks(status="pending")
        if rows:
            db.complete_task(rows[0]["id"], {
                "success": True,
                "inspect": {"dirs": ["src", "tests"], "path": "/on/worker/repo"},
            })

    monkeypatch.setattr("asyncio.to_thread", _inline_to_thread)
    monkeypatch.setattr("asyncio.sleep", complete_on_sleep)
    with patch("src.control.node_registry.get_registry", return_value=reg), \
         patch("src.control.db.get_db", return_value=db):
        result = await NodeInspector().run(s, "list_dirs")

    assert result["dirs"] == ["src", "tests"]
    # The task was pinned to the owning node.
    task = db.list_tasks()[0]
    assert task["machine_id"] == "worker-1"
    assert task["action"] == "inspect"


# ---------------------------------------------------------------------------
# Worker side — _execute_task handles action == "inspect" with no backend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_executes_inspect_action(tmp_path):
    from src.worker.agent import _execute_task

    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    task_row = {
        "id": "inspect_x",
        "action": "inspect",
        "backend": "claude",
        "payload": {
            "action": "inspect",
            "session": {"repo_path": str(tmp_path)},
            "metadata": {"op": "list_dirs", "repo_path": str(tmp_path),
                         "params": {"sort_by_recent": False}},
        },
    }
    # No backends passed — inspection must not require one.
    result = await _execute_task(task_row, backends={})
    assert result["success"] is True
    assert set(result["inspect"]["dirs"]) == {"one", "two"}


@pytest.mark.asyncio
async def test_remote_inspect_failure_propagates(monkeypatch, tmp_path):
    monkeypatch.setattr(config.mesh, "enabled", True, raising=False)
    db = MeshDB(str(tmp_path / "mesh.db"))
    reg = NodeRegistry()
    reg.register(_online_node("worker-1"))
    s = _session(machine_id="worker-1", repo_path="/on/worker/repo")
    db.upsert_session(s)  # gateway always has the session mirrored before inspecting

    async def _inline_to_thread(fn, *a, **k):
        return fn(*a, **k)

    async def fail_on_sleep(_delay):
        rows = db.list_tasks(status="pending")
        if rows:
            db.fail_task(rows[0]["id"], "boom on worker")

    monkeypatch.setattr("asyncio.to_thread", _inline_to_thread)
    monkeypatch.setattr("asyncio.sleep", fail_on_sleep)
    with patch("src.control.node_registry.get_registry", return_value=reg), \
         patch("src.control.db.get_db", return_value=db):
        with pytest.raises(InspectError) as ei:
            await NodeInspector().run(s, "git_status")
    assert "boom on worker" in str(ei.value)
