"""A82 Stage 4c — producer 3: Case continuation token→turn linkage and durable
finalization for ENROLLED Manager sessions.

Real pieces: a file-backed ``MeshDB`` as ``get_db()``, REAL bound orchestrator
methods on a bare instance (``_continue_case_once`` → managed admission →
lineage; ``_reconcile_continuation_finalizers``; ``interrupt_case`` /
``close_case`` / ``record_review`` / ``stop_managed_session_turn``), the REAL
scheduler pass (activation-time revalidation) and the REAL managed claim/start/
complete DB seams. No backend/CLI (autouse spawn guard from the producer-1 suite).
"""
import asyncio
import json

import pytest

from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control.db import continuation_task_id, producer_turn_id
from src.core.interfaces import Session, SessionStatus as SS
from tests.test_turn_queue_4b import _pass, _run, _wire
from tests.test_turn_queue_producer1 import (  # noqa: F401
    NOW, _flags, _managed_rows, _no_cli_spawn, _setup, _submit, _sess,
)


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch):
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


def _env(tmp_path, monkeypatch, *, status=SS.AWAITING_INPUT, enroll=True):
    # set here (after the imported autouse `_flags`, which clears the drive flag)
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    db, o = _setup(tmp_path, monkeypatch, enroll=enroll)
    _wire(o)
    s = _sess()
    s.status = status
    o.session_store.save(s)
    return db, o


def _fresh(o_like, monkeypatch):
    """A NEW orchestrator process view of the same DB (no in-memory state)."""
    from src.orchestrator import TaskOrchestrator
    from src.services.session_store import SessionStore

    o = TaskOrchestrator.__new__(TaskOrchestrator)
    o.session_store = SessionStore()
    o.task_queue = o_like.task_queue
    o.active_tasks = {}
    o._compact_injected_ids = set()
    o.events = []
    o._emit_event = lambda name, task, data=None: o.events.append(name)
    o._emit_turn_telemetry = lambda name, task, data=None, **k: o.events.append(name)
    return _wire(o)


def _case(db, *, members=("w1",), finished=("w1",)):
    cid = db.open_case("ship X", "sess-1", role="manager",
                       completion_criteria='{"round_cap": 5}')
    db.arm_wait_group(cid, "g1", "ALL", list(members))
    for t in finished:
        _finish(db, cid, t)
    return cid


def _finish(db, cid, tid):
    db.append_flow_event(cid, "task.finished", "worker", entity_type="task",
                         entity_id=tid, payload={"outcome": "success"})


def _tick(o, db, cid):
    return asyncio.run(o._continue_case_once(db, cid))


def _reconcile(o, db):
    return asyncio.run(o._reconcile_continuation_finalizers(db))


def _cont_rows(db):
    return [r for r in _managed_rows(db) if r["turn_kind"] == "continuation"]


def _complete(db, tid, *, status="completed"):
    tok = _run(db, tid)
    return db.complete_turn(tid, tok, {"success": status == "completed"}, status=status)


def _resolved_events(db, cid, gid="g1"):
    return [e for e in db.list_flow_events(cid)
            if e["event_type"] == "worker.wait_resolved" and e.get("entity_id") == gid]


def _running_operator_turn(db, o, op="op-1"):
    t = _submit(o, operation_id=op)
    _pass(db, o)
    _run(db, t)
    return t


# --------------------------------------------------------------------------- #
# Busy Manager: admitted as ONE durable turn, queued behind, no interrupt
# --------------------------------------------------------------------------- #
def test_Q01_busy_manager_gets_one_durable_continuation_queued_behind(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _case(db)
    t = _running_operator_turn(db, o)
    assert _tick(o, db, cid) == 1  # legacy would return 0 for a BUSY Manager
    cont_id = continuation_task_id(cid, 1)
    expect = producer_turn_id(cont_id, "sess-1", 1)
    rows = _cont_rows(db)
    assert [r["id"] for r in rows] == [expect]
    c = rows[0]
    assert c["status"] == "queued" and c["turn_source"] == "system"
    assert c["idempotency_scope"] == "automation:sess-1:continuation"
    assert c["idempotency_key"] == f"{cont_id}#1"
    assert c["lineage_state"] == "done" and c["flow_run_id"] == cid
    token = db.get_task(cont_id)
    assert token["status"] == "claimed" and token["producer_turn_id"] == expect
    assert token["claimed_at"] is None  # the legacy lease reaper never re-offers it
    # the active turn is untouched: no cancel, no interrupt control row
    active = db.get_task(t)
    assert active["status"] == "running" and not active["cancel_token"]
    assert not db._conn().execute(
        "SELECT 1 FROM mesh_tasks WHERE action = 'cancel_managed'").fetchone()
    # coalesced: further ticks never mint a second turn — and never even open
    # an admission txn (the durable link short-circuits them)
    real_enqueue = type(db).enqueue_turn
    admissions = []
    monkeypatch.setattr(type(db), "enqueue_turn",
                        lambda self, *a, **k: admissions.append(k) or real_enqueue(self, *a, **k))
    assert _tick(o, db, cid) == 0 and _tick(o, db, cid) == 0
    assert admissions == []
    assert len(_cont_rows(db)) == 1
    assert db.token_to_turn(coalesce_key=cont_id, session_id="sess-1") == expect


def test_Q01b_concurrent_ticks_collapse_to_one_turn(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)

    async def both():
        return await asyncio.gather(o._continue_case_once(db, cid), o._continue_case_once(db, cid))
    assert sum(asyncio.run(both())) == 1
    assert len(_cont_rows(db)) == 1


# --------------------------------------------------------------------------- #
# Crash windows
# --------------------------------------------------------------------------- #
def test_Q02_crash_between_token_write_and_admission_replays_same_id(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    real = type(db).enqueue_turn

    def crash(self, *a, **k):
        raise RuntimeError("process died before admission (injected)")
    monkeypatch.setattr(type(db), "enqueue_turn", crash)
    with pytest.raises(RuntimeError):
        _tick(o, db, cid)
    cont_id = continuation_task_id(cid, 1)
    token = db.get_task(cont_id)
    assert token is not None and token["status"] == "pending" and not token["producer_turn_id"]
    assert _cont_rows(db) == []
    monkeypatch.setattr(type(db), "enqueue_turn", real)
    o2 = _fresh(o, monkeypatch)
    assert _tick(o2, db, cid) == 1
    assert [r["id"] for r in _cont_rows(db)] == [producer_turn_id(cont_id, "sess-1", 1)]


def test_Q02b_crash_after_admission_commit_never_mints_a_second_turn(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)

    async def die(*_a, **_k):
        raise RuntimeError("process died after the admission commit (injected)")
    o._write_managed_lineage = die
    with pytest.raises(RuntimeError):
        _tick(o, db, cid)
    rows = _cont_rows(db)
    assert len(rows) == 1 and rows[0]["lineage_state"] == "pending"
    cont_id = continuation_task_id(cid, 1)
    assert db.get_task(cont_id)["producer_turn_id"] == rows[0]["id"]  # linked in the same txn
    o2 = _fresh(o, monkeypatch)
    assert _tick(o2, db, cid) == 0
    assert len(_cont_rows(db)) == 1
    # lease expiry → the scheduler's lineage recovery repairs + activates it
    db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until = '2000-01-01T00:00:00' "
                       "WHERE id = ?", (rows[0]["id"],)).connection.commit()
    res = _pass(db, o2)
    assert res.lineage_recovered == 1 and res.activated == 1
    assert db.get_task(rows[0]["id"])["status"] == "pending"


# --------------------------------------------------------------------------- #
# Durable finalization (restart-safe), exactly-once round accounting
# --------------------------------------------------------------------------- #
def _drive_to_completion(db, o, cid, status="completed"):
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    assert _pass(db, o).activated == 1
    _complete(db, c, status=status)
    return c


def test_Q03_completed_wake_finalized_after_restart_counts_one_round(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    c = _drive_to_completion(db, o, cid)
    assert db.compute_continuation_tick(cid)["completed_rounds"] == 0  # not yet finalized
    o2 = _fresh(o, monkeypatch)  # the in-memory world that admitted it is gone
    assert _reconcile(o2, db) == 1
    token = db.get_task(continuation_task_id(cid, 1))
    assert token["status"] == "completed"
    res = json.loads(token["result"])
    assert res["consumed_task_ids"] == ["w1"] and res["turn_id"] == c and res["generation"] == 1
    tick = db.compute_continuation_tick(cid)
    assert tick["completed_rounds"] == 1 and not tick["satisfied"]
    assert len(_resolved_events(db, cid)) == 1
    assert "case_continuation_consumed" in o2.events
    # exactly once: re-running reconciles nothing, ticks admit nothing
    assert _reconcile(o2, db) == 0 and _tick(o2, db, cid) == 0
    assert db.compute_continuation_tick(cid)["completed_rounds"] == 1
    assert len(_cont_rows(db)) == 1


def test_Q03b_crash_between_group_resolution_and_token_finalize_converges(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    _drive_to_completion(db, o, cid)
    real = type(db).append_flow_event_once
    calls = {"n": 0}

    def crash_after(self, *a, **k):
        out = real(self, *a, **k)
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("died after the wait_resolved write (injected)")
        return out
    monkeypatch.setattr(type(db), "append_flow_event_once", crash_after)
    assert _reconcile(o, db) == 0
    assert db.get_task(continuation_task_id(cid, 1))["status"] == "claimed"
    assert len(_resolved_events(db, cid)) == 1
    assert _reconcile(o, db) == 1
    assert len(_resolved_events(db, cid)) == 1  # once
    assert db.compute_continuation_tick(cid)["completed_rounds"] == 1


def test_Q04_failed_wake_consumes_like_legacy(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    _drive_to_completion(db, o, cid, status="failed")
    assert _reconcile(o, db) == 1
    assert db.compute_continuation_tick(cid)["completed_rounds"] == 1


def test_Q05_operator_stopped_wake_rearms_without_a_round_and_respects_the_hold(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    _pass(db, o)
    tok = _run(db, c)
    assert o.stop_managed_session_turn(_sess())[0] is True  # REAL operator stop
    db.complete_turn(c, tok, {"success": False}, status="failed")  # interrupted result
    assert db.get_task(c)["status"] == "cancelled"
    assert _reconcile(o, db) == 1
    cont_id = continuation_task_id(cid, 1)
    token = db.get_task(cont_id)
    assert token["status"] == "pending" and not token["producer_turn_id"]
    assert json.loads(token["payload"])["attempt"] == 2
    assert db.compute_continuation_tick(cid)["completed_rounds"] == 0  # no round lost/counted
    # held: automation neither admits nor releases
    assert _tick(o, db, cid) == 0 and len(_cont_rows(db)) == 1
    assert db.operator_stop_hold("sess-1") == "operator_stop"
    # the operator's next send releases the hold; the wake is re-admitted (attempt 2)
    _submit(o, operation_id="op-after-stop")
    assert _tick(o, db, cid) == 1
    ids = [r["id"] for r in _cont_rows(db)]
    assert ids == [c, producer_turn_id(cont_id, "sess-1", 2)]


# --------------------------------------------------------------------------- #
# Activation-time revalidation / obsolete withdrawal
# --------------------------------------------------------------------------- #
def test_Q06_intervening_review_withdraws_the_queued_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _case(db)
    t = _running_operator_turn(db, o)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    # during the operator's turn the Manager reviews w1 out-of-band
    assert o.record_review(cid, verdict="accepted", task_id="w1")["ok"]
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": True})
    res = _pass(db, o)
    assert res.withdrawn == 1 and res.activated == 0
    assert db.get_task(c)["status"] == "withdrawn"
    audit = db.get_turn_revisions(c)
    assert any("scheduler:obsolete:reviewed" in str(a.get("actor")) for a in audit)
    assert _reconcile(o, db) == 1  # re-armed, not consumed
    assert db.compute_continuation_tick(cid)["completed_rounds"] == 0
    assert _tick(o, db, cid) == 0  # nothing left to present → retired, no paid turn
    assert len(_cont_rows(db)) == 1


def test_Q07_interrupt_withdraws_queued_automation_but_keeps_human_work(tmp_path, monkeypatch):
    """4b residual 4: queued managed AUTOMATION turns of a blocked Case are
    withdrawn at activation; a human instruction in the same Case still runs."""
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _case(db)
    t = _running_operator_turn(db, o)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    human = _submit(o, operation_id="op-human")  # attaches to the Manager's Case
    assert db.get_task(human)["flow_run_id"] == cid
    # a worker session with a queued automation dispatch joined to the Case
    db.upsert_session(Session(session_id="sess-2", backend="claude", repo_path="/tmp/repo",
                              status=SS.IDLE, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    db.enroll_session("sess-2")
    w = _submit(o, session_id="sess-2", source="automation_session",
                join_case_id=cid, operation_id="op-dispatch")
    assert db.get_task(w)["turn_source"] == "system" and db.get_task(w)["flow_run_id"] == cid
    # a runtime-principal (non-automation) system turn joined to the same Case
    db.upsert_session(Session(session_id="sess-3", backend="claude", repo_path="/tmp/repo",
                              status=SS.IDLE, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    db.enroll_session("sess-3")
    runtime = _submit(o, session_id="sess-3", source="runtime", join_case_id=cid,
                      operation_id="op-runtime")
    assert db.get_task(runtime)["turn_source"] == "system"
    assert db.get_task(runtime)["flow_run_id"] == cid
    out = asyncio.run(o.interrupt_case(cid))  # REAL kill path
    assert out["ok"] and db.get_flow_run(cid)["status"] == "blocked"
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": True})
    for _ in range(3):
        _pass(db, o)
    assert db.get_task(w)["status"] == "withdrawn"
    assert db.get_task(c)["status"] == "withdrawn"
    assert db.get_task(human)["status"] == "pending"  # humans never silently disappear
    assert db.get_task(runtime)["status"] == "pending"  # only the automation principal is withdrawn
    assert any("case_blocked" in str(a.get("actor")) for a in db.get_turn_revisions(w))


def test_Q07b_closed_case_withdraws_the_queued_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _case(db)
    t = _running_operator_turn(db, o)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    out = o.close_case(cid, outcome="cancelled", force=True)  # REAL close
    assert out.get("ok"), out
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": True})
    res = _pass(db, o)
    assert res.withdrawn == 1 and db.get_task(c)["status"] == "withdrawn"
    assert any("case_closed" in str(a.get("actor")) for a in db.get_turn_revisions(c))


def test_Q08_manager_rebinding_withdraws_the_stale_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _case(db)
    t = _running_operator_turn(db, o)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    db.upsert_session(Session(session_id="sess-9", backend="claude", repo_path="/tmp/repo",
                              status=SS.AWAITING_INPUT, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    db.create_flow_link(cid, "session", "sess-9", "manager", created_by="system")
    assert db.case_manager_session_id(cid) == "sess-9"
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": True})
    assert _pass(db, o).withdrawn == 1
    assert db.get_task(c)["status"] == "withdrawn"


def test_Q09_current_wake_is_not_withdrawn(tmp_path, monkeypatch):
    """Control for Q06-Q08: an unchanged Case activates its wake."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    assert _tick(o, db, cid) == 1
    res = _pass(db, o)
    assert res.activated == 1 and res.withdrawn == 0


# --------------------------------------------------------------------------- #
# DB contract: in-txn link, reaper exclusion, index use
# --------------------------------------------------------------------------- #
def test_Q10_link_failure_rolls_the_admission_back(tmp_path, monkeypatch):
    db, _o = _env(tmp_path, monkeypatch)
    db.enqueue_task("cont:x:1", session_id=None, machine_id="__manager_continuation__",
                    backend="claude", action="manager_continuation", payload={})
    db.record_continuation_consumed("x", "cont:x:1", 1, [])
    with pytest.raises(tq.OwnershipConflictError):
        db.enqueue_turn(session_id="sess-1", body="wake", turn_kind="continuation",
                        operation_id="cont:x:1#1", producer_token="cont:x:1",
                        require_enrolled=True)
    assert _managed_rows(db) == []
    with pytest.raises(tq.TurnNotFoundError):
        db.enqueue_turn(session_id="sess-1", body="wake", operation_id="k2",
                        producer_token="cont:missing:1", require_enrolled=True)
    assert _managed_rows(db) == []


def test_Q11_linked_token_is_invisible_to_the_stale_claim_reaper(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    assert _tick(o, db, cid) == 1
    stale = db.list_stale_claims(lease_sec=-1)
    assert continuation_task_id(cid, 1) not in [r["id"] for r in stale]


def test_Q12_reconcile_and_link_lookup_use_the_partial_index(tmp_path, monkeypatch):
    db, _o = _env(tmp_path, monkeypatch)
    plan = " ".join(str(tuple(r)) for r in db._conn().execute(
        "EXPLAIN QUERY PLAN SELECT t.id FROM mesh_tasks t INDEXED BY idx_mesh_tasks_producer_link "
        "JOIN mesh_tasks x ON x.id = t.producer_turn_id WHERE t.producer_turn_id IS NOT NULL "
        "AND t.status = 'claimed' AND t.action = 'manager_continuation' "
        "AND x.status IN ('completed') LIMIT 25").fetchall())
    assert "idx_mesh_tasks_producer_link" in plan
    assert db.reconcile_finalizers() == []  # the INDEXED BY query itself is valid


# --------------------------------------------------------------------------- #
# Unenrolled / nothing enrolled: byte-identical legacy path (all-thread trace)
# --------------------------------------------------------------------------- #
def test_Q13_unenrolled_wake_and_finalizer_touch_no_managed_state(tmp_path, monkeypatch):
    import inspect

    from src.orchestrator import TaskOrchestrator
    from tests.test_case_transient_resume import _Orch

    class _Auto(_Orch):
        def __getattr__(self, name):
            static = inspect.getattr_static(TaskOrchestrator, name)
            if isinstance(static, (staticmethod, classmethod)):
                return getattr(TaskOrchestrator, name)
            attr = getattr(TaskOrchestrator, name)
            return attr.__get__(self) if callable(attr) else attr

    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")
    db, o = _env(tmp_path, monkeypatch, enroll=False)
    cid = _case(db)
    stmts, threads = [], set()
    import threading as _th
    real_conn = type(db)._conn

    def traced(self):
        c = real_conn(self)
        c.set_trace_callback(lambda sql: (stmts.append(sql), threads.add(_th.get_ident())))
        return c
    monkeypatch.setattr(type(db), "_conn", traced)
    auto = _Auto(o.session_store)
    assert asyncio.run(auto._continue_case_once(db, cid)) == 1
    assert asyncio.run(auto._reconcile_continuation_finalizers(db)) == 0
    monkeypatch.setattr(type(db), "_conn", real_conn)
    assert stmts and len(threads) >= 2  # the offloaded reads were traced too
    forbidden = ("producer_turn_id", "turn_queue_enrolled", "queue_protocol = 1",
                 "idx_mesh_tasks_producer_link", "turn_queue_hold",
                 # the rework's rebound-wake probe reads the token row by id;
                 # legacy never does (it only INSERTs / claims it)
                 "SELECT * FROM mesh_tasks WHERE id = 'cont:")  # trace sees bound values
    assert not [q for q in stmts if any(f in q for f in forbidden)]
    # the legacy delivery happened exactly as before
    assert [d["source"] for d in auto.deliveries] == ["manager_continuation"]
    assert _managed_rows(db) == []
    token = db.get_task(continuation_task_id(cid, 1))
    assert token["status"] == "claimed" and token["claimed_at"] and not token["producer_turn_id"]


def test_Q10b_stale_attempt_replay_cannot_relink_a_terminal_turn(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]
    cont_id = continuation_task_id(cid, 1)
    db.withdraw_turn(c["id"], int(c["revision"]), actor="test")
    assert _reconcile(o, db) == 1  # re-armed → attempt 2, unlinked
    with pytest.raises(tq.OwnershipConflictError):
        db.enqueue_turn(session_id="sess-1", body=c["prompt"], turn_source="system",
                        turn_kind="continuation", idempotency_scope=c["idempotency_scope"],
                        operation_id=c["idempotency_key"], admission_hash=c["admission_hash"],
                        producer_token=cont_id, require_enrolled=True)
    token = db.get_task(cont_id)
    assert token["status"] == "pending" and not token["producer_turn_id"]


def test_Q14_wake_dispatcher_tick_finalizes_before_evaluating(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    _drive_to_completion(db, o, cid)
    o2 = _fresh(o, monkeypatch)
    assert asyncio.run(o2._wake_dispatcher_tick_once()) == 0  # finalized, nothing new to wake
    assert db.get_task(continuation_task_id(cid, 1))["status"] == "completed"
    assert db.compute_continuation_tick(cid)["completed_rounds"] == 1
    # a NEW finish ⇒ round 2 through the same tick
    db.arm_wait_group(cid, "g2", "ALL", ["w2"])
    _finish(db, cid, "w2")
    assert asyncio.run(o2._wake_dispatcher_tick_once()) == 1
    assert [r["idempotency_key"] for r in _cont_rows(db)][-1] == f"{continuation_task_id(cid, 2)}#1"


def test_Q15_finalizer_cas_is_fenced_to_the_linked_turn(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    assert _tick(o, db, cid) == 1
    cont_id = continuation_task_id(cid, 1)
    payload = json.loads(db.get_task(cont_id)["payload"])
    assert db._finalize_producer_token(cont_id, "cturn_stale", "completed", payload) is None
    assert db._finalize_producer_token(cont_id, "cturn_stale", "withdrawn", payload) is None
    token = db.get_task(cont_id)
    assert token["status"] == "claimed" and token["producer_turn_id"] == _cont_rows(db)[0]["id"]


def test_Q16_admission_racing_a_stop_never_releases_or_runs_through_the_hold(tmp_path, monkeypatch):
    """The automation principal: a wake admitted into a session that got held
    between the tick's hold check and admission keeps the hold and waits."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    t = _running_operator_turn(db, o)
    assert o.stop_managed_session_turn(_sess())[0] is True
    tick = db.compute_continuation_tick(cid)
    assert asyncio.run(o._continue_case_managed(db, cid, _sess(), 1, tick)) == 1
    assert db.operator_stop_hold("sess-1") == "operator_stop"
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": False}, status="failed")
    assert _pass(db, o).activated == 0
    assert _cont_rows(db)[0]["status"] == "queued"


# --------------------------------------------------------------------------- #
# Stage 4c rework (A87 review): adopted probes P1/P2/P3b/P5/P6 + kill tests
# --------------------------------------------------------------------------- #
def _rebind_setup(tmp_path, monkeypatch, *, enroll_new):
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _case(db)
    t = _running_operator_turn(db, o)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    assert o.stop_managed_session_turn(_sess())[0] is True  # S1 operator-held
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": False}, status="failed")
    _pass(db, o)
    assert db.get_task(c)["status"] == "queued"  # wedged behind the hold
    s9 = Session(session_id="sess-9", backend="claude", repo_path="/tmp/repo",
                 status=SS.AWAITING_INPUT, created_at=NOW, updated_at=NOW, machine_id="worker-a")
    db.upsert_session(s9)
    o.session_store.save(s9)
    if enroll_new:
        db.enroll_session("sess-9")
    db.create_flow_link(cid, "session", "sess-9", "manager", created_by="system")
    return db, o, cid, c


def test_Q17_rebound_manager_is_woken_while_the_old_one_is_held_legacy(tmp_path, monkeypatch):
    """P1 adopted: the Case is rebound to an UNENROLLED S2 while S1 is held —
    S2 is woken on the next tick (legacy parity), S1's hold is untouched."""
    db, o, cid, c = _rebind_setup(tmp_path, monkeypatch, enroll_new=False)
    delivered = []

    async def spy(**kw):
        delivered.append(kw)
        return "legacy-x"

    async def no_finalize(*_a, **_k):
        return None
    monkeypatch.setattr(o, "submit_instruction", spy)
    monkeypatch.setattr(o, "_finalize_continuation", no_finalize)
    outs = [_tick(o, db, cid) for _ in range(3)]
    assert outs[0] == 1 and sum(outs) == 1
    assert [d["session_id"] for d in delivered] == ["sess-9"]
    assert db.get_task(c)["status"] == "withdrawn"
    assert any("manager_rebound" in str(a.get("actor")) for a in db.get_turn_revisions(c))
    assert db.operator_stop_hold("sess-1") == "operator_stop"
    tokrow = db.get_task(continuation_task_id(cid, 1))
    assert tokrow["status"] == "claimed" and not tokrow["producer_turn_id"]  # legacy lease


def test_Q17b_rebound_enrolled_manager_gets_the_managed_wake(tmp_path, monkeypatch):
    db, o, cid, c = _rebind_setup(tmp_path, monkeypatch, enroll_new=True)
    assert _tick(o, db, cid) == 1
    rows = {r["session_id"]: r for r in _cont_rows(db)}
    assert set(rows) == {"sess-1", "sess-9"}
    assert rows["sess-1"]["status"] == "withdrawn" and rows["sess-9"]["status"] == "queued"
    assert rows["sess-9"]["id"] == producer_turn_id(continuation_task_id(cid, 1), "sess-9", 2)
    assert db.operator_stop_hold("sess-1") == "operator_stop"
    assert _tick(o, db, cid) == 0 and len(_cont_rows(db)) == 2


def test_Q17c_running_wake_on_the_old_manager_is_not_withdrawn(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    _pass(db, o)
    _run(db, c)
    db.upsert_session(Session(session_id="sess-9", backend="claude", repo_path="/tmp/repo",
                              status=SS.AWAITING_INPUT, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    db.create_flow_link(cid, "session", "sess-9", "manager", created_by="system")
    assert asyncio.run(o._withdraw_rebound_continuation(db, cid, 1, "sess-9")) is False
    assert db.get_task(c)["status"] == "running"


def test_Q18_multicase_manager_wake_lineage_pinned_to_the_woken_case(tmp_path, monkeypatch):
    """P2 adopted: Manager on open Cases A and B; the wake for A attaches to A
    only and does not move the session's current Case."""
    db, o = _env(tmp_path, monkeypatch)
    cid_a = _case(db)
    cid_b = db.open_case("other", "sess-1", role="manager", completion_criteria='{"round_cap": 5}')
    before = (db.get_session("sess-1") or {}).get("current_case_id")
    assert _tick(o, db, cid_a) == 1
    c = _cont_rows(db)[0]
    assert c["flow_run_id"] == cid_a

    def attached(cid):
        return [e for e in db.list_flow_events(cid)
                if e["event_type"] == "task.attached" and e.get("entity_id") == c["id"]]
    assert len(attached(cid_a)) == 1 and attached(cid_b) == []
    assert not db.list_flow_links(flow_run_id=cid_b, entity_type="task", entity_id=c["id"])
    assert (db.get_session("sess-1") or {}).get("current_case_id") == before
    _pass(db, o)
    _complete(db, c["id"])
    _reconcile(o, db)
    assert db.compute_continuation_tick(cid_a)["completed_rounds"] == 1
    assert db.compute_continuation_tick(cid_b)["completed_rounds"] == 0


def test_Q18b_pinned_lineage_recovery_converges_on_the_woken_case(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid_a = _case(db)
    cid_b = db.open_case("other", "sess-1", role="manager", completion_criteria='{"round_cap": 5}')

    async def die(*_a, **_k):
        raise RuntimeError("died before lineage (injected)")
    o._write_managed_lineage = die
    with pytest.raises(RuntimeError):
        _tick(o, db, cid_a)
    c = _cont_rows(db)[0]["id"]
    db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until = '2000-01-01T00:00:00' "
                       "WHERE id = ?", (c,)).connection.commit()
    o2 = _fresh(o, monkeypatch)
    assert _pass(db, o2).lineage_recovered == 1
    assert db.get_task(c)["flow_run_id"] == cid_a
    assert not db.list_flow_links(flow_run_id=cid_b, entity_type="task", entity_id=c)


def test_Q19_finalizer_appends_nothing_after_flow_closed(tmp_path, monkeypatch):
    """P3b adopted: the Manager closes the Case during its wake turn."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    _pass(db, o)
    tok = _run(db, c)
    assert o.close_case(cid, outcome="cancelled", force=True).get("ok")
    db.complete_turn(c, tok, {"success": True})
    assert _reconcile(o, db) == 1
    evs = db.list_flow_events(cid)
    closed_at = max(i for i, e in enumerate(evs)
                    if e["event_type"] in ("flow.closed", "flow.status_changed"))
    assert [e["event_type"] for e in evs[closed_at + 1:]] == []
    assert db.get_task(continuation_task_id(cid, 1))["status"] == "completed"


def test_Q20_garbled_token_payload_converges(tmp_path, monkeypatch):
    """P5 adopted: a garbled counter never replays the terminal attempt-1 turn."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    _pass(db, o)
    tok = _run(db, c)
    o.stop_managed_session_turn(_sess())
    db.complete_turn(c, tok, {"success": False}, status="failed")
    assert _reconcile(o, db) == 1
    _submit(o, operation_id="rel")  # operator releases the hold
    cont = continuation_task_id(cid, 1)
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET payload='not json' WHERE id=?", (cont,))
    assert [_tick(o, db, cid) for _ in range(2)] == [1, 0]
    token = db.get_task(cont)
    new = _cont_rows(db)[-1]["id"]
    assert token["status"] == "claimed" and token["producer_turn_id"] == new
    assert new == producer_turn_id(cont, "sess-1", 2)
    assert json.loads(token["payload"])["case_id"] == cid  # self-healed on link


def test_Q21_rearm_links_the_fresh_presented_set(tmp_path, monkeypatch):
    """P6 adopted (kills MA): after a re-arm the NEW tick's presented/retired
    lists win over the stale ones stored at the first link."""
    db, o = _env(tmp_path, monkeypatch)
    cid = db.open_case("x", "sess-1", role="manager", completion_criteria='{"round_cap": 5}')
    db.arm_wait_group(cid, "g1", "ANY", ["w1", "w2"])
    _finish(db, cid, "w1")
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    _pass(db, o)
    tok = _run(db, c)
    o.stop_managed_session_turn(_sess())
    db.complete_turn(c, tok, {"success": False}, status="failed")
    _reconcile(o, db)
    _finish(db, cid, "w2")
    _submit(o, operation_id="rel")
    rel = [r for r in _managed_rows(db) if r["turn_kind"] != "continuation"][-1]["id"]
    _pass(db, o)
    t2 = _run(db, rel)
    db.complete_turn(rel, t2, {"success": True})
    assert _tick(o, db, cid) == 1
    c2 = _cont_rows(db)[-1]["id"]
    _pass(db, o)
    _complete(db, c2)
    assert _reconcile(o, db) == 1
    res = json.loads(db.get_task(continuation_task_id(cid, 1))["result"])
    assert sorted(res["consumed_task_ids"]) == ["w1", "w2"]
    assert not db.compute_continuation_tick(cid)["satisfied"]
    assert len(_resolved_events(db, cid)) == 1  # the fresh retire list (ANY fully finished)


def test_Q22_node_offline_wake_consumes_like_legacy(tmp_path, monkeypatch):
    """Kills MB (carried m4: legacy parity — consumed even if never run)."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    _drive_to_completion(db, o, cid, status="failed_node_offline")
    assert _reconcile(o, db) == 1
    assert db.compute_continuation_tick(cid)["completed_rounds"] == 1


def test_Q23_partial_review_does_not_withdraw_the_wake(tmp_path, monkeypatch):
    """Kills MC: the Manager reviewed w1 but w2 is still unresolved ⇒ activate."""
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _case(db, members=("w1", "w2"), finished=("w1", "w2"))
    t = _running_operator_turn(db, o)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    assert o.record_review(cid, verdict="accepted", task_id="w1")["ok"]
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": True})
    res = _pass(db, o)
    assert res.withdrawn == 0 and res.activated == 1
    assert db.get_task(c)["status"] == "pending"
