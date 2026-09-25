"""A87 Stage 4a adversarial-review probes, adopted as permanent tests (DB /
admission / scheduler / claim). Offline only; spawn guard autouse."""
import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control import turn_scheduler as ts
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _no_cli_spawn(monkeypatch):
    from src.backends import claude_driver

    def _boom(*_a, **_k):
        raise AssertionError("real CLI spawn attempted")

    monkeypatch.setattr(claude_driver._SDKSession, "start", _boom, raising=False)
    try:
        import claude_agent_sdk
        monkeypatch.setattr(claude_agent_sdk.ClaudeSDKClient, "connect", _boom, raising=False)
    except Exception:
        pass
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


def _session(db, sid, machine="worker-a", enroll=True):
    db.upsert_session(Session(session_id=sid, backend="claude", repo_path="/tmp/repo",
                              status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW,
                              machine_id=machine))
    if enroll:
        db.enroll_session(sid)
    # Fixture (rework 2): the assigned carrier is a LIVE registered node, since
    # a pending row on a dead/unknown carrier is now returned to queued.
    if machine:
        db.upsert_node(node_id=machine, tailscale_ip="", api_port=9001, backends=["claude"],
                       max_concurrent=2, managed_backends=["claude"])


# --- concurrency: 30 threads, per-session cap 20 / fleet cap 50 ------------- #
def test_concurrent_caps_direct_txn(tmp_path):
    db = MeshDB(str(tmp_path / "m.db"))
    _session(db, "s1")
    for i in range(5):
        _session(db, f"f{i}")
    results = {"ok": 0, "cap": 0, "other": []}
    lock = threading.Lock()
    barrier = threading.Barrier(30)

    def one(i, sid, fleet):
        barrier.wait()
        try:
            db.enqueue_turn(session_id=sid, body=f"b{i}", operation_id=f"{sid}-op{i}",
                            turn_source="human", fleet_cap=fleet, require_enrolled=True)
            with lock:
                results["ok"] += 1
        except tq.CapacityError:
            with lock:
                results["cap"] += 1
        except Exception as e:  # noqa
            with lock:
                results["other"].append(repr(e))

    ths = [threading.Thread(target=one, args=(i, "s1", 1000)) for i in range(30)]
    [t.start() for t in ths]; [t.join() for t in ths]
    n = db._conn().execute("SELECT COUNT(*) FROM mesh_tasks WHERE session_id='s1'").fetchone()[0]
    print("per-session", results, "rows", n)
    assert n == 20 and results["ok"] == 20 and results["cap"] == 10, results
    assert not results["other"]

    # fleet cap 50 against 5 sessions x 12 (60 attempts from 30 threads x2)
    results.update(ok=0, cap=0, other=[])
    barrier2 = threading.Barrier(30)

    def two(i):
        barrier2.wait()
        for k in range(2):
            sid = f"f{(i * 2 + k) % 5}"
            try:
                db.enqueue_turn(session_id=sid, body=f"x{i}-{k}", operation_id=f"{sid}-x{i}-{k}",
                                turn_source="human", fleet_cap=50 + 20, require_enrolled=True,
                                external_waiting=0)
                with lock:
                    results["ok"] += 1
            except tq.CapacityError:
                with lock:
                    results["cap"] += 1
            except Exception as e:  # noqa
                with lock:
                    results["other"].append(repr(e))

    ths = [threading.Thread(target=two, args=(i,)) for i in range(30)]
    [t.start() for t in ths]; [t.join() for t in ths]
    tot = db.managed_waiting_totals()
    print("fleet", results, tot)
    # 20 from s1 + ... fleet cap 70 total
    assert tot["count"] <= 70, tot
    assert not results["other"], results["other"]


def test_concurrent_via_service_permits(tmp_path):
    db = MeshDB(str(tmp_path / "m.db"))
    _session(db, "s1")
    allow = ta.SharedWaitingAllowance()
    out = {"ok": 0, "429": 0, "other": []}
    lock = threading.Lock()
    barrier = threading.Barrier(30)

    def one(i):
        barrier.wait()
        try:
            ta.admit_turn(db, ta.AdmissionRequest(
                session_id="s1", body=f"b{i}", payload={"prompt": f"b{i}"}, turn_source="human",
                operation_id=f"op{i}", idempotency_scope="operator:s1:instruction",
                admission_hash=f"h{i}"), fleet_cap=20, allowance=allow)
            with lock:
                out["ok"] += 1
        except tq.CapacityError:
            with lock:
                out["429"] += 1
        except Exception as e:  # noqa
            with lock:
                out["other"].append(repr(e))

    ths = [threading.Thread(target=one, args=(i,)) for i in range(30)]
    [t.start() for t in ths]; [t.join() for t in ths]
    n = db.managed_waiting_totals()["count"]
    print("service", out, "rows", n, "cache", allow.managed_cached())
    assert n <= 20 and out["ok"] == n and not out["other"]
    assert allow.managed_cached() == n


# --- commit failure: no row, typed 503 ------------------------------------ #
def test_commit_failure_no_ack(tmp_path, monkeypatch):
    db = MeshDB(str(tmp_path / "m.db"))
    _session(db, "s1")
    real = db._conn()

    class Wrap:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a):
            if sql.strip().upper().startswith("COMMIT"):
                raise sqlite3.OperationalError("disk I/O error (injected)")
            return self._c.execute(sql, *a)

        def __getattr__(self, n):
            return getattr(self._c, n)

    monkeypatch.setattr(db, "_conn", lambda: Wrap(real))
    with pytest.raises(tq.BackingStoreError):
        db.enqueue_turn(session_id="s1", body="x", operation_id="o", turn_source="human",
                        fleet_cap=50, require_enrolled=True)
    monkeypatch.undo()
    assert db._conn().execute("SELECT COUNT(*) FROM mesh_tasks").fetchone()[0] == 0


# --- 5 s deadline covers in-process lock AND sqlite lock ------------------- #
def test_deadline_inprocess_lock(tmp_path):
    db = MeshDB(str(tmp_path / "m.db"))
    _session(db, "s1")
    db._write_lock.acquire()
    try:
        t0 = time.monotonic()
        with pytest.raises(tq.BackingStoreError):
            db.enqueue_turn(session_id="s1", body="x", operation_id="o", turn_source="human",
                            fleet_cap=50, deadline_sec=1.0)
        el = time.monotonic() - t0
    finally:
        db._write_lock.release()
    print("inproc lock elapsed", el)
    assert 0.9 < el < 1.5


def test_deadline_sqlite_lock_split(tmp_path):
    """Hold the in-process lock 0.6s then the sqlite lock (other connection):
    total must stay ~deadline."""
    db = MeshDB(str(tmp_path / "m.db"))
    _session(db, "s1")
    other = sqlite3.connect(str(tmp_path / "m.db"), isolation_level=None)
    other.execute("BEGIN IMMEDIATE;")
    db._write_lock.acquire()
    threading.Timer(0.6, db._write_lock.release).start()
    t0 = time.monotonic()
    with pytest.raises(tq.BackingStoreError):
        db.enqueue_turn(session_id="s1", body="x", operation_id="o", turn_source="human",
                        fleet_cap=50, deadline_sec=1.5)
    el = time.monotonic() - t0
    other.execute("ROLLBACK;")
    print("split elapsed", el)
    assert el < 1.8, el
    # busy_timeout restored
    bt = db._conn().execute("PRAGMA busy_timeout").fetchone()[0]
    print("busy_timeout after", bt)


# --- idempotency: different messages never collide ------------------------ #
def test_distinct_messages_no_collision_and_legit_retry(tmp_path):
    db = MeshDB(str(tmp_path / "m.db"))
    _session(db, "s1")
    a = db.enqueue_turn(session_id="s1", body="same", operation_id="task:1", turn_source="human", fleet_cap=50)
    b = db.enqueue_turn(session_id="s1", body="same", operation_id="task:2", turn_source="human", fleet_cap=50)
    assert str(a) != str(b)
    r = db.enqueue_turn(session_id="s1", body="same", operation_id="task:1", turn_source="human", fleet_cap=50)
    assert r.idempotent_replay and str(r) == str(a)


# --- scheduler starvation: 25 prepare-failing heads block everyone else ----- #
def test_blocked_heads_starve_other_sessions(tmp_path):
    db = MeshDB(str(tmp_path / "m.db"))
    for i in range(25):
        _session(db, f"bad{i:02d}")
        db.enqueue_turn(session_id=f"bad{i:02d}", body="x", operation_id=f"b{i}", turn_source="human", fleet_cap=1000)
        time.sleep(0.002)
    _session(db, "good")
    good = db.enqueue_turn(session_id="good", body="y", operation_id="g", turn_source="human", fleet_cap=1000)

    async def prep(head, row):
        if str(row["session_id"]).startswith("bad"):
            raise RuntimeError("prepare keeps failing")
        return ts.PreparedTurn(action="resume_session", payload={"prompt": "y"}, machine_id="worker-a")

    for _ in range(3):
        res = asyncio.run(ts.run_scheduler_pass(db, prep, allowance=ta.SharedWaitingAllowance()))
        print(res)
    st = db.get_task(str(good))["status"]
    print("good status after 3 passes:", st)
    assert st == "pending", "healthy session starved by 25 blocked heads"


# --- claim ignores carrier assignment (machine_id) ------------------------- #
def test_claim_ignores_machine_assignment(tmp_path):
    db = MeshDB(str(tmp_path / "m.db"))
    _session(db, "s1", machine="hostA")
    t = db.enqueue_turn(session_id="s1", body="y", operation_id="g", turn_source="human", fleet_cap=50)

    async def prep(head, row):
        return ts.PreparedTurn(action="resume_session", payload={"prompt": "y"}, machine_id="hostA")

    asyncio.run(ts.run_scheduler_pass(db, prep, allowance=ta.SharedWaitingAllowance()))
    assert db.get_task(str(t))["machine_id"] == "hostA"
    with pytest.raises(tq.OwnershipConflictError):
        db.claim_turn(task_id=str(t), node_id="hostB-ROGUE", carrier_kind="daemon",
                      incarnation_id="inc-1")
    row = db.get_task(str(t))
    assert row.get("claimed_by") != "hostB-ROGUE", "row pinned to hostA claimed by hostB"
    assert row["status"] == "pending"
    tok = db.claim_turn(task_id=str(t), node_id="hostA", carrier_kind="daemon",
                        incarnation_id="inc-1")
    assert tok and db.get_task(str(t))["claimed_by"] == "hostA"


# --- query plans at 100k completed rows ------------------------------------ #
def test_plans_100k(tmp_path):
    db = MeshDB(str(tmp_path / "m.db"))
    _session(db, "a")
    with db._write() as w:
        w.executemany(
            "INSERT INTO mesh_tasks (id, session_id, backend, action, payload, status, created_at, updated_at, "
            "queue_protocol, queue_sequence, idempotency_scope, idempotency_key) VALUES "
            "(?, 'a', 'claude', 'resume_session', '{}', 'completed', ?, ?, 1, ?, 'operator:a:instruction', ?)",
            [(f"h{i}", NOW, NOW, i + 1, f"k{i}") for i in range(100000)])
    db.enqueue_turn(session_id="a", body="live", operation_id="live", turn_source="human", fleet_cap=50)
    c = db._conn()
    c.execute("ANALYZE")

    def plan(sql, args):
        return " | ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql, args))

    print("IDEM:", plan("SELECT id FROM mesh_tasks WHERE queue_protocol = 1 AND idempotency_scope IS ? AND idempotency_key = ?", ("operator:a:instruction", "k5")))
    print("SEQ:", plan("SELECT queue_sequence FROM mesh_tasks WHERE queue_protocol = 1 AND session_id = ? AND queue_sequence IS NOT NULL ORDER BY queue_sequence DESC LIMIT 1", ("a",)))
    for name, fn in [("heads", lambda: db.select_eligible_turn_heads(25)),
                     ("totals", db.managed_waiting_totals),
                     ("idem", lambda: db.find_turn_by_idempotency("operator:a:instruction", "k99999")),
                     ("enroll", lambda: db.is_session_enrolled("a"))]:
        t0 = time.perf_counter()
        for _ in range(20):
            fn()
        print(name, "avg ms", (time.perf_counter() - t0) / 20 * 1000)
    t0 = time.perf_counter()
    db.enqueue_turn(session_id="a", body="live2", operation_id="live2", turn_source="human", fleet_cap=50)
    print("enqueue ms", (time.perf_counter() - t0) * 1000)
    import inspect
    src = inspect.getsource(MeshDB.select_eligible_turn_heads)
    sql = src.split('f"""', 1)[1].split('"""', 1)[0]
    heads_plan = plan(sql, (NOW, NOW, 25))
    assert "idx_mesh_turns_waiting" in heads_plan, heads_plan
    src = inspect.getsource(MeshDB.activate_prepared_turn)
    bsql = src.split('f"""', 1)[1].split('"""', 1)[0]
    assert "SCAN mesh_tasks" not in plan(bsql, ("a", "x")).replace("USING", "~")
