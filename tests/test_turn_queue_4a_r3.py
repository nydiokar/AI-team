"""A87 round-3 review probes (Stage 4a rework 2), adopted as permanent tests
(rework 3) asserting the CORRECT behavior. Offline; spawn guard autouse via import."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import tests.test_turn_queue_producer1 as P
from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control import turn_scheduler as ts
from tests.test_turn_queue_producer1 import (  # noqa: F401
    _flags, _managed_rows, _no_cli_spawn, _setup, _submit, _sess,
)

PAST = (datetime.now(tz=timezone.utc) - timedelta(seconds=1)).isoformat()


def _pass(db, o):
    return asyncio.run(ts.run_scheduler_pass(
        db, o._prepare_managed_turn, allowance=ta.SharedWaitingAllowance(),
        recover_lineage=o._recover_managed_lineage))


def _snapshot(db, case_ids, tid):
    links = []
    evs = []
    for c in case_ids:
        links += sorted((c == case_ids[0], l["entity_type"], l["role"],
                         l["entity_id"] == tid) for l in db.list_flow_links(flow_run_id=c))
        evs += sorted(e["event_type"] for e in db.list_flow_events(c))
    return links, evs


# 1a — a DB failure inside _record_flow_run_start (swallowed) -------------- #
def test_1a_join_lineage_db_error_must_not_finalize_caseless(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    real = db.get_flow_run
    calls = {"n": 0}

    def flaky(fid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(fid)
    monkeypatch.setattr(db, "get_flow_run", flaky)
    # A DB error inside the lineage procedure RAISES: typed 503, row stays pending.
    with pytest.raises(tq.BackingStoreError):
        _submit(o, join_case_id=case_id, operation_id="j1", source="runtime")
    [r] = _managed_rows(db)
    tid = r["id"]
    assert r["lineage_state"] == "pending" and r["flow_run_id"] is None
    res = _pass(db, o)
    assert res.activated == 0 and db.get_task(tid)["status"] == "queued"
    # Recovery on lease expiry re-runs the same procedure (DB healthy now).
    db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until=? WHERE id=?", (PAST, tid))
    res = _pass(db, o)
    links = [l["entity_id"] for l in db.list_flow_links(flow_run_id=case_id)
             if l["entity_type"] == "task"]
    assert res.activated == 1 and links == [tid]
    assert db.get_task(tid)["flow_run_id"] == case_id


def test_1a_birth_lineage_db_error_must_not_finalize_caseless(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    # rework 3: the managed birth is `get_or_create_task_flow_run` (convergent).
    real = db.get_or_create_task_flow_run
    monkeypatch.setattr(db, "get_or_create_task_flow_run",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with pytest.raises(tq.BackingStoreError):
        _submit(o, operation_id="b1", source="runtime", parent_flow_run_id="parent-flow")
    [r] = _managed_rows(db)
    assert r["lineage_state"] == "pending" and r["flow_run_id"] is None
    assert _pass(db, o).activated == 0
    monkeypatch.setattr(db, "get_or_create_task_flow_run", real)
    db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until=? WHERE id=?", (PAST, r["id"]))
    res = _pass(db, o)
    r = db.get_task(r["id"])
    assert res.activated == 1 and r["flow_run_id"] is not None


# 1b — recovery after a crash MID-lineage reproduces the live writes -------- #
def _crash_on_event(o, monkeypatch, event_type):
    real = o._record_flow_event

    def ev(fid, et, *a, **k):
        if et == event_type:
            raise SystemExit("crash mid-lineage")
        return real(fid, et, *a, **k)
    monkeypatch.setattr(o, "_record_flow_event", ev)
    return real


def test_1b_join_partial_crash_recovery_equals_live(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    # Control: clean live run.
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    db, o = _setup(tmp_path / "a", monkeypatch)
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    tid = _submit(o, join_case_id=case_id, operation_id="j", source="runtime")
    live = _snapshot(db, [case_id], tid)
    live_aff = (getattr(_sess(), "current_case_id", None), getattr(_sess(), "case_role", None))
    # Crash after the task link, before task.attached.
    db, o = _setup(tmp_path / "b", monkeypatch)
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    real = _crash_on_event(o, monkeypatch, "task.attached")
    with pytest.raises(SystemExit):
        _submit(o, join_case_id=case_id, operation_id="j", source="runtime")
    monkeypatch.setattr(o, "_record_flow_event", real)
    [row] = _managed_rows(db)
    db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until=? WHERE id=?", (PAST, row["id"]))
    _pass(db, o)
    rec = _snapshot(db, [case_id], row["id"])
    rec_aff = (getattr(_sess(), "current_case_id", None), getattr(_sess(), "case_role", None))
    print("LIVE", live, live_aff)
    print("RECOVERED", rec, rec_aff)
    assert rec == live


def test_1b_birth_partial_crash_recovery_equals_live(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    db, o = _setup(tmp_path / "a", monkeypatch)
    parent = db.open_case(objective="p", session_id="mgr-x", role="manager")
    tid = _submit(o, operation_id="b", source="runtime", parent_flow_run_id=parent)
    fid = db.get_task(tid)["flow_run_id"]
    live = _snapshot(db, [fid, parent], tid)
    db, o = _setup(tmp_path / "b", monkeypatch)
    parent = db.open_case(objective="p", session_id="mgr-x", role="manager")
    real = _crash_on_event(o, monkeypatch, "flow.created")
    with pytest.raises(SystemExit):
        _submit(o, operation_id="b", source="runtime", parent_flow_run_id=parent)
    monkeypatch.setattr(o, "_record_flow_event", real)
    [row] = _managed_rows(db)
    db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until=? WHERE id=?", (PAST, row["id"]))
    _pass(db, o)
    fid = db.get_task(row["id"])["flow_run_id"]
    rec = _snapshot(db, [fid, parent], row["id"])
    print("LIVE", live)
    print("RECOVERED", rec)
    assert rec == live


# 1c — live writer stalled past its lease BEFORE writing; recovery races ---- #
def test_1c_stalled_live_writer_births_second_case(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    parent = db.open_case(objective="p", session_id="mgr-x", role="manager")
    real = db.existing_task_lineage
    state = {"first": True}

    def stalled(tid):
        res = real(tid)
        if state["first"]:
            state["first"] = False
            # Writer stalls (thread-pool starvation / GC / slow FS) past the lease.
            db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until=? WHERE id=?", (PAST, tid))
            _pass(db, o)  # recovery claims, births, finalizes, activates
        return res
    monkeypatch.setattr(db, "existing_task_lineage", stalled)
    try:
        tid = _submit(o, operation_id="c1", source="runtime", parent_flow_run_id=parent)
        print("RETURNED", tid)
    except tq.TurnQueueError as e:
        print("RAISED", type(e).__name__, e)
    [row] = _managed_rows(db)
    n = db._conn().execute("SELECT flow_run_id FROM flow_runs WHERE task_id=?", (row["id"],)).fetchall()
    child = [l for l in db.list_flow_links(flow_run_id=parent) if l["role"] == "child_flow"]
    print("FLOWS", [x[0] for x in n], "row.flow_run_id", row["flow_run_id"], "child_links", len(child))
    assert len(n) == 1 and len(child) == 1


# 1d — withdraw during the lineage-pending window ------------------------- #
def test_1d_withdraw_during_lineage_window(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    o._LINEAGE_REPLAY_WAIT_SEC = 0.2
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    real = db.existing_task_lineage

    def wd(tid):
        db.withdraw_turn(tid, expected_revision=1)
        return real(tid)
    monkeypatch.setattr(db, "existing_task_lineage", wd)
    out = []
    try:
        out.append(("ok", _submit(o, join_case_id=case_id, operation_id="w1", source="runtime")))
    except Exception as e:  # noqa: BLE001
        out.append(("err", type(e).__name__, str(e)[:80]))
    monkeypatch.setattr(db, "existing_task_lineage", real)
    try:
        out.append(("replay_ok", _submit(o, join_case_id=case_id, operation_id="w1", source="runtime")))
    except Exception as e:  # noqa: BLE001
        out.append(("replay_err", type(e).__name__, str(e)[:80]))
    [row] = _managed_rows(db)
    links = [l["entity_id"] for l in db.list_flow_links(flow_run_id=case_id) if l["entity_type"] == "task"]
    # The admitting call reports withdrawn (not "recovery will retry"); the
    # lineage is resolved (void); replays report withdrawn; no Case link left.
    assert out[0][0] == "ok" and out[0][1].status == "withdrawn"
    assert out[1][0] == "replay_ok" and out[1][1].status == "withdrawn"
    assert row["status"] == "withdrawn" and row["lineage_state"] == "void" and links == []
    assert db.list_lineage_recovery() == []


# 2 — requeue on dead carrier keeps lineage metadata for re-preparation ---- #
def test_2_requeue_then_reactivate_keeps_case_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    tid = _submit(o, join_case_id=case_id, operation_id="q1", source="runtime")
    assert _pass(db, o).activated == 1
    p1 = json.loads(db.get_task(tid)["payload"])
    old = (datetime.now(tz=timezone.utc) - timedelta(seconds=1000)).isoformat()
    db._conn().execute("UPDATE nodes SET last_heartbeat=? WHERE node_id='worker-a'", (old,))
    assert db.requeue_turns_on_dead_carriers() == [tid]
    # race: carrier comes back and tries to claim with the old view
    with pytest.raises(tq.TurnQueueError):
        db.claim_turn(task_id=tid, node_id="worker-a", carrier_kind="worker", incarnation_id="i")
    db._conn().execute("UPDATE nodes SET last_heartbeat=? WHERE node_id='worker-a'",
                       (datetime.now(tz=timezone.utc).isoformat(),))
    db._conn().execute("UPDATE mesh_tasks SET blocked_until=NULL WHERE id=?", (tid,))
    assert _pass(db, o).activated == 1
    p2 = json.loads(db.get_task(tid)["payload"])
    print("META1", {k: v for k, v in p1["metadata"].items() if k.startswith("__")})
    print("META2", {k: v for k, v in p2["metadata"].items() if k.startswith("__")})
    assert p1["metadata"] == p2["metadata"] and p1["prompt"] == p2["prompt"]


# 2b — carrier dies AFTER activation with nothing else queued: does anything
# ever run the requeue? (scheduler sleeps "until a hint" when waiting == 0)
def test_2b_idle_fleet_pending_on_dead_carrier_is_requeued(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    tid = _submit(o, operation_id="d1", source="runtime")

    async def scenario():
        s = ts.TurnScheduler(db, o._prepare_managed_turn, fallback_sec=0.05,
                             safety_net_sec=0.2, recover_lineage=o._recover_managed_lineage)
        task = asyncio.create_task(s.run())
        await asyncio.sleep(0.3)
        assert db.get_task(tid)["status"] == "pending"  # activated to worker-a
        old = (datetime.now(tz=timezone.utc) - timedelta(seconds=1000)).isoformat()
        db._conn().execute("UPDATE nodes SET last_heartbeat=?, status='offline' WHERE node_id='worker-a'", (old,))
        p0 = s.passes
        await asyncio.sleep(2.0)
        s.stop(); await asyncio.wait_for(task, 2)
        return p0, s.passes
    p0, p1 = asyncio.run(scenario())
    r = db.get_task(tid)
    print("PASSES before/after carrier death", p0, p1, "status", r["status"], "reason", r["blocked_reason"])
    assert r["status"] == "queued" and "carrier_offline" in (r["blocked_reason"] or "")


# rework 3 test additions -------------------------------------------------- #
def test_requeue_never_touches_claimed_or_running(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _sid2 = "sess-2"
    from src.core.interfaces import Session, SessionStatus
    db.upsert_session(Session(session_id=_sid2, backend="claude", repo_path="/tmp/repo",
                              status=SessionStatus.IDLE, created_at=P.NOW, updated_at=P.NOW,
                              machine_id="worker-a"))
    db.enroll_session(_sid2)
    t1 = _submit(o, operation_id="c1")
    t2 = _submit(o, operation_id="r1", session_id=_sid2)
    assert _pass(db, o).activated == 2
    tok1 = db.claim_turn(t1, "worker-a", "worker_daemon", "i")
    tok2 = db.claim_turn(t2, "worker-a", "worker_daemon", "i")
    db.start_turn(t2, tok2, incarnation_id="i")
    db.mark_node_offline("worker-a")
    assert db.requeue_turns_on_dead_carriers() == []
    assert db.get_task(t1)["status"] == "claimed" and db.get_task(t2)["status"] == "running"


def test_requeue_on_stale_heartbeat_with_online_status(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    tid = _submit(o, operation_id="s1")
    assert _pass(db, o).activated == 1
    old = (datetime.now(tz=timezone.utc) - timedelta(seconds=1000)).isoformat()
    db._conn().execute("UPDATE nodes SET last_heartbeat=?, status='online' WHERE node_id='worker-a'", (old,))
    assert db.requeue_turns_on_dead_carriers() == [tid]
    assert db.get_task(tid)["blocked_reason"] == "carrier_offline: worker-a"


def test_requeue_reads_before_taking_the_write_lock(tmp_path, monkeypatch):
    """m2: an empty requeue pass never enters BEGIN IMMEDIATE."""
    db, o = _setup(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(db, "_managed_write", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("write lock taken")))
    assert db.requeue_turns_on_dead_carriers() == []
    assert called == []


@pytest.mark.parametrize("shape", ["join", "attach", "birth"])
def test_managed_lineage_parity_with_legacy_path(tmp_path, monkeypatch, shape):
    """The managed procedure writes EXACTLY what the legacy
    `_record_flow_run_start` writes for the same request (links by
    type/role/creator, events with payloads, session affiliation)."""
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")

    def run(sub, enroll):
        (tmp_path / sub).mkdir()
        db, o = _setup(tmp_path / sub, monkeypatch, enroll=enroll)
        mgr = db.open_case(objective="obj", session_id="mgr-x", role="manager")
        kw = {"operation_id": "p", "source": "runtime"}
        if shape == "join":
            kw["join_case_id"] = mgr
        elif shape == "attach":
            db.open_case(objective="own", session_id="sess-1", role="manager")
        else:
            kw["parent_flow_run_id"] = mgr
        tid = str(_submit(o, **kw))
        links = sorted((l["entity_type"], l["role"], l["created_by"] or "",
                        l["entity_id"] == tid) for l in db._conn().execute(
                            "SELECT * FROM flow_links WHERE entity_id != 'mgr-x'").fetchall())
        evs = sorted((e["event_type"], e["entity_type"] or "",
                      (e["payload_json"] or "").replace(tid, "TID"))
                     for e in db._conn().execute(
                         "SELECT * FROM flow_events WHERE event_type != 'flow.created' "
                         "OR entity_id = ?", (tid,)).fetchall())
        s = db.get_session("sess-1")
        return links, evs, bool(s["current_case_id"]), s["case_role"]

    legacy = run("legacy", False)
    managed = run("managed", True)
    assert legacy == managed
