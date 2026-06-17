import asyncio

import pytest

from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
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
