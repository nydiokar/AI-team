"""Tests for T4 — Reclaim in-flight tasks dropped by a worker restart.

All tests honour the test cost guard: no paid Claude/Codex CLI invoked.
"""

import json
import uuid

import pytest

from src.control.db import MeshDB
from src.control.node_registry import NodeCapabilities, NodeInfo, NodeRegistry
from src.core.telemetry import build_event, utc_now


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


def test_no_stale_claims_when_worker_reregisters_same_process_incarnation(tmp_path):
    """Controller restart forces re-register, but same worker process is not stale."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)

    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online", incarnation_id="proc-1")
    db.claim_task(tid, "node-a")

    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online", incarnation_id="proc-1")

    stale = db.list_stale_claims(lease_sec=0)
    assert stale == []


def test_stale_claims_when_worker_reregisters_new_process_incarnation(tmp_path):
    """Actual worker process restart changes incarnation and makes old claim stale."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    tid = _task_id()
    _enqueue_test_task(db, tid)

    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online", incarnation_id="proc-1")
    db.claim_task(tid, "node-a")

    db.upsert_node("node-a", "127.0.0.1", 9001, ["claude"], 2, status="online", incarnation_id="proc-2")

    stale = db.list_stale_claims(lease_sec=0)
    assert [row["id"] for row in stale] == [tid]
    assert stale[0]["_stale_reason"] == "incarnation_mismatch"


def test_registry_reregister_same_incarnation_does_not_release_claim(tmp_path):
    from config import config as cfg
    import src.control.db as db_mod

    cfg.mesh.db_path = str(tmp_path / "mesh.db")
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old:
        old.close()

    try:
        registry = NodeRegistry()
        registry.register(NodeInfo(
            node_id="node-a",
            tailscale_ip="127.0.0.1",
            api_port=9001,
            capabilities=NodeCapabilities(backends=["claude"], max_concurrent=2),
            incarnation_id="proc-1",
        ))
        db = db_mod.get_db()
        tid = _task_id()
        _enqueue_test_task(db, tid)
        assert db.claim_task(tid, "node-a")

        registry.register(NodeInfo(
            node_id="node-a",
            tailscale_ip="127.0.0.1",
            api_port=9001,
            capabilities=NodeCapabilities(backends=["claude"], max_concurrent=2),
            incarnation_id="proc-1",
        ))

        row = db.get_task(tid)
        assert row["status"] == "claimed"
        assert row["claimed_by"] == "node-a"
        assert row["claimer_incarnation"] == "proc-1"
    finally:
        db_mod._db_instance = None
        if old:
            old.close()
        db_mod._db_instance = old


def test_registry_reregister_new_incarnation_releases_claim(tmp_path):
    from config import config as cfg
    import src.control.db as db_mod

    cfg.mesh.db_path = str(tmp_path / "mesh.db")
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old:
        old.close()

    try:
        registry = NodeRegistry()
        registry.register(NodeInfo(
            node_id="node-a",
            tailscale_ip="127.0.0.1",
            api_port=9001,
            capabilities=NodeCapabilities(backends=["claude"], max_concurrent=2),
            incarnation_id="proc-1",
        ))
        db = db_mod.get_db()
        tid = _task_id()
        _enqueue_test_task(db, tid)
        assert db.claim_task(tid, "node-a")

        registry.register(NodeInfo(
            node_id="node-a",
            tailscale_ip="127.0.0.1",
            api_port=9001,
            capabilities=NodeCapabilities(backends=["claude"], max_concurrent=2),
            incarnation_id="proc-2",
        ))

        row = db.get_task(tid)
        assert row["status"] == "pending"
        assert row["claimed_by"] is None
        assert row["claimer_incarnation"] is None
    finally:
        db_mod._db_instance = None
        if old:
            old.close()
        db_mod._db_instance = old


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


def test_submit_result_reconciles_failed_telemetry_turn(tmp_path):
    """A terminal failed mesh task must not leave its telemetry turn running."""
    from config import config as cfg
    cfg.mesh.db_path = str(tmp_path / "mesh_api_reconcile.db")
    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old is not None:
        old.close()

    try:
        from src.control.task_server import ExecutionResultPayload, submit_result
        from src.control.telemetry_store import TelemetryStore

        db = db_mod.get_db()
        assert db is not None
        store = TelemetryStore(db)
        tid = _task_id()
        invocation_id = "inv_reconcile_failed"
        _enqueue_test_task(db, tid)
        db.claim_task(tid, "worker-a")

        start = utc_now()
        common = {
            "turn_id": tid,
            "session_id": "session_reconcile_failed",
            "node_id": "worker-a",
            "emitter_process_instance_id": "worker_proc",
            "source": "worker",
            "backend": "codex",
            "invocation_id": invocation_id,
        }
        store.insert_events(
            [
                build_event(
                    "invocation.created",
                    event_time=start,
                    observed_time=start,
                    attributes={"attempt": 1, "spawn_reason": "initial"},
                    **common,
                ),
                build_event(
                    "process.spawned",
                    event_time=start,
                    observed_time=start,
                    pid=123,
                    attributes={
                        "process_instance_id": "proc_reconcile_failed",
                        "process_role": "agent",
                        "executable_name": "codex",
                    },
                    **common,
                ),
                build_event(
                    "invocation.completed",
                    event_time=start,
                    observed_time=start,
                    attributes={"status": "failed", "exit_code": 1},
                    **common,
                ),
            ]
        )
        assert store.get_turn(tid)["final_status"] == "running"

        resp = submit_result(tid, ExecutionResultPayload(
            node_id="worker-a",
            success=False,
            errors=["codex exited with code 1"],
            return_code=1,
            telemetry_invocation_id=invocation_id,
        ))

        assert resp["status"] == "accepted"
        turn = store.get_turn(tid)
        assert turn["final_status"] == "failed"
        assert turn["final_exit_code"] == 1
        assert "telemetry.reconciled" in [
            event["event_name"] for event in store.list_events(tid)
        ]

    finally:
        db_mod._db_instance = None
        if old is not None:
            old.close()
        db_mod._db_instance = old


# ---------------------------------------------------------------------------
# F2 — the gateway's OWN local node must stay online so its in-process
# self-claims (the double-execution lock) are not reaped as node_offline.
# ---------------------------------------------------------------------------


def test_local_node_registration_keeps_self_claim_live(tmp_path, monkeypatch):
    """A locally-run task self-claimed under the gateway host must NOT be released
    by the reaper while the gateway is alive. ``_register_local_node`` keeps the
    host node online (no live_state), so ``list_stale_claims`` leaves the self-claim
    be even past the lease. Regression guard for F2 (spurious node_offline release
    of a live local task exceeding claim_lease_sec)."""
    import socket
    import src.control.db as db_mod
    import src.control.node_registry as nr_mod
    import src.control.task_server as ts

    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "_db_instance", db)
    monkeypatch.setattr(nr_mod, "_registry", NodeRegistry(heartbeat_timeout_sec=90))

    host = socket.gethostname()
    ts._register_local_node()
    node = db.get_node(host)
    assert node is not None and node["status"] == "online"

    tid = _task_id()
    _enqueue_test_task(db, tid, machine_id=host)
    assert db.claim_task(tid, host) is True

    # lease_sec=0 => stale by time; but the host node is ONLINE without live_state,
    # so the self-claim must NOT be flagged (the F2 fix).
    stale = db.list_stale_claims(lease_sec=0)
    assert all(s["id"] != tid for s in stale), (
        "live gateway self-claim was reaped — F2 regression"
    )


def test_local_node_reregister_reaps_orphaned_self_claim(tmp_path, monkeypatch):
    """A gateway RESTART must reap self-claims orphaned by the dead process. Each
    ``_register_local_node`` mints a fresh incarnation, so ``register()``'s fast
    path releases the previous process's self-claims (incarnation change) — the
    double-execution lock is handed off cleanly instead of stranding the row."""
    import socket
    import src.control.db as db_mod
    import src.control.node_registry as nr_mod
    import src.control.task_server as ts

    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "_db_instance", db)
    monkeypatch.setattr(nr_mod, "_registry", NodeRegistry(heartbeat_timeout_sec=90))

    host = socket.gethostname()
    ts._register_local_node()
    inc1 = db.get_node(host)["incarnation_id"]

    tid = _task_id()
    _enqueue_test_task(db, tid, machine_id=host)
    assert db.claim_task(tid, host) is True

    # Simulate a gateway restart: a new process re-registers the same host node.
    ts._register_local_node()
    inc2 = db.get_node(host)["incarnation_id"]
    assert inc1 != inc2, "restart must mint a fresh incarnation"

    # register()'s fast path already released the orphaned self-claim to pending.
    row = db.get_task(tid)
    assert row["status"] == "pending" and row["claimed_by"] is None
