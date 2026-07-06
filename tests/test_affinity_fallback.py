"""AGENT_18 — pinned-worker offline fallback (bounded hold-and-requeue).

Covers the offline-pinned-node decision table added to
``TaskOrchestrator._process_task_remote``:

    | condition                     | state                       | event                    |
    |-------------------------------|-----------------------------|--------------------------|
    | node offline, grace=0         | ERROR (legacy A11)          | mesh_routing_failed      |
    | node offline, within grace    | PAUSED_PINNED_NODE_OFFLINE  | affinity_hold_started    |
    | node returns within grace     | BUSY -> AWAITING_INPUT      | affinity_hold_resolved   |
    | grace expires                 | PINNED_NODE_OFFLINE         | affinity_offline_timeout |

Plus the affinity claim-filter invariant (a pinned task is claimable only by its
pinned node; unpinned tasks are claimable by anyone) — the independent, DB-level
guarantee that the mesh never runs a pinned turn off-host.

No paid CLI turn; pure unit tests with a controllable clock + fake registry.
"""
import asyncio
import types
from datetime import datetime
from unittest.mock import patch

import pytest

from config import config
from src.control.db import MeshDB
from src.control.node_registry import NodeCapabilities, NodeInfo
from src.core.interfaces import (
    Session,
    SessionStatus,
    Task,
    TaskPriority,
    TaskResult,
    TaskStatus,
    TaskType,
)
from src.orchestrator import TaskOrchestrator


# --------------------------------------------------------------------------- #
# Fixtures / harness
# --------------------------------------------------------------------------- #
def _session(session_id: str = "sess-affinity", machine_id: str = "remote-worker-01") -> Session:
    now = datetime.now().isoformat()
    return Session(
        session_id=session_id,
        backend="claude",
        repo_path="/tmp/testrepo",
        status=SessionStatus.BUSY,
        created_at=now,
        updated_at=now,
        machine_id=machine_id,
        backend_session_id="claude-before",
    )


def _task(task_id: str = "task-affinity", session_id: str = "sess-affinity") -> Task:
    now = datetime.now().isoformat()
    return Task(
        id=task_id,
        type=TaskType.FIX,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING,
        created=now,
        title="test task",
        target_files=[],
        prompt="hello",
        success_criteria=[],
        context="",
        metadata={"session_id": session_id},
    )


def _node(node_id: str = "remote-worker-01", status: str = "online") -> NodeInfo:
    return NodeInfo(
        node_id=node_id,
        tailscale_ip="100.64.0.2",
        api_port=9001,
        capabilities=NodeCapabilities(backends=["claude"], max_concurrent=2),
        status=status,
    )


class _FakeRegistry:
    """Registry whose single node handle is mutable so a test can flip it
    online mid-hold. ``node=None`` models a node absent from the registry."""

    def __init__(self, node):
        self.node = node

    def get(self, _node_id):
        return self.node


class _Store:
    def __init__(self):
        self.saved_states = []

    def save(self, session):
        self.saved_states.append(session.status)


def _make_orch(dispatch_result=None):
    """A minimal object carrying exactly the attributes _process_task_remote
    touches, so the real method can be bound to it and exercised in isolation."""
    orch = types.SimpleNamespace()
    orch.session_store = _Store()
    orch.events = []
    orch._task_cancel_events = {}
    orch.telegram_interface = None
    orch._telemetry_sink = types.SimpleNamespace(emit=lambda *a, **k: None)

    orch._emit_event = lambda name, task=None, extra=None: orch.events.append(name)
    orch._classify_error = lambda result: "offline"

    async def _dispatch_to_node(task, session, node):
        # Record which node we dispatched to — the affinity assertion.
        orch.dispatched_to = node.node_id if node is not None else session.machine_id
        return dispatch_result

    orch._dispatch_to_node = _dispatch_to_node
    orch.dispatched_to = None
    return orch


def _bind(orch):
    return TaskOrchestrator._process_task_remote.__get__(orch, TaskOrchestrator)


class _Clock:
    """Rebound as the orchestrator module's ``time`` — only ``.time()`` is used
    on the exercised path. Advanced by the patched ``asyncio.sleep``."""

    def __init__(self, start=1000.0):
        self.t = start

    def time(self):
        return self.t


def _install_clock(monkeypatch, clock, on_advance=None):
    monkeypatch.setattr("src.orchestrator.time", clock)
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        clock.t += float(delay) if delay and delay > 0 else 0.0
        if on_advance is not None:
            on_advance(clock)
        await real_sleep(0)

    return patch("asyncio.sleep", fake_sleep)


# --------------------------------------------------------------------------- #
# 1. grace=0 reproduces the legacy A11 immediate-ERROR path (regression lock)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pinned_offline_grace_zero_fails_immediately(monkeypatch):
    monkeypatch.setattr(config.mesh, "affinity_offline_grace_sec", 0)
    orch = _make_orch()
    session, task = _session(), _task()

    with patch("src.control.node_registry.get_registry", return_value=_FakeRegistry(None)), \
         patch("src.control.db.get_db", return_value=None):
        result = await _bind(orch)(task, session, start_time=0.0, timeout_s=60)

    assert result.success is False
    assert session.status is SessionStatus.ERROR
    assert result.retries == 0
    assert "offline" in result.errors[0]
    # No hold machinery must engage on the legacy path.
    assert "affinity_hold_started" not in orch.events
    assert "affinity_offline_timeout" not in orch.events
    assert "mesh_routing_failed" in orch.events


# --------------------------------------------------------------------------- #
# 2. offline within grace -> holds, node returns, dispatches remote (never local)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pinned_offline_within_grace_holds_then_dispatches(monkeypatch):
    monkeypatch.setattr(config.mesh, "affinity_offline_grace_sec", 30)
    monkeypatch.setattr(config.mesh, "affinity_offline_poll_interval_sec", 5.0)

    ok = TaskResult(
        task_id="task-affinity", success=True, output="done", errors=[],
        files_modified=[], execution_time=1.0, timestamp=datetime.now().isoformat(),
    )
    orch = _make_orch(dispatch_result=ok)
    session, task = _session(), _task()
    registry = _FakeRegistry(_node(status="offline"))

    clock = _Clock()

    def bring_online(_clock):
        # Node re-registers on the 2nd poll (~10s into the 30s grace window).
        if _clock.t >= 1010.0:
            registry.node.status = "online"

    with patch("src.control.node_registry.get_registry", return_value=registry), \
         patch("src.control.db.get_db", return_value=None), \
         _install_clock(monkeypatch, clock, on_advance=bring_online):
        result = await _bind(orch)(task, session, start_time=1000.0, timeout_s=60)

    assert result.success is True
    # Held in the honest PAUSED state, then resumed to BUSY before dispatch.
    assert SessionStatus.PAUSED_PINNED_NODE_OFFLINE in orch.session_store.saved_states
    assert orch.events.count("affinity_hold_started") == 1
    assert orch.events.count("affinity_hold_resolved") == 1
    assert "mesh_dispatch" in orch.events
    assert "affinity_offline_timeout" not in orch.events
    # The turn ran on the pinned node — never relocated.
    assert orch.dispatched_to == "remote-worker-01"
    assert session.status is SessionStatus.AWAITING_INPUT


# --------------------------------------------------------------------------- #
# 3. grace expires -> honest, resumable PINNED_NODE_OFFLINE (NOT bare ERROR)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pinned_offline_grace_expires_honest_terminal(monkeypatch):
    monkeypatch.setattr(config.mesh, "affinity_offline_grace_sec", 20)
    monkeypatch.setattr(config.mesh, "affinity_offline_poll_interval_sec", 5.0)

    orch = _make_orch()
    session, task = _session(), _task()
    registry = _FakeRegistry(_node(status="offline"))  # stays offline forever
    clock = _Clock()

    with patch("src.control.node_registry.get_registry", return_value=registry), \
         patch("src.control.db.get_db", return_value=None), \
         _install_clock(monkeypatch, clock):
        result = await _bind(orch)(task, session, start_time=1000.0, timeout_s=60)

    assert result.success is False
    assert session.status is SessionStatus.PINNED_NODE_OFFLINE
    assert session.status is not SessionStatus.ERROR
    assert result.retries >= 1  # reflects the wait (polls performed)
    assert orch.events.count("affinity_hold_started") == 1
    assert "affinity_offline_timeout" in orch.events
    assert orch.dispatched_to is None  # nothing ever dispatched


# --------------------------------------------------------------------------- #
# 4. operator cancel during the hold -> CANCELLED, no dispatch
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pinned_offline_cancelled_during_hold(monkeypatch):
    monkeypatch.setattr(config.mesh, "affinity_offline_grace_sec", 30)
    monkeypatch.setattr(config.mesh, "affinity_offline_poll_interval_sec", 5.0)

    orch = _make_orch()
    session, task = _session(), _task()
    cancel_ev = asyncio.Event()
    cancel_ev.set()  # operator cancels immediately
    orch._task_cancel_events = {task.id: cancel_ev}
    registry = _FakeRegistry(_node(status="offline"))
    clock = _Clock()

    with patch("src.control.node_registry.get_registry", return_value=registry), \
         patch("src.control.db.get_db", return_value=None), \
         _install_clock(monkeypatch, clock):
        result = await _bind(orch)(task, session, start_time=1000.0, timeout_s=60)

    assert result.success is False
    assert session.status is SessionStatus.CANCELLED
    assert orch.dispatched_to is None


# --------------------------------------------------------------------------- #
# 5. affinity claim filter — the DB-level invariant (defense in depth)
# --------------------------------------------------------------------------- #
def test_local_pool_never_claims_pinned_task(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    session = _session("sess-pin", machine_id="node-a")
    db.upsert_session(session)
    db.enqueue_task(
        task_id="t-pinned",
        session_id=session.session_id,
        machine_id="node-a",
        backend="claude",
        action="resume_session",
        payload={"prompt": "hi", "task_id": "t-pinned"},
    )

    def _ids(**kw):
        return [t["id"] for t in db.get_pending_tasks(**kw)]

    # A pinned task is invisible to any other node's claim scan — both in the
    # permissive (unpinned-accepting) and strict scans a local worker pool uses.
    assert "t-pinned" not in _ids(node_id="node-b")
    assert "t-pinned" not in _ids(node_id="node-b", accept_unpinned=False)
    # ... and visible only to its pinned node.
    assert "t-pinned" in _ids(node_id="node-a")


def test_unpinned_task_claimable_by_any_node(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    session = _session("sess-unpinned", machine_id="")
    db.upsert_session(session)
    db.enqueue_task(
        task_id="t-unpinned",
        session_id=session.session_id,
        machine_id=None,  # unpinned ⇒ any node
        backend="claude",
        action="run_oneoff",
        payload={"prompt": "hi", "task_id": "t-unpinned"},
    )
    ids_b = [t["id"] for t in db.get_pending_tasks(node_id="node-b")]
    assert "t-unpinned" in ids_b


# --------------------------------------------------------------------------- #
# 6. defense-in-depth: if the resolved dispatch target ever differs from the
#    pinned host, fail CLOSED — never run the turn off-host (the A11 regression
#    class). The claim filter (test 5) is the primary guard; this assert is the
#    belt-and-braces at the dispatch site.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dispatch_target_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(config.mesh, "affinity_offline_grace_sec", 0)
    orch = _make_orch(dispatch_result=None)
    session, task = _session(), _task()  # pinned to "remote-worker-01"
    # An ONLINE node whose id does NOT match the pin — the exact silent-fork bug.
    registry = _FakeRegistry(_node(node_id="some-other-host", status="online"))

    with patch("src.control.node_registry.get_registry", return_value=registry), \
         patch("src.control.db.get_db", return_value=None):
        result = await _bind(orch)(task, session, start_time=1000.0, timeout_s=60)

    assert result.success is False
    assert orch.dispatched_to is None                 # never dispatched off-host
    assert "affinity violation" in result.errors[0]


# --------------------------------------------------------------------------- #
# 7. reversibility contract — the feature ships DISABLED by default (grace=0 ⇒
#    byte-identical A11), so a redeploy changes no live behavior until opt-in.
# --------------------------------------------------------------------------- #
def test_grace_default_is_disabled():
    from config.settings import MeshConfig

    assert MeshConfig().affinity_offline_grace_sec == 0
