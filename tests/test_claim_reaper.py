"""Tests for T4 — Reclaim in-flight tasks dropped by a worker restart.

All tests honour the test cost guard: no paid Claude/Codex CLI invoked.
"""

import json
import uuid

import pytest

from src.control.db import MeshDB


def _task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


def _enqueue_test_task(db: MeshDB, task_id: str, machine_id: str = "test-node") -> None:
    # Use NULL session_id to avoid foreign key constraint requiring a sessions row
    db.enqueue_task(
        task_id=task_id,
        session_id=None,
        machine_id=machine_id,
        backend="claude",
        action="run_oneoff",
        payload={"prompt": "hello", "task_id": task_id},
    )


# ---------------------------------------------------------------------------
# DB layer — release_task
# ---------------------------------------------------------------------------


def test_release_task_releases_claimed(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    assert db.claim_task(tid, "node-a")
    assert db.release_task(tid, "node-a") is True
    row = db.get_task(tid)
    assert row["status"] == "pending"
    assert row["claimed_by"] is None
    assert row["claimed_at"] is None


def test_release_task_rejects_wrong_node(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    assert db.claim_task(tid, "node-a")
    assert db.release_task(tid, "node-b") is False
    row = db.get_task(tid)
    assert row["status"] == "claimed"  # still claimed
    assert row["claimed_by"] == "node-a"


def test_release_task_noop_for_pending(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    assert db.release_task(tid, "node-a") is False  # not claimed


def test_release_task_noop_for_completed(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.claim_task(tid, "node-a")
    db.complete_task(tid, {"success": True, "output": "ok"})
    assert db.release_task(tid, "node-a") is False  # already completed


# ---------------------------------------------------------------------------
# DB layer — list_stale_claims
# ---------------------------------------------------------------------------


def test_no_stale_claims_when_node_online_without_live_state(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.claim_task(tid, "node-a")
    # Register the node as online
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online")
    stale = db.list_stale_claims(lease_sec=0)  # 0 = everything is stale by time
    assert len(stale) == 0  # old workers without live_state preserve compatibility


def test_stale_claims_when_online_live_state_missing_task(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online")
    db.claim_task(tid, "node-a")
    db.heartbeat_node("node-a", live_state=json.dumps({
        "v": 1,
        "active_tasks": [],
        "slots_used": 0,
        "slots_total": 2,
    }))

    stale = db.list_stale_claims(lease_sec=0)
    assert len(stale) == 1
    assert stale[0]["id"] == tid
    assert stale[0]["_stale_reason"] == "missing_from_live_state"


def test_stale_claims_when_online_active_task_exceeds_runtime(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online")
    db.claim_task(tid, "node-a")
    db.heartbeat_node("node-a", live_state=json.dumps({
        "v": 1,
        "active_tasks": [tid],
        "active_task_details": {
            tid: {
                "task_id": tid,
                "phase": "running",
                "started_at": "2000-01-01T00:00:00+00:00",
            }
        },
        "slots_used": 1,
        "slots_total": 2,
    }))

    stale = db.list_stale_claims(lease_sec=0, active_task_max_runtime_sec=60)
    assert len(stale) == 1
    assert stale[0]["id"] == tid
    assert stale[0]["_stale_reason"] == "active_task_over_max_runtime"


def test_stale_claims_when_node_offline(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.claim_task(tid, "node-a")
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="offline")
    stale = db.list_stale_claims(lease_sec=0)
    assert len(stale) >= 1
    assert stale[0]["id"] == tid


def test_stale_claims_when_node_gone(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.claim_task(tid, "node-gone")
    stale = db.list_stale_claims(lease_sec=0)
    assert len(stale) >= 1
    assert stale[0]["id"] == tid


def test_stale_claims_respects_lease(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.claim_task(tid, "node-a")
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="offline")
    # Use a large lease that won't be exceeded
    stale = db.list_stale_claims(lease_sec=999999)
    assert len(stale) == 0  # claim is too recent


def test_release_and_reclaim(tmp_path):
    """A released task can be re-claimed by another worker."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.claim_task(tid, "node-a")
    db.release_task(tid, "node-a")
    assert db.claim_task(tid, "node-b") is True
    row = db.get_task(tid)
    assert row["claimed_by"] == "node-b"


# ---------------------------------------------------------------------------
# Idempotency — submit_result rejects late results
# ---------------------------------------------------------------------------


def test_late_result_after_reclaim_is_safe(tmp_path):
    """A late POST /result from a superseded worker is safe (idempotency)."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)

    # Worker A claims, gets released, worker B claims and completes
    db.claim_task(tid, "node-a")
    db.release_task(tid, "node-a")
    db.claim_task(tid, "node-b")
    db.complete_task(tid, {"success": True, "output": "done by B"})

    # Now worker A tries to submit late — the task is already terminal
    # The server should detect this and return "accepted (stale)" without error
    # (tested via server endpoint, but we verify the DB state is unchanged)
    row = db.get_task(tid)
    assert row["status"] == "completed"
    assert row["claimed_by"] == "node-b"  # B's claim won


# ---------------------------------------------------------------------------
# Incarnation mismatch — restart-in-place orphan detection
# ---------------------------------------------------------------------------


def test_stale_claims_when_node_restarted_in_place(tmp_path):
    """Claim from dead process is stale even though the same node_id is online."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)

    # Simulate original process: register node, then claim
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online")
    db.claim_task(tid, "node-a")  # claimer_incarnation = original incarnation_id

    # Simulate PM2 restart: new upsert mints a fresh incarnation_id
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online")

    stale = db.list_stale_claims(lease_sec=0)
    assert len(stale) == 1
    assert stale[0]["id"] == tid


def test_no_stale_claims_when_same_incarnation_without_live_state(tmp_path):
    """Claim from the current process incarnation is not stale."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)

    # Register then claim — claimer_incarnation matches the current incarnation_id
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online")
    db.claim_task(tid, "node-a")

    stale = db.list_stale_claims(lease_sec=0)
    assert len(stale) == 0


def test_stale_claims_incarnation_mismatch_respects_lease(tmp_path):
    """Incarnation-mismatch claims still require the lease to expire."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online")
    db.claim_task(tid, "node-a")
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online")

    stale = db.list_stale_claims(lease_sec=999999)
    assert len(stale) == 0  # claim is too recent even though incarnation changed


# ---------------------------------------------------------------------------
# release_node_claims — startup sweep
# ---------------------------------------------------------------------------


def test_release_node_claims_returns_task_ids(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2)
    db.claim_task(tid, "node-a")

    released = db.release_node_claims("node-a")
    assert released == [tid]
    row = db.get_task(tid)
    assert row["status"] == "pending"
    assert row["claimed_by"] is None
    assert row["claimer_incarnation"] is None


def test_release_node_claims_noop_for_different_node(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)
    db.claim_task(tid, "node-a")

    released = db.release_node_claims("node-b")
    assert released == []
    row = db.get_task(tid)
    assert row["status"] == "claimed"  # untouched


def test_release_node_claims_noop_when_none_claimed(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    released = db.release_node_claims("node-a")
    assert released == []


# ---------------------------------------------------------------------------
# FastAPI endpoint functions — release endpoint + idempotency
# ---------------------------------------------------------------------------


def test_release_endpoint_function(tmp_path):
    from fastapi import HTTPException

    from config import config as cfg
    cfg.mesh.db_path = str(tmp_path / "mesh_api.db")
    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old is not None:
        old.close()

    try:
        from src.control.task_server import ClaimPayload, release_task

        # Enqueue + claim via DB directly
        db = db_mod.get_db()
        assert db is not None
        tid = _task_id()
        _enqueue_test_task(db, tid)
        db.claim_task(tid, "test-node")

        # Release via API
        resp = release_task(tid, ClaimPayload(node_id="test-node"))
        assert resp["status"] == "released"

        # Verify released in DB
        row = db.get_task(tid)
        assert row["status"] == "pending"

        # Wrong node cannot release
        db.claim_task(tid, "test-node")
        with pytest.raises(HTTPException) as exc:
            release_task(tid, ClaimPayload(node_id="wrong-node"))
        assert exc.value.status_code == 409

    finally:
        db_mod._db_instance = None
        if old is not None:
            old.close()
        db_mod._db_instance = old


def test_submit_result_idempotency_endpoint_function(tmp_path):
    """Late result after task is already terminal is accepted without error."""
    from config import config as cfg
    cfg.mesh.db_path = str(tmp_path / "mesh_api_idem.db")
    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old is not None:
        old.close()

    try:
        from src.control.task_server import ExecutionResultPayload, submit_result

        db = db_mod.get_db()
        assert db is not None
        tid = _task_id()
        _enqueue_test_task(db, tid)
        db.claim_task(tid, "worker-a")

        # First result goes through
        resp = submit_result(tid, ExecutionResultPayload(
            node_id="worker-a",
            success=True,
            output="first result",
        ))
        assert resp["status"] == "accepted"

        # Late result from same node (after completion) should not error
        resp = submit_result(tid, ExecutionResultPayload(
            node_id="worker-a",
            success=True,
            output="late duplicate",
        ))
        assert resp["status"] == "accepted (stale)"

        # DB state still shows first result
        row = db.get_task(tid)
        result = json.loads(row["result"])
        assert result["output"] == "first result"

    finally:
        db_mod._db_instance = None
        if old is not None:
            old.close()
        db_mod._db_instance = old
