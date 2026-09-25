"""A87 Stage 4a round-2 review probes (R1-R5 + test-honesty A/B), adopted as
permanent tests asserting the CORRECT behavior. Offline; autouse spawn guard
(re-used from the producer-1 suite)."""
import asyncio
import contextlib
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

import tests.test_turn_queue_producer1 as P
from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control import turn_scheduler as ts
from src.control.db import MeshDB
from tests.test_turn_queue_producer1 import (  # noqa: F401 — autouse fixtures
    _flags, _managed_rows, _no_cli_spawn, _setup, _submit,
)

PAST = (datetime.now(tz=timezone.utc) - timedelta(seconds=1)).isoformat()


def _links(db, case_id):
    return [l["entity_id"] for l in db.list_flow_links(flow_run_id=case_id)
            if l["entity_type"] == "task"]


def _pass(db, o):
    return asyncio.run(ts.run_scheduler_pass(
        db, o._prepare_managed_turn, allowance=ta.SharedWaitingAllowance(),
        recover_lineage=o._recover_managed_lineage))


# R1 — crash after commit: durable lineage-pending, never runs Case-less ------ #
def test_R1_crash_after_commit_is_recovered_never_runs_caseless(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    o._LINEAGE_REPLAY_WAIT_SEC = 0.2
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    real = o._record_flow_run_start
    o._record_flow_run_start = lambda t: (_ for _ in ()).throw(SystemExit("process died"))
    with pytest.raises(SystemExit):
        _submit(o, join_case_id=case_id, operation_id="op1", source="runtime")
    o._record_flow_run_start = real
    [row] = _managed_rows(db)
    assert row["lineage_state"] == "pending"
    assert json.loads(row["payload"])["metadata"]["__join_case_id"] == case_id
    # Not schedulable while lineage is pending (lease still held by the dead writer).
    assert db.select_eligible_turn_heads(25) == []
    assert _pass(db, o).activated == 0 and db.get_task(row["id"])["status"] == "queued"
    # A same-key replay never acks without lineage: the writer lease is live ⇒ 503.
    with pytest.raises(tq.BackingStoreError):
        _submit(o, join_case_id=case_id, operation_id="op1", source="runtime")
    # Lease expires ⇒ the scheduler recovers lineage from the persisted metadata,
    # then activates in the same pass.
    db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until=? WHERE id=?", (PAST, row["id"]))
    res = _pass(db, o)
    r = db.get_task(row["id"])
    assert res.lineage_recovered == 1 and res.activated == 1
    assert r["status"] == "pending" and r["flow_run_id"] == case_id
    assert r["lineage_state"] == "done" and _links(db, case_id) == [row["id"]]
    assert json.loads(r["payload"])["metadata"]["__case_id"] == case_id


def test_R1b_replay_after_lease_expiry_recovers_then_acks(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    real = o._record_flow_run_start
    o._record_flow_run_start = lambda t: (_ for _ in ()).throw(SystemExit("died"))
    with pytest.raises(SystemExit):
        _submit(o, join_case_id=case_id, operation_id="op1", source="runtime")
    o._record_flow_run_start = real
    [row] = _managed_rows(db)
    db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until=? WHERE id=?", (PAST, row["id"]))
    tid = _submit(o, join_case_id=case_id, operation_id="op1", source="runtime")
    assert tid == row["id"] and tid.idempotent_replay
    assert db.get_task(tid)["lineage_state"] == "done" and _links(db, case_id) == [tid]


def test_R1c_recovery_reuses_partial_birth_never_second_case(tmp_path, monkeypatch):
    """Writer died after BIRTHING the Case but before finalize: recovery reuses
    that flow, never creates a second one."""
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    real_fin = db.finalize_turn_lineage
    monkeypatch.setattr(db, "finalize_turn_lineage",
                        lambda *a, **k: (_ for _ in ()).throw(SystemExit("died")))
    with pytest.raises(SystemExit):
        _submit(o, operation_id="b1", source="runtime",
                parent_flow_run_id="parent-flow")  # lineage ⇒ birth
    monkeypatch.setattr(db, "finalize_turn_lineage", real_fin)
    [row] = _managed_rows(db)
    births = db._conn().execute(
        "SELECT flow_run_id FROM flow_runs WHERE task_id=?", (row["id"],)).fetchall()
    assert len(births) == 1
    db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until=? WHERE id=?", (PAST, row["id"]))
    _pass(db, o)
    r = db.get_task(row["id"])
    assert r["lineage_state"] == "done" and r["flow_run_id"] == births[0][0]
    assert len(db._conn().execute(
        "SELECT 1 FROM flow_runs WHERE task_id=?", (row["id"],)).fetchall()) == 1


# R2 — finalize after activation is impossible by construction --------------- #
def test_R2_stalled_writer_cannot_be_overtaken_by_activation(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    real_fin = db.finalize_turn_lineage
    out = {}

    def slow_finalize(tid, token, frid, meta):
        # Stalled past its lease: a scheduler pass recovers lineage + activates.
        db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until=? WHERE id=?", (PAST, tid))
        monkeypatch.setattr(db, "finalize_turn_lineage", real_fin)  # the recovery writer's own finalize
        out["pass"] = _pass(db, o)
        out["ret"] = real_fin(tid, token, frid, meta)
        return out["ret"]

    monkeypatch.setattr(db, "finalize_turn_lineage", slow_finalize)
    tid = _submit(o, join_case_id=case_id, operation_id="op2", source="runtime")
    r = db.get_task(tid)
    assert out["ret"] is False  # lost the lease: its write was refused …
    assert r["status"] == "pending" and r["flow_run_id"] == case_id  # … recovery did it first
    assert _links(db, case_id) == [tid]
    assert json.loads(r["payload"])["metadata"]["__case_id"] == case_id


def test_B_finalize_never_writes_into_a_non_queued_row(tmp_path, monkeypatch):
    """Test-honesty B: without `AND status='queued'` a finalize could rewrite an
    activated (frozen) payload."""
    db, o = _setup(tmp_path, monkeypatch)
    t = db.enqueue_turn(session_id="sess-1", body="x", operation_id="k", lineage_token="tok")
    db._conn().execute("UPDATE mesh_tasks SET status='pending', payload='{\"frozen\": 1}' WHERE id=?", (t,))
    assert db.finalize_turn_lineage(t, "tok", "case-x", {"a": 1}) is False
    r = db.get_task(t)
    assert r["payload"] == '{"frozen": 1}' and r["flow_run_id"] is None
    assert db.activate_turn(t) is False  # and a pending-lineage row never activates


# R3 / A — enrollment presence vs a concurrent refresh ------------------------ #
def test_R3_refresh_during_enroll_never_lowers_presence(tmp_path):
    from src.core.interfaces import Session, SessionStatus

    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(Session(session_id="x", backend="claude", repo_path="/tmp/r",
                              status=SessionStatus.IDLE, created_at=P.NOW, updated_at=P.NOW))
    assert db.any_session_enrolled() is False
    real_write = db._write
    seen = {}

    @contextlib.contextmanager
    def racing_write():
        # refresh runs AFTER the pre-commit raise and BEFORE the commit
        th = threading.Thread(target=db.refresh_enrollment_presence)
        th.start()
        th.join()
        seen["inside_window"] = db._any_enrolled  # a legacy admission would read this
        with real_write() as conn:
            yield conn

    db._write = racing_write
    db.enroll_session("x")
    db._write = real_write
    assert seen["inside_window"] is True, "flag lowered while the enrollment was in flight"
    assert db.is_session_enrolled("x") is True and db.any_session_enrolled() is True


def test_R3b_refresh_that_read_before_commit_cannot_lower_after(tmp_path):
    from src.core.interfaces import Session, SessionStatus

    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(Session(session_id="x", backend="claude", repo_path="/tmp/r",
                              status=SessionStatus.IDLE, created_at=P.NOW, updated_at=P.NOW))
    real_conn = db._conn
    gate = {"n": 0}

    class _Slow:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a):
            cur = self._c.execute(sql, *a)
            if "turn_queue_enrolled = 1 LIMIT 1" in sql and gate["n"] == 0:
                gate["n"] = 1
                db.enroll_session("x")  # an enrollment commits AFTER our read
            return cur

        def __getattr__(self, n):
            return getattr(self._c, n)

    db._conn = lambda: _Slow(real_conn())
    db.refresh_enrollment_presence()
    db._conn = real_conn
    assert db.any_session_enrolled() is True


# R4 — carried (cross-process enrollment); documented, not fixed here --------- #
def test_R4_documented_precondition_enroll_in_gateway_process(tmp_path, monkeypatch):
    """Residual M3 (Stage-7 precondition): a flag held by ANOTHER process's
    MeshDB is not visible until this process refreshes. Pins the behaviour the
    §15 residual documents; the gateway's own refresh then sees it."""
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    other = MeshDB(str(tmp_path / "mesh.db"))
    other.enroll_session("sess-1")
    assert db.any_session_enrolled() is False
    assert db.refresh_enrollment_presence() is True


# R5 — dead carrier: refused, backs off visibly, pending never wedges -------- #
def test_R5_offline_carrier_refused_at_admission(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    db.mark_node_offline("worker-a")
    with pytest.raises(tq.CarrierUnavailableError):
        _submit(o, operation_id="z")
    assert _managed_rows(db) == []


def test_R5b_stale_heartbeat_counts_as_dead(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
    db._conn().execute("UPDATE nodes SET last_heartbeat=? WHERE node_id='worker-a'", (old,))
    assert db.node_managed_backends("worker-a") == []


def test_R5c_carrier_dies_before_activation_backs_off_visibly(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    tid = _submit(o, operation_id="z")
    db.mark_node_offline("worker-a")
    res = _pass(db, o)
    r = db.get_task(tid)
    assert res.activated == 0 and r["status"] == "queued" and r["blocked_until"]
    assert "CarrierUnavailableError" in r["blocked_reason"] and "worker-a" in r["blocked_reason"]


def test_R5d_pending_on_carrier_that_went_offline_is_requeued_visibly(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    tid = _submit(o, operation_id="z")
    assert _pass(db, o).activated == 1 and db.get_task(tid)["status"] == "pending"
    db.mark_node_offline("worker-a")
    res = _pass(db, o)
    r = db.get_task(tid)
    assert res.carrier_requeued == 1 and r["status"] == "queued"
    assert r["blocked_reason"] == "carrier_offline: worker-a" and r["blocked_until"]
    # The slot is free again and the carrier's return re-activates it.
    assert db.get_active_turn("sess-1") is None
    P._register_carrier(db, "worker-a")
    db._conn().execute("UPDATE mesh_tasks SET blocked_until=? WHERE id=?", (PAST, tid))
    assert _pass(db, o).activated == 1
    assert json.loads(db.get_task(tid)["payload"])["task"]["title"]  # spec survived requeue
    tok = db.claim_turn(tid, "worker-a", "worker_daemon", "inc-1")
    assert tok and db.get_task(tid)["blocked_reason"] is None
