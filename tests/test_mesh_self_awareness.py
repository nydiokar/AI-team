import asyncio

import pytest

from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from src.orchestrator import TaskOrchestrator
from src.worker.agent import WorkerAgent, _mark_nudge_received


def _session(session_id: str, status: SessionStatus = SessionStatus.BUSY) -> Session:
    return Session(
        session_id=session_id,
        backend="claude",
        repo_path="/tmp/repo",
        status=status,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        machine_id="worker-a",
        last_task_id=f"task_{session_id}",
    )


@pytest.mark.asyncio
async def test_nudge_listener_wakes_poll_and_heartbeat_events():
    poll_event = asyncio.Event()
    heartbeat_event = asyncio.Event()

    _mark_nudge_received(poll_event, heartbeat_event)

    assert poll_event.is_set()
    assert heartbeat_event.is_set()


@pytest.mark.asyncio
async def test_heartbeat_wait_returns_immediately_on_nudge_event():
    agent = WorkerAgent.__new__(WorkerAgent)
    agent._shutdown = asyncio.Event()
    agent._heartbeat_now = asyncio.Event()

    agent._heartbeat_now.set()
    await asyncio.wait_for(agent._wait_for_next_heartbeat(), timeout=0.1)


def test_list_stale_busy_sessions_excludes_pending_and_claimed(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    stale = _session("stale")
    pending = _session("pending")
    claimed = _session("claimed")
    idle = _session("idle", status=SessionStatus.AWAITING_INPUT)

    for session in (stale, pending, claimed, idle):
        db.upsert_session(session)

    db.enqueue_task(
        task_id=pending.last_task_id,
        session_id=pending.session_id,
        machine_id=pending.machine_id,
        backend="claude",
        action="resume_session",
        payload={"task_id": pending.last_task_id, "prompt": "pending"},
    )
    db.enqueue_task(
        task_id=claimed.last_task_id,
        session_id=claimed.session_id,
        machine_id=claimed.machine_id,
        backend="claude",
        action="resume_session",
        payload={"task_id": claimed.last_task_id, "prompt": "claimed"},
    )
    assert db.claim_task(claimed.last_task_id, claimed.machine_id)

    rows = db.list_stale_busy_sessions()
    assert [row["session_id"] for row in rows] == ["stale"]


def test_dispatch_nudge_uses_node_address(monkeypatch):
    calls = []

    class _Node:
        node_id = "worker-a"
        tailscale_ip = "100.64.0.10"
        api_port = 9001

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(req, timeout):
        calls.append((req.full_url, req.get_method(), timeout))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)

    assert orchestrator._nudge_worker_for_dispatch(_Node(), "worker-a", db=None) is True
    assert calls == [("http://100.64.0.10:9001/nudge", "POST", 2)]


def test_dispatch_nudge_falls_back_to_db_node(monkeypatch, tmp_path):
    calls = []
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_node("worker-a", "100.64.0.11", 9002, ["claude"], 2)

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(req, timeout):
        calls.append((req.full_url, req.get_method(), timeout))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)

    assert orchestrator._nudge_worker_for_dispatch(None, "worker-a", db=db) is True
    assert calls == [("http://100.64.0.11:9002/nudge", "POST", 2)]


@pytest.mark.asyncio
async def test_reconcile_stale_busy_sessions_marks_error(monkeypatch, tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    stale = _session("stale")
    db.upsert_session(stale)

    saved = []
    events = []
    session_events = []

    class _Store:
        def get(self, session_id):
            return stale if session_id == stale.session_id else None

        def save(self, session):
            saved.append(session)
            db.upsert_session(session)

    async def direct_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
    orchestrator.active_tasks = {}
    orchestrator.session_store = _Store()
    orchestrator._append_session_event = lambda *args: session_events.append(args)
    orchestrator._emit_event = lambda *args: events.append(args)

    monkeypatch.setattr("src.control.db.get_db", lambda: db)
    monkeypatch.setattr("asyncio.to_thread", direct_to_thread)

    assert await orchestrator._reconcile_stale_busy_sessions_once() == 1
    assert stale.status == SessionStatus.ERROR
    assert "mesh reconciliation" in stale.last_result_summary
    assert saved == [stale]
    assert session_events[0][0] == stale.session_id
    assert events[0][0] == "stale_busy_session_reconciled"


@pytest.mark.asyncio
async def test_reconcile_stale_busy_sessions_skips_gateway_active_task(monkeypatch, tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    stale = _session("active")
    db.upsert_session(stale)

    class _Store:
        def get(self, session_id):
            return stale if session_id == stale.session_id else None

        def save(self, session):
            raise AssertionError("active in-memory task should not be reconciled")

    async def direct_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
    orchestrator.active_tasks = {stale.last_task_id: object()}
    orchestrator.session_store = _Store()
    orchestrator._append_session_event = lambda *args: None
    orchestrator._emit_event = lambda *args: None

    monkeypatch.setattr("src.control.db.get_db", lambda: db)
    monkeypatch.setattr("asyncio.to_thread", direct_to_thread)

    assert await orchestrator._reconcile_stale_busy_sessions_once() == 0
    assert stale.status == SessionStatus.BUSY
