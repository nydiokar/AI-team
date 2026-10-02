"""A82 Stage 7 — rollout rehearsal on fixtures (ROLL01-05, packet §5/§11, design §10).

The enrollment SERVICE (design §10 step 6/8): an operator action inside the
gateway process that enrolls only a quiescent session with no legacy queued /
pending / claimed / in-memory work and a managed-capable carrier, behind the
default-OFF ``TURN_QUEUE_ENROLLMENT_ENABLED`` flag, using an admission exclusion
that a racing legacy arrival cannot slip past; and the drain/rollback exit that
removes enrollment only when no waiting/active/recovery obligation remains.
The flag gates NEW enrollment only — accepted managed work keeps flowing.

Real bound orchestrator (bare instance) + real temp ``MeshDB`` + real task-server
app; no backend, network or CLI (producer-1 autouse spawn guard).
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import src.control.db as db_mod
from src.control import turn_queue as tq
from src.control.db import MeshDB
from src.core.interfaces import SessionStatus
from src.orchestrator import TaskOrchestrator
from src.core.session_task_queue import SessionTaskQueue
from tests.test_turn_queue_4b import _pass, _run, _wire  # noqa: F401
from tests.test_turn_queue_producer1 import (  # noqa: F401
    _flags, _managed_rows, _no_cli_spawn, _register_carrier, _sess, _setup, _submit,
)

FLAG = "TURN_QUEUE_ENROLLMENT_ENABLED"


@pytest.fixture()
def flag_on(monkeypatch: Any) -> None:
    monkeypatch.setenv(FLAG, "1")


def _enroll(o: Any, sid: str = "sess-1") -> bool:
    return bool(asyncio.run(o.enroll_session_turn_queue(sid)))


def _unenroll(o: Any, sid: str = "sess-1") -> bool:
    return bool(asyncio.run(o.unenroll_session_turn_queue(sid)))


def _refused(fn: Any, *a: Any) -> tq.TurnQueueError:
    with pytest.raises(tq.TurnQueueError) as exc:
        fn(*a)
    return exc.value


def _enrolled(db: MeshDB, sid: str = "sess-1") -> bool:
    return bool(db.get_session(sid)["turn_queue_enrolled"])


# --------------------------------------------------------------------------- #
# ROLL01 — flag OFF: no new enrollment; accepted managed work keeps flowing,
# across a gateway restart.
# --------------------------------------------------------------------------- #
def test_ROLL01_flag_off_refuses_new_enrollment(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv(FLAG, raising=False)
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    err = _refused(_enroll, o)
    assert err.code == "enrollment_disabled" and err.status_code == 409
    assert not _enrolled(db)
    # Mesh-off / legacy behaviour unchanged: the session still takes the legacy path.
    tid = _submit(o, operation_id="legacy-1")
    assert type(tid) is str and o.task_queue.qsize() == 1 and _managed_rows(db) == []


def test_ROLL01b_flag_off_with_accepted_rows_survives_restart(tmp_path: Any, monkeypatch: Any) -> None:
    """Accepted managed rows + flag OFF + gateway restart (new process state:
    new MeshDB instance on the same file, new orchestrator): consumption,
    admission for the still-enrolled session and recovery keep working."""
    monkeypatch.delenv(FLAG, raising=False)
    db, o = _setup(tmp_path, monkeypatch)  # enrolled before the flag went off
    t1 = str(_submit(o, operation_id="a"))
    t2 = str(_submit(o, operation_id="b"))
    t3 = str(_submit(o, operation_id="c"))
    # ---- restart ----
    db2 = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db2)
    o2 = TaskOrchestrator.__new__(TaskOrchestrator)
    from src.services.session_store import SessionStore

    o2.session_store = SessionStore()
    o2.task_queue = SessionTaskQueue(50, lambda _t: "")
    o2.active_tasks = {}
    o2._compact_injected_ids = set()
    o2.events = []
    o2._emit_event = lambda name, task, data=None: o2.events.append(name)
    o2._emit_turn_telemetry = lambda name, task, data=None, **k: o2.events.append(name)
    _wire(o2)
    assert db2.any_session_enrolled() is True
    assert _pass(db2, o2).activated == 1
    tok = _run(db2, t1)
    db2.complete_turn(t1, tok, {"success": True, "output": "ok"})
    assert _pass(db2, o2).activated == 1 and db2.get_task(t2)["status"] == "pending"
    # A new turn for the enrolled session is still admitted (the flag gates
    # enrollment, never admission/consumption).
    t4 = _submit(o2, operation_id="d")
    assert isinstance(t4, tq.TurnAdmission)
    # Recovery stays available: an uncertain attempt can be entered + resolved.
    tok2 = _run(db2, t2)
    assert db2.enter_recovery(t2, tok2, reason="deadline")
    res = db2.resolve_recovery(t2, tok2, {"source": "operator", "task_id": t2, "quiescent": True,
                                          "terminal": True, "terminal_status": "cancelled",
                                          "acknowledged_uncertain": True}, resolved_status="cancelled")
    assert res.resolved_status == "cancelled"
    assert _pass(db2, o2).activated == 1 and db2.get_task(t3)["status"] == "pending"


# --------------------------------------------------------------------------- #
# ROLL02 — mesh off / no canonical DB: enrollment refused, legacy unchanged
# --------------------------------------------------------------------------- #
def test_ROLL02_no_canonical_db_refuses_enrollment(tmp_path: Any, monkeypatch: Any, flag_on: None) -> None:
    _db, o = _setup(tmp_path, monkeypatch, enroll=False)
    monkeypatch.setattr(db_mod, "get_db", lambda: None)
    err = _refused(_enroll, o)
    assert err.status_code == 503
    tid = _submit(o)
    assert type(tid) is str and o.task_queue.qsize() == 1


# --------------------------------------------------------------------------- #
# ROLL03 — eligibility: capability, liveness, quiescence, legacy work
# --------------------------------------------------------------------------- #
def test_ROLL03_enrolls_a_quiescent_capable_session_idempotently(
        tmp_path: Any, monkeypatch: Any, flag_on: None) -> None:
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    assert _enroll(o) is True and _enrolled(db)
    assert db.any_session_enrolled() is True
    assert _enroll(o) is False  # already enrolled: idempotent, no error
    tid = _submit(o, operation_id="m-1")
    assert isinstance(tid, tq.TurnAdmission)  # producers now take protocol 1


def test_ROLL03b_old_worker_capabilities_refused(tmp_path: Any, monkeypatch: Any, flag_on: None) -> None:
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    _register_carrier(db, "worker-a", managed=())  # old worker: no managed backends
    err = _refused(_enroll, o)
    assert err.code == "capability_missing" and err.status_code == 409
    assert not _enrolled(db)


def test_ROLL03c_offline_carrier_refused(tmp_path: Any, monkeypatch: Any, flag_on: None) -> None:
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    db._conn().execute("UPDATE nodes SET status = 'offline' WHERE node_id = 'worker-a'")
    err = _refused(_enroll, o)
    assert err.code == "carrier_offline" and not _enrolled(db)


@pytest.mark.parametrize("status", [SessionStatus.BUSY, SessionStatus.PINNED_NODE_OFFLINE])
def test_ROLL03d_busy_session_refused(tmp_path: Any, monkeypatch: Any, flag_on: None,
                                      status: SessionStatus) -> None:
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    db._conn().execute("UPDATE sessions SET status = ? WHERE session_id = 'sess-1'", (status.value,))
    err = _refused(_enroll, o)
    assert err.code == "session_not_quiescent" and not _enrolled(db)


def test_ROLL03e_closed_session_refused(tmp_path: Any, monkeypatch: Any, flag_on: None) -> None:
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    db._conn().execute("UPDATE sessions SET status = ? WHERE session_id = 'sess-1'", (SessionStatus.CLOSED.value,))
    err = _refused(_enroll, o)
    assert err.code == "session_closed" and not _enrolled(db)


def test_ROLL03f_legacy_in_memory_work_refused(tmp_path: Any, monkeypatch: Any, flag_on: None) -> None:
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    tid = _submit(o, operation_id="legacy-1")  # legacy queued in the gateway
    assert type(tid) is str and o.task_queue.qsize() == 1
    err = _refused(_enroll, o)
    assert err.code == "legacy_work_in_flight" and not _enrolled(db)


@pytest.mark.parametrize("status", ["pending", "claimed"])
def test_ROLL03g_durable_legacy_row_refused(tmp_path: Any, monkeypatch: Any, flag_on: None,
                                            status: str) -> None:
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    db.enqueue_task("legacy-row", "sess-1", "worker-a", "claude", "resume_session", {"prompt": "x"})
    if status == "claimed":
        assert db.claim_task("legacy-row", "worker-a")
    err = _refused(_enroll, o)
    assert err.code == "legacy_work_in_flight" and not _enrolled(db)
    # A finished legacy row is history, not work: enrollment proceeds.
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET status = 'completed' WHERE id = 'legacy-row'")
    assert _enroll(o) is True


# --------------------------------------------------------------------------- #
# ROLL04 — admission exclusion closes the cutover race (both interleavings)
# --------------------------------------------------------------------------- #
def test_ROLL04_legacy_arrival_during_enrollment_is_refused(
        tmp_path: Any, monkeypatch: Any, flag_on: None) -> None:
    """Enrollment is committing (its DB txn in a worker thread); a legacy
    submit for the same session arrives on the gateway loop. It must not be
    put on the legacy queue (it would execute beside the managed queue)."""
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    gate = threading.Event()
    entered = threading.Event()
    real = db.enroll_session_checked

    def _slow(sid: str) -> bool:
        entered.set()
        assert gate.wait(5)
        return real(sid)

    monkeypatch.setattr(db, "enroll_session_checked", _slow)

    async def scenario() -> Dict[str, Any]:
        enroll = asyncio.create_task(o.enroll_session_turn_queue("sess-1"))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        out: Dict[str, Any] = {}
        try:
            out["legacy"] = await o.submit_instruction(
                description="racing legacy", session_id="sess-1", cwd="/tmp/repo",
                source="web_session")
        except tq.TurnQueueError as err:
            out["legacy_err"] = err
        gate.set()
        out["enrolled"] = await enroll
        return out

    out = asyncio.run(scenario())
    assert out["enrolled"] is True and _enrolled(db)
    assert "legacy" not in out, "a legacy turn was queued beside the enrollment"
    assert out["legacy_err"].code == "enrollment_in_progress" and out["legacy_err"].status_code == 409
    assert o.task_queue.qsize() == 0 and not o.active_tasks


def test_ROLL04b_enrollment_during_legacy_admission_is_refused(
        tmp_path: Any, monkeypatch: Any, flag_on: None) -> None:
    """A legacy admission is between its marker decision and the legacy put
    (suspended in the offloaded marker read); enrollment must refuse rather
    than commit underneath it."""
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    release = asyncio.Event()
    reading = asyncio.Event()

    async def _slow_marker(_sid: str) -> bool:
        reading.set()
        await release.wait()
        return False

    monkeypatch.setattr(o, "_session_turn_queue_enrolled", _slow_marker)

    async def scenario() -> Dict[str, Any]:
        legacy = asyncio.create_task(o.submit_instruction(
            description="legacy first", session_id="sess-1", cwd="/tmp/repo", source="web_session"))
        await reading.wait()
        out: Dict[str, Any] = {}
        try:
            out["enrolled"] = await o.enroll_session_turn_queue("sess-1")
        except tq.TurnQueueError as err:
            out["enroll_err"] = err
        release.set()
        out["legacy"] = await legacy
        return out

    out = asyncio.run(scenario())
    assert "enrolled" not in out and out["enroll_err"].code == "legacy_work_in_flight"
    assert type(out["legacy"]) is str and o.task_queue.qsize() == 1
    assert not _enrolled(db)


# --------------------------------------------------------------------------- #
# ROLL05 — drain / remove enrollment only without obligations; no downgrade
# path hands managed rows to a legacy poller.
# --------------------------------------------------------------------------- #
def test_ROLL05_unenroll_only_after_drain(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv(FLAG, raising=False)  # rollback works with the flag OFF
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = str(_submit(o, operation_id="a"))
    err = _refused(_unenroll, o)  # queued
    assert err.code == "managed_obligation_remaining" and _enrolled(db)
    _pass(db, o)
    err = _refused(_unenroll, o)  # pending
    assert err.code == "managed_obligation_remaining"
    tok = _run(db, t1)
    assert _refused(_unenroll, o).code == "managed_obligation_remaining"  # running
    assert db.enter_recovery(t1, tok, reason="deadline")
    assert _refused(_unenroll, o).code == "managed_obligation_remaining"  # recovery_required
    db.resolve_recovery(t1, tok, {"source": "operator", "task_id": t1, "quiescent": True,
                                  "terminal": True, "terminal_status": "cancelled",
                                  "acknowledged_uncertain": True}, resolved_status="cancelled")
    assert _unenroll(o) is True and not _enrolled(db)
    assert _unenroll(o) is False  # idempotent
    # Back on the legacy path, exactly as before enrollment.
    tid = _submit(o, operation_id="after")
    assert type(tid) is str and o.task_queue.qsize() == 1


def test_ROLL05b_legacy_poller_never_receives_managed_rows(tmp_path: Any, monkeypatch: Any) -> None:
    """Mixed version / rollback shape: an OLD worker (no managed capability)
    polls the legacy routes. Managed rows are never handed to it (no double
    run), stay durable (nothing lost) and a new managed admission whose
    carrier lost its managed capability is refused (503), not accepted."""
    import src.control.task_server as ts_mod
    import src.control.node_registry as nr_mod

    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = str(_submit(o, operation_id="a"))
    _pass(db, o)
    assert db.get_task(t1)["status"] == "pending"
    monkeypatch.setattr(ts_mod, "get_db", lambda: db)
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    monkeypatch.setattr(ts_mod, "_worker_token", lambda: "tok")
    client = TestClient(ts_mod.app)
    h = {"Authorization": "Bearer tok"}
    r = client.post("/nodes/register", json={
        "node_id": "worker-a", "tailscale_ip": "127.0.0.1", "api_port": 0,
        "incarnation_id": "old-binary", "capabilities": {"backends": ["claude"]},
    }, headers=h)
    assert r.status_code == 200, r.text
    legacy: List[Dict[str, Any]] = client.get(
        "/tasks/pending", params={"node_id": "worker-a"}, headers=h).json()
    rows = legacy if isinstance(legacy, list) else legacy.get("tasks", [])
    assert all(row.get("id") != t1 for row in rows)
    assert client.get("/tasks/pending-managed", params={"node_id": "worker-a"},
                      headers=h).json() in ([], {"tasks": []})
    assert client.post(f"/tasks/{t1}/claim", json={"node_id": "worker-a"},
                       headers=h).status_code >= 400
    row = db.get_task(t1)
    assert row["status"] == "pending" and row["queue_protocol"] == 1  # durable, not run
    err = _refused(_submit, o)
    assert err.status_code == 503 and err.code == "carrier_unavailable"
