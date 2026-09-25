"""A82 Stage 4a — managed admission service (design §4, §8).

Real temp file-backed MeshDB; no backend, no network, no CLI. An autouse guard
makes every Claude SDK spawn path raise so no test (or mutant) can start a CLI.

ADM01 idempotency replay / conflict       ADM06 commit-before-acknowledge
ADM02 per-session cap 20                   ADM07 durable enrollment marker
ADM03 fleet cap                            ADM08 bounded lock deadline
ADM04 shared legacy+managed allowance      ADM09 admission concurrency permit
ADM05 stored-intent byte caps              ADM10 edit byte re-accounting
"""
import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from src.core.session_task_queue import SessionTaskQueue

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _no_cli_spawn(monkeypatch):
    """Cost guard: any attempt to start a real CLI fails loudly."""
    from src.backends import claude_driver

    def _boom(*_a, **_k):
        raise AssertionError("real CLI spawn attempted in an offline test")

    monkeypatch.setattr(claude_driver._SDKSession, "start", _boom, raising=False)
    try:
        import claude_agent_sdk

        monkeypatch.setattr(claude_agent_sdk.ClaudeSDKClient, "connect", _boom, raising=False)
    except Exception:  # noqa: BLE001
        pass
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _session(db: MeshDB, sid: str = "sess-1", *, enroll: bool = True,
             status: SessionStatus = SessionStatus.IDLE) -> None:
    db.upsert_session(Session(
        session_id=sid, backend="claude", repo_path="/tmp/repo", status=status,
        created_at=NOW, updated_at=NOW, machine_id="worker-a",
    ))
    if enroll:
        db.enroll_session(sid)


def _req(sid: str = "sess-1", body: str = "hello", op: str = "op-1", **kw) -> ta.AdmissionRequest:
    fields = dict(
        session_id=sid, body=body, payload={"prompt": body},
        turn_source="human", operation_id=op, idempotency_scope=f"operator:{sid}:instruction",
        admission_hash=f"h:{sid}:{body}",
    )
    fields.update(kw)
    return ta.AdmissionRequest(**fields)


def _admit(db, req, cap=50, allowance=None):
    return ta.admit_turn(db, req, fleet_cap=cap, allowance=allowance or ta.SharedWaitingAllowance())


# ADM01 --------------------------------------------------------------------- #
def test_ADM01_idempotent_replay_and_conflict(tmp_path):
    db = _db(tmp_path)
    _session(db)
    a1 = _admit(db, _req(op="op-1", body="hello"))
    a2 = _admit(db, _req(op="op-1", body="hello"))
    assert a1 == a2 and a2.idempotent_replay and not a1.idempotent_replay
    assert a1["id"] == str(a1) and a1["queue_sequence"] == 1
    with pytest.raises(tq.OwnershipConflictError) as ei:
        _admit(db, _req(op="op-1", body="DIFFERENT"))
    assert ei.value.status_code == 409
    # Replay still resolves after the queue is full / session closed.
    _admit(db, _req(op="op-2", body="b2"))
    db._conn().execute("UPDATE sessions SET status='closed' WHERE session_id='sess-1'")
    assert _admit(db, _req(op="op-1", body="hello"), cap=1) == a1


def test_ADM01b_db_default_hash_detects_body_change(tmp_path):
    """Convenience form: same operation id + different body ⇒ 409 even without
    a caller-supplied hash (the DB derives it from the canonical request)."""
    db = _db(tmp_path)
    _session(db)
    first = db.enqueue_turn(session_id="sess-1", body="one", operation_id="k")
    assert db.enqueue_turn(session_id="sess-1", body="one", operation_id="k") == first
    with pytest.raises(tq.OwnershipConflictError):
        db.enqueue_turn(session_id="sess-1", body="two", operation_id="k")


# ADM02 --------------------------------------------------------------------- #
def test_ADM02_per_session_cap_20(tmp_path):
    db = _db(tmp_path)
    _session(db)
    _session(db, "sess-2")
    for i in range(20):
        _admit(db, _req(op=f"o{i}", body=f"m{i}"))
    with pytest.raises(tq.CapacityError) as ei:
        _admit(db, _req(op="o20", body="m20"))
    assert ei.value.status_code == 429
    # Another session is unaffected (the cap is per session, not global).
    assert _admit(db, _req("sess-2", op="x", body="x"))


# ADM03 --------------------------------------------------------------------- #
def test_ADM03_fleet_cap_counts_queued_and_pending_only(tmp_path):
    db = _db(tmp_path)
    for s in ("s1", "s2", "s3"):
        _session(db, s)
    ids = [_admit(db, _req(s, op=f"{s}-{i}", body=f"{s}{i}"), cap=5)
           for s in ("s1", "s2") for i in range(2)]
    ids.append(_admit(db, _req("s3", op="s3-0", body="z"), cap=5))
    with pytest.raises(tq.CapacityError):
        _admit(db, _req("s3", op="s3-1", body="zz"), cap=5)
    # Activating (pending) still counts; claiming frees the waiting slot.
    db.activate_turn(ids[0])
    with pytest.raises(tq.CapacityError):
        _admit(db, _req("s3", op="s3-1", body="zz"), cap=5)
    db.claim_turn(ids[0], "worker-a", "worker_daemon", "inc-1")
    assert _admit(db, _req("s3", op="s3-1", body="zz"), cap=5)


# ADM04 --------------------------------------------------------------------- #
def test_ADM04_shared_allowance_legacy_blocks_managed(tmp_path):
    db = _db(tmp_path)
    _session(db)
    shared = ta.SharedWaitingAllowance()
    legacy_depth = {"n": 4}
    shared.register_legacy_probe(lambda: legacy_depth["n"])
    _admit(db, _req(op="a", body="a"), cap=5, allowance=shared)
    with pytest.raises(tq.CapacityError):
        _admit(db, _req(op="b", body="b"), cap=5, allowance=shared)
    legacy_depth["n"] = 3
    assert _admit(db, _req(op="b", body="b"), cap=5, allowance=shared)


def test_ADM04b_shared_allowance_managed_blocks_legacy(tmp_path):
    """Managed waiting rows consume the SAME allowance the legacy in-memory
    queue uses: no independent second '50' in one gateway."""
    db = _db(tmp_path)
    _session(db)
    shared = ta.SharedWaitingAllowance()

    async def scenario():
        q = SessionTaskQueue(5, lambda _t: "")
        shared.register_legacy_probe(q.qsize)
        q.share_allowance(shared)
        for i in range(3):
            _admit(db, _req(op=f"m{i}", body=f"m{i}"), cap=5, allowance=shared)
        q.put_nowait(_fake_task("l1"))
        q.put_nowait(_fake_task("l2"))
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait(_fake_task("l3"))
        assert q.full()
        # After a DB refresh showing managed rows left the waiting set, legacy
        # regains the room.
        for tid in [r["id"] for r in db._conn().execute(
                "SELECT id FROM mesh_tasks WHERE queue_protocol=1").fetchall()]:
            db.withdraw_turn(tid)
        gen = shared.snapshot_generation()
        assert shared.refresh_managed(db.managed_waiting_totals()["count"], gen)
        q.put_nowait(_fake_task("l3"))

    asyncio.run(scenario())


def test_ADM04c_unshared_queue_is_plain_asyncio_capacity():
    async def scenario():
        q = SessionTaskQueue(2, lambda _t: "")
        q.put_nowait(_fake_task("a"))
        q.put_nowait(_fake_task("b"))
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait(_fake_task("c"))

    asyncio.run(scenario())


def test_ADM04d_refresh_never_undercounts_a_racing_admission():
    shared = ta.SharedWaitingAllowance()
    gen = shared.snapshot_generation()          # refresh starts, reads DB = 0
    with shared.reserve(10) as slot:            # an admission commits meanwhile
        slot["committed"] = 1
    assert shared.refresh_managed(0, gen) is False  # stale read discarded
    assert shared.managed_cached() == 1


def _fake_task(tid):
    import types
    return types.SimpleNamespace(id=tid)


# ADM05 --------------------------------------------------------------------- #
def test_ADM05_stored_intent_byte_caps(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _session(db)
    big = "x" * (tq.MAX_INTENT_BYTES_PER_ROW // 2 + 10)  # prompt + payload copy > 2 MiB
    with pytest.raises(tq.ByteCapError) as ei:
        db.enqueue_turn(session_id="sess-1", body=big, operation_id="big")
    assert ei.value.status_code == 413
    assert db.managed_queued_bytes() == 0
    a = db.enqueue_turn(session_id="sess-1", body="é" * 100, operation_id="u")
    stored = db.get_task(a)["intent_bytes"]
    assert stored == db.managed_queued_bytes() > 200  # UTF-8 bytes, prompt + payload
    monkeypatch.setattr(tq, "MAX_INTENT_BYTES_FLEET", stored + 50)
    with pytest.raises(tq.CapacityError):
        db.enqueue_turn(session_id="sess-1", body="é" * 100, operation_id="v")


# ADM06 --------------------------------------------------------------------- #
class _CommitFailingConn:
    def __init__(self, real):
        self._real = real

    def execute(self, sql, *a):
        if sql.strip().upper().startswith("COMMIT"):
            raise sqlite3.OperationalError("disk I/O error (simulated at COMMIT)")
        return self._real.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_ADM06_failed_commit_is_never_acknowledged(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _session(db)
    real = db._conn()
    monkeypatch.setattr(db, "_conn", lambda: _CommitFailingConn(real))
    with pytest.raises(tq.BackingStoreError) as ei:
        _admit(db, _req(op="op-x", body="x", task_id="turn-x"))
    assert ei.value.status_code == 503
    monkeypatch.undo()
    assert db.get_task("turn-x") is None
    assert db.managed_waiting_totals()["count"] == 0


# ADM07 --------------------------------------------------------------------- #
def test_ADM07_enrollment_marker_required_by_service(tmp_path):
    db = _db(tmp_path)
    _session(db, "plain", enroll=False)
    with pytest.raises(tq.OwnershipConflictError):
        _admit(db, _req("plain"))
    with pytest.raises(tq.TurnNotFoundError):
        _admit(db, _req("ghost"))
    _session(db, "gone", status=SessionStatus.CLOSED)
    with pytest.raises(tq.OwnershipConflictError):
        _admit(db, _req("gone"))
    assert db.managed_waiting_totals()["count"] == 0


def test_ADM07b_human_turn_cannot_coalesce(tmp_path):
    db = _db(tmp_path)
    _session(db)
    with pytest.raises(tq.MalformedTurnError):
        db.enqueue_turn(session_id="sess-1", body="x", turn_source="human", coalesce_key="k")


# ADM08 --------------------------------------------------------------------- #
def test_ADM08_held_lock_fails_closed_within_deadline(tmp_path):
    db = _db(tmp_path)
    _session(db)
    blocker = sqlite3.connect(str(db._path), timeout=0.1)
    blocker.isolation_level = None
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.monotonic()
        with pytest.raises(tq.BackingStoreError):
            db.enqueue_turn(session_id="sess-1", body="x", operation_id="o", deadline_sec=0.5)
        assert time.monotonic() - t0 < 2.0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    # busy_timeout restored for later (legacy) writers on this thread.
    assert db._conn().execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    assert db.get_task("o") is None


# ADM09 --------------------------------------------------------------------- #
def test_ADM09_concurrency_permit_rejects_excess_promptly(tmp_path):
    db = _db(tmp_path)
    _session(db)
    held = 0
    try:
        for _ in range(ta.MAX_CONCURRENT_ADMISSIONS):
            assert ta._ADMISSION_PERMITS.acquire(blocking=False)
            held += 1
        t0 = time.monotonic()
        with pytest.raises(tq.CapacityError) as ei:
            _admit(db, _req())
        assert time.monotonic() - t0 < 0.5
        assert ei.value.context.get("retry_after") == 1
    finally:
        for _ in range(held):
            ta._ADMISSION_PERMITS.release()
    assert _admit(db, _req())  # permits released ⇒ admits again


def test_ADM09b_permit_released_by_thread_not_by_cancellation(tmp_path, monkeypatch):
    """Cancelling the awaiting coroutine must not free the permit while the
    thread still runs the DB work (design §8)."""
    db = _db(tmp_path)
    _session(db)
    gate = threading.Event()
    real = db.enqueue_turn

    def slow(**kw):
        gate.wait(5)
        return real(**kw)

    monkeypatch.setattr(db, "enqueue_turn", slow)

    async def scenario():
        t = asyncio.create_task(ta.admit_turn_async(
            db, _req(), fleet_cap=50, allowance=ta.SharedWaitingAllowance()))
        await asyncio.sleep(0.1)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        # thread still inside: its permit is still held
        free = 0
        while ta._ADMISSION_PERMITS.acquire(blocking=False):
            free += 1
        for _ in range(free):
            ta._ADMISSION_PERMITS.release()
        assert free == ta.MAX_CONCURRENT_ADMISSIONS - 1
        gate.set()
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    free = 0
    while ta._ADMISSION_PERMITS.acquire(blocking=False):
        free += 1
    for _ in range(free):
        ta._ADMISSION_PERMITS.release()
    assert free == ta.MAX_CONCURRENT_ADMISSIONS


# ADM10 --------------------------------------------------------------------- #
def test_ADM10_edit_reaccounts_bytes_and_holds_cap(tmp_path):
    db = _db(tmp_path)
    _session(db)
    a = db.enqueue_turn(session_id="sess-1", body="short", operation_id="e")
    before = db.get_task(a)["intent_bytes"]
    db.revise_turn(a, 1, body="a much longer edited body")
    grown = db.get_task(a)["intent_bytes"]
    assert grown > before
    with pytest.raises(tq.ByteCapError):
        db.revise_turn(a, 2, body="y" * tq.MAX_INTENT_BYTES_PER_ROW)
    row = db.get_task(a)
    assert row["revision"] == 2 and row["intent_bytes"] == grown


def test_ADM04e_db_transaction_counts_legacy_even_with_cold_cache(tmp_path):
    """After a restart the in-process managed cache is cold (0) while managed
    rows already wait in the DB; the admission TRANSACTION must still add the
    legacy occupancy to the durable managed count (the authoritative check)."""
    db = _db(tmp_path)
    _session(db)
    for i in range(3):
        _admit(db, _req(op=f"pre{i}", body=f"p{i}"), cap=50)
    cold = ta.SharedWaitingAllowance()
    cold.register_legacy_probe(lambda: 2)
    with pytest.raises(tq.CapacityError):
        _admit(db, _req(op="new", body="n"), cap=5, allowance=cold)
    assert db.managed_waiting_totals()["count"] == 3


def test_ADM04f_shared_blocking_put_never_leaks_raw_queuefull(tmp_path):
    """A87 m9: the legacy throttle branch (blocking put under wait_for) must end
    in TimeoutError (→ the orchestrator's RuntimeError('Task queue is full')),
    never a raw QueueFull, when a managed reservation holds the allowance; and it
    succeeds once room frees on another thread."""
    shared = ta.SharedWaitingAllowance()

    async def scenario():
        q = SessionTaskQueue(2, lambda _t: "")
        shared.register_legacy_probe(q.qsize)
        q.share_allowance(shared)
        q.put_nowait(_fake_task("l1"))
        with shared.reserve(2):  # a racing managed admission holds the last slot
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.put(_fake_task("l2")), 0.2)
            waiter = asyncio.ensure_future(asyncio.wait_for(q.put(_fake_task("l2")), 2))
            await asyncio.sleep(0.1)
        await waiter  # reservation released ⇒ put completes
        assert q.qsize() == 2

    asyncio.run(scenario())


def test_ADM04g_orchestrator_throttle_maps_to_runtime_error(tmp_path, monkeypatch):
    from src.orchestrator import TaskOrchestrator
    import types

    shared = ta.SharedWaitingAllowance()
    o = TaskOrchestrator.__new__(TaskOrchestrator)

    async def scenario():
        o.task_queue = SessionTaskQueue(1, lambda _t: "")
        shared.register_legacy_probe(o.task_queue.qsize)
        o.task_queue.share_allowance(shared)
        o.active_tasks = {}
        o._emit_event = lambda *a, **k: None
        o._emit_turn_telemetry = lambda *a, **k: None
        o._record_flow_run_start = lambda t: None
        o._record_flow_stage = lambda *a, **k: None
        o._task_requires_local_execution = lambda t: False
        task = types.SimpleNamespace(id="t1", metadata={"source": "runtime"},
                                     priority=types.SimpleNamespace(value="medium"))
        monkeypatch.setattr(asyncio, "wait_for", _fast_wait_for)
        with shared.reserve(1):
            with pytest.raises(RuntimeError, match="Task queue is full"):
                await o._enqueue_task(task)

    asyncio.run(scenario())


_real_wait_for = asyncio.wait_for


async def _fast_wait_for(aw, timeout):
    return await _real_wait_for(aw, min(timeout, 0.2))
