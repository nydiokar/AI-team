"""Pinned carrier routing through the canonical durable queue; offline stays pinned."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from src.control import turn_queue as tq
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from tests.test_turn_queue_4b import _pass, _wire
from tests.test_turn_queue_producer1 import (
    _no_cli_spawn, _register_carrier, _setup, _submit,
)


def test_unknown_pinned_carrier_never_falls_back_to_a_live_alternative(tmp_path, monkeypatch):
    db, orchestrator = _setup(tmp_path, monkeypatch, machine="missing-carrier")
    _register_carrier(db, "other-live-node")
    with pytest.raises(tq.CarrierUnavailableError):
        _submit(orchestrator, operation_id="missing-pin")
    assert db.get_active_turn("sess-1") is None
    assert db._conn().execute("SELECT COUNT(*) FROM mesh_tasks").fetchone()[0] == 0


def test_offline_registered_carrier_keeps_the_queued_turn_pinned(tmp_path, monkeypatch):
    db, orchestrator = _setup(tmp_path, monkeypatch)
    db._conn().execute("UPDATE nodes SET status = 'offline' WHERE node_id = 'worker-a'")
    _register_carrier(db, "other-live-node")
    turn_id = str(_submit(orchestrator, operation_id="offline-pin"))
    assert _pass(db, orchestrator).activated == 0
    row = db.get_task(turn_id)
    assert row["status"] == "queued" and row["machine_id"] == "worker-a"
    assert row["blocked_reason"].startswith("carrier_offline")
    assert db.get_pending_managed_turns(node_id="other-live-node", backends=["claude"]) == []


def test_returning_carrier_activates_its_original_turn(tmp_path, monkeypatch):
    db, orchestrator = _setup(tmp_path, monkeypatch)
    db._conn().execute("UPDATE nodes SET status = 'offline' WHERE node_id = 'worker-a'")
    turn_id = str(_submit(orchestrator, operation_id="returning-pin"))
    assert _pass(db, orchestrator).activated == 0
    _register_carrier(db, "worker-a")
    # Advance the bounded retry eligibility without a real clock wait.
    db._conn().execute("UPDATE mesh_tasks SET blocked_until = NULL WHERE id = ?", (turn_id,))
    assert _pass(db, orchestrator).activated == 1
    assert db.get_task(turn_id)["machine_id"] == "worker-a"


def test_withdraw_during_offline_wait_prevents_later_native_execution(tmp_path, monkeypatch):
    db, orchestrator = _setup(tmp_path, monkeypatch)
    _wire(orchestrator)
    db._conn().execute("UPDATE nodes SET status = 'offline' WHERE node_id = 'worker-a'")
    turn_id = str(_submit(orchestrator, operation_id="cancel-offline"))
    assert db.withdraw_turn(turn_id, actor="operator", void_lineage=True)
    assert db.get_task(turn_id)["status"] == "withdrawn"
    _register_carrier(db, "worker-a")
    assert _pass(db, orchestrator).activated == 0


def test_wrong_carrier_cannot_claim_a_pinned_session_turn(tmp_path, monkeypatch):
    db, orchestrator = _setup(tmp_path, monkeypatch)
    _register_carrier(db, "other-node")
    turn_id = str(_submit(orchestrator, operation_id="wrong-claim"))
    assert _pass(db, orchestrator).activated == 1
    with pytest.raises(tq.TurnQueueError):
        db.claim_turn(turn_id, "other-node", "worker_daemon", "inc-1")
    assert db.get_task(turn_id)["claimed_by"] is None


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


def test_local_pool_never_claims_pinned_task(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    session = _session("sess-pin", machine_id="node-a")
    db.upsert_session(session)
    # [A82 Stage 8a] A pinned protocol-0 row is now a CONTROL row (session
    # execution rows are refused at insert); the affinity scan is the same.
    db.enqueue_task(
        task_id="t-pinned",
        session_id=session.session_id,
        machine_id="node-a",
        backend="claude",
        action="close_session",
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
