"""A82 Stage 4b — producer 2: compaction, operator cancel/stop of the ACTIVE
managed turn, and session close with managed rows (gateway side).

Real pieces: a file-backed ``MeshDB`` as ``get_db()``, REAL bound orchestrator
methods on a bare instance, the REAL ``SessionService`` (close via its public
``close_session``), the REAL control API app (TestClient) for the web routes, and
the REAL scheduler pass / Case ``close_case``. No backend/CLI (autouse spawn
guard imported from the producer-1 suite).
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control import turn_scheduler as ts
from src.core.interfaces import ExecutionResult, SessionStatus
from src.orchestrator import TaskOrchestrator
from tests.test_turn_queue_producer1 import (  # noqa: F401
    _client, _flags, _managed_rows, _no_cli_spawn, _setup, _submit, _sess,
)

PAST = (datetime.now(tz=timezone.utc) - timedelta(seconds=1)).isoformat()


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch):
    """The shared legacy+managed allowance is process-global: isolate it so
    this file's admissions never leak into other suites' capacity."""
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())

class _GatewayBackend:
    """Gateway-local backend stand-in: records legacy close/compact calls (a
    managed session must never reach them)."""

    def __init__(self) -> None:
        self.closed = []
        self.compacted = []

    def close(self, session) -> None:
        self.closed.append(session.session_id)

    def compact_session(self, session):
        self.compacted.append(session.session_id)
        return ExecutionResult(success=True, output="legacy compacted", errors=[])

    def cancel(self, session) -> None:  # legacy cancel_task path
        pass


def _wire(o):
    from src.services.session_service import SessionService

    o._task_cancel_events = {}
    o._running_exec_tasks = {}
    o._backends = {"claude": _GatewayBackend()}
    o.session_service = SessionService(
        o.session_store, repo_path_validator=lambda _p: None,
        remote_close_dispatcher=o._dispatch_remote_close,
        managed_close=o._close_managed_session,
    )
    return o


def _pass(db, o):
    return asyncio.run(ts.run_scheduler_pass(
        db, o._prepare_managed_turn, allowance=ta.SharedWaitingAllowance(),
        recover_lineage=o._recover_managed_lineage,
        void_lineage=o._void_withdrawn_lineage_async,
    ))


def _run(db, tid, node="worker-a"):
    tok = db.claim_turn(tid, node, "worker_daemon", "inc-1")
    db.start_turn(tid, tok, incarnation_id="inc-1")
    return tok


def _control_rows(db, action):
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE action = ? ORDER BY created_at", (action,)).fetchall()]


def _with_native(db, native="native-1"):
    db.update_session_fields("sess-1", native_session_id=native)


# --------------------------------------------------------------------------- #
# Operator cancel of the ACTIVE turn (token-fenced)
# --------------------------------------------------------------------------- #
def test_C01_cancel_pending_unclaimed_is_terminal_and_queued_untouched(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, description="one", operation_id="a")
    t2 = _submit(o, description="two", operation_id="b")
    _pass(db, o)
    assert db.get_task(t1)["status"] == "pending"
    assert o.cancel_task(t1) is True
    r1 = db.get_task(t1)
    assert r1["status"] == "cancelled" and r1["completed_at"]
    assert db.get_task(t2)["status"] == "queued"  # the turn behind it is untouched
    assert db.get_active_turn("sess-1") is None
    with pytest.raises(tq.TurnQueueError):
        db.claim_turn(t1, "worker-a", "worker_daemon", "inc-1")
    _pass(db, o)
    assert db.get_task(t2)["status"] == "pending"  # next head runs normally
    assert o.cancel_task(t1) is False  # already terminal: idempotent no-op


def test_C02_cancel_claimed_not_started_refuses_the_late_start(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    tok = db.claim_turn(t1, "worker-a", "worker_daemon", "inc-1")
    assert o.cancel_task(t1) is True
    assert db.get_task(t1)["status"] == "cancelled"
    with pytest.raises(tq.TurnQueueError):
        db.start_turn(t1, tok, incarnation_id="inc-1")
    assert _control_rows(db, "cancel_managed") == []  # nothing ran: no carrier signal


def test_C03_cancel_running_is_fenced_to_the_attempt_and_idempotent(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    t2 = _submit(o, operation_id="b")
    _pass(db, o)
    tok = _run(db, t1)
    assert o.cancel_task(t1) is True
    assert o.cancel_task(t1) is True  # repeat converges on the same control row
    row = db.get_task(t1)
    assert row["status"] == "running" and row["cancel_token"] == tok
    [ctl] = _control_rows(db, "cancel_managed")
    assert ctl["queue_protocol"] == 0 and ctl["status"] == "pending"
    assert ctl["machine_id"] == "worker-a"  # pinned to the claiming carrier
    payload = json.loads(ctl["payload"])
    assert payload["target_task_id"] == t1 and tok not in ctl["payload"]
    assert db.get_task(t2)["status"] == "queued"
    # The attempt's own interrupted/failed result is truthfully `cancelled`.
    out = db.complete_turn(t1, tok, {"success": False, "output": ""}, status="failed")
    assert out.status == "cancelled" and db.get_task(t1)["status"] == "cancelled"


def test_C03b_turn_that_finished_before_the_interrupt_stays_completed(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    tok = _run(db, t1)
    o.cancel_task(t1)
    out = db.complete_turn(t1, tok, {"success": True, "output": "done"}, status="completed")
    assert out.status == "completed"


def test_C04_cancelled_attempt_released_not_invoked_is_never_reoffered(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    tok = _run(db, t1)
    o.cancel_task(t1)
    # The carrier's managed_conflict / reconciler exit: released as not-invoked.
    assert db.release_turn(t1, tok, backend_not_invoked=True, node_id="worker-a") is True
    assert db.get_task(t1)["status"] == "cancelled"
    assert db.get_pending_managed_turns(node_id="worker-a", backends=["claude"]) == []


def test_C04b_uncancelled_not_invoked_release_still_returns_to_pending(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    tok = _run(db, t1)
    assert db.release_turn(t1, tok, backend_not_invoked=True, node_id="worker-a") is True
    assert db.get_task(t1)["status"] == "pending"


def test_C05_recovery_hold_with_cancel_resolves_cancelled(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    tok = _run(db, t1)
    assert db.enter_recovery(t1, tok, "managed_turn_deadline")
    assert o.cancel_task(t1) is True
    assert len(_control_rows(db, "cancel_managed")) == 1
    res = db.resolve_recovery(
        t1, tok, {"quiescent": True, "terminal": True, "source": "carrier",
                  "stop_evidence": "backend_quiescent", "task_id": t1},
        resolved_status="failed",
    )
    assert res.resolved_status == "cancelled" and db.get_task(t1)["status"] == "cancelled"


def test_C06_web_stop_cancels_the_slot_holder_not_last_task_id(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t0 = _submit(o, operation_id="z")
    _pass(db, o)
    tok0 = _run(db, t0)
    db.complete_turn(t0, tok0, {"success": True}, status="completed", native_session_id="n0")
    assert _sess().last_task_id == t0
    t1 = _submit(o, operation_id="a")
    t2 = _submit(o, operation_id="b")
    _pass(db, o)
    _run(db, t1)
    c = _client(monkeypatch, o)
    r = c.post("/api/sessions/sess-1/stop", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.json() == {"ok": True, "cancelled": True, "task_id": t1}
    assert db.get_task(t0)["status"] == "completed"
    assert db.get_task(t2)["status"] == "queued"
    assert len(_control_rows(db, "cancel_managed")) == 1
    s = _sess()
    # [rework] stop holds the session like legacy (CANCELLED, field-scoped in
    # the cancel txn); no whole-session save (last_task_id untouched).
    assert s.status == SessionStatus.CANCELLED and s.last_task_id == t0


def test_C06b_stop_with_no_active_turn_cancels_nothing(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")  # queued, never activated
    assert o.stop_managed_session_turn(_sess()) == (False, None)
    assert db.get_task(t1)["status"] == "queued"


def test_C06c_stop_ledger_unreadable_fails_closed_503(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    monkeypatch.setattr(db, "is_session_enrolled",
                        lambda sid: (_ for _ in ()).throw(RuntimeError("malformed")))
    c = _client(monkeypatch, o)
    r = c.post("/api/sessions/sess-1/stop", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 503


def test_C07_unenrolled_cancel_stop_compact_close_touch_no_queue_state(tmp_path, monkeypatch):
    """Legacy byte-identity: with nothing enrolled, none of the producer-2
    entry points reads the enrollment marker or the managed ledger, and each
    takes exactly the legacy branch."""
    import threading as _th

    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    _wire(o)
    _with_native(db)
    # [rework] Trace EVERY thread's connection (`_conn()` is per-thread; the
    # web routes run on portal/threadpool threads) — reviewer probe P2.
    stmts = []
    lock = _th.Lock()
    real_conn = type(db)._conn

    def traced(self):
        c = real_conn(self)

        def cb(sql):
            with lock:
                stmts.append((_th.current_thread().name, sql))
        c.set_trace_callback(cb)
        return c
    monkeypatch.setattr(type(db), "_conn", traced)
    assert o.cancel_task("task_unknown") is False
    c = _client(monkeypatch, o)
    r = c.post("/api/sessions/sess-1/stop", headers={"Authorization": "Bearer tok"})
    assert r.json() == {"ok": True, "cancelled": False, "task_id": _sess().last_task_id}
    r = c.post("/api/sessions/sess-1/compact", headers={"Authorization": "Bearer tok"})
    assert r.json() == {"ok": True, "output": "legacy compacted", "errors": []}
    r = c.post("/api/sessions/sess-1/close", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(type(db), "_conn", real_conn)
    assert o._backends["claude"].compacted == ["sess-1"]
    assert len({t for t, _ in stmts}) >= 2  # the route threads were traced too
    bad = [sql for _, sql in stmts if "INSERT INTO sessions" not in sql and (
        "turn_queue_enrolled" in sql or "queue_protocol" in sql or "cancel_managed" in sql
        or "cancel_token" in sql or "lineage" in sql or "task_unknown" in sql)]
    assert bad == [], bad
    assert _managed_rows(db) == [] and _control_rows(db, "cancel_managed") == []


# --------------------------------------------------------------------------- #
# Compaction as a managed, serialized turn
# --------------------------------------------------------------------------- #
def test_K01_enrolled_compaction_is_one_durable_managed_turn(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    _with_native(db)
    res = asyncio.run(o.compact_session("sess-1", operation_id="k1"))
    assert res.success and res.parsed_output["managed"] is True
    tid = res.parsed_output["task_id"]
    row = db.get_task(tid)
    assert row["queue_protocol"] == 1 and row["status"] == "queued"
    assert row["turn_kind"] == "compaction" and row["action"] == "compact_session"
    assert row["prompt"] == "/compact" and row["turn_source"] == "operator"
    assert row["lineage_state"] is None and row["flow_run_id"] is None
    assert row["idempotency_scope"] == "operator:sess-1:compaction"
    assert row["idempotency_key"] == "k1"
    assert o._backends["claude"].compacted == []  # never the direct backend call
    replay = asyncio.run(o.compact_session("sess-1", operation_id="k1"))
    assert replay.parsed_output["task_id"] == tid
    coalesced = asyncio.run(o.compact_session("sess-1"))
    assert coalesced.parsed_output["task_id"] == tid and coalesced.parsed_output["coalesced"]
    assert [r["id"] for r in _managed_rows(db)] == [tid]


def test_K01b_web_compact_route_returns_queued_envelope(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    _with_native(db)
    c = _client(monkeypatch, o)
    r = c.post("/api/sessions/sess-1/compact",
               headers={"Authorization": "Bearer tok", "Idempotency-Key": "web-k"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["queued"] is True
    assert db.get_task(body["task_id"])["idempotency_key"] == "web-k"


def test_K02_compaction_waits_for_the_slot_and_keeps_its_bare_command(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    _with_native(db)

    async def inject(task):  # stands in for restart/compact context injection
        task.prompt = "<prior_context>x</prior_context>\n" + (task.prompt or "")
    monkeypatch.setattr(o, "_maybe_inject_restart_recovery_context", inject, raising=False)
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    tok = _run(db, t1)
    k = asyncio.run(o.compact_session("sess-1")).parsed_output["task_id"]
    _pass(db, o)
    assert db.get_task(k)["status"] == "queued"  # never concurrent with the running turn
    db.complete_turn(t1, tok, {"success": True}, status="completed", native_session_id="n1")
    _pass(db, o)
    row = db.get_task(k)
    payload = json.loads(row["payload"])
    assert row["status"] == "pending" and row["action"] == "compact_session"
    assert payload["action"] == "compact_session" and payload["prompt"] == "/compact"
    assert payload["session"]["backend_session_id"] == "n1"
    assert json.loads(db.get_task(t1)["payload"])["prompt"].startswith("<prior_context>")


def test_K03_compaction_refused_without_a_managed_carrier(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch, machine="nobody")
    _wire(o)
    _with_native(db)
    with pytest.raises(tq.CarrierUnavailableError):
        asyncio.run(o.compact_session("sess-1"))
    assert _managed_rows(db) == []


# --------------------------------------------------------------------------- #
# Session close with managed rows
# --------------------------------------------------------------------------- #
def test_L01_close_withdraws_queued_cancels_active_and_tears_down_on_carrier(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    _with_native(db, "native-1")
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    tok = _run(db, t1)
    t2 = _submit(o, operation_id="b")
    t3 = _submit(o, operation_id="c")
    res = o.session_service.close_session("sess-1", backends=o._backends)
    assert res.ok
    s = _sess()
    assert s.status == SessionStatus.CLOSED and s.backend_session_id == ""
    assert "native-1" in (s.previous_backend_session_ids or [])
    for t in (t2, t3):
        r = db.get_task(t)
        assert r["status"] == "withdrawn"
        assert [v["change_kind"] for v in db.get_turn_revisions(t)][-1] == "withdraw"
    assert db.get_task(t1)["status"] == "running" and db.get_task(t1)["cancel_token"] == tok
    [ctl] = _control_rows(db, "cancel_managed")
    [cls] = _control_rows(db, "close_session")
    assert ctl["machine_id"] == "worker-a" and cls["machine_id"] == "worker-a"
    assert o._backends["claude"].closed == []  # never the gateway-local backend
    # Admission and activation refuse the closed session.
    with pytest.raises(tq.TurnQueueError):
        _submit(o, operation_id="late")
    # The cancelled attempt's late result cannot resurrect the native id.
    out = db.complete_turn(t1, tok, {"success": False}, status="failed", native_session_id="native-2")
    assert out.status == "cancelled"
    assert _sess().backend_session_id == ""
    assert db.get_active_turn("sess-1") is None
    # Idempotent re-close.
    assert o.session_service.close_session("sess-1", backends=o._backends).ok
    assert len(_control_rows(db, "cancel_managed")) == 1


def test_L01b_close_pending_active_turn_is_cancelled_directly(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    assert o.session_service.close_session("sess-1", backends=o._backends).ok
    assert db.get_task(t1)["status"] == "cancelled"
    assert _control_rows(db, "cancel_managed") == []
    assert len(_control_rows(db, "close_session")) == 1


def test_L01c_close_ledger_failure_fails_closed_and_retry_converges(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    real = db.close_session_turns
    monkeypatch.setattr(db, "close_session_turns",
                        lambda *a, **k: (_ for _ in ()).throw(tq.BackingStoreError("locked")))
    res = o.session_service.close_session("sess-1", backends=o._backends)
    assert not res.ok and res.reason == "turn_queue_unavailable"
    assert _sess().status != SessionStatus.CLOSED and db.get_task(t1)["status"] == "queued"
    monkeypatch.setattr(db, "close_session_turns", real)
    assert o.session_service.close_session("sess-1", backends=o._backends).ok
    assert db.get_task(t1)["status"] == "withdrawn"


def test_L01d_web_close_route_maps_ledger_failure_to_503(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    monkeypatch.setattr(db, "close_session_turns",
                        lambda *a, **k: (_ for _ in ()).throw(tq.BackingStoreError("locked")))
    c = _client(monkeypatch, o)
    r = c.post("/api/sessions/sess-1/close", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 503


def _birth(o, op, parent):
    return _submit(o, operation_id=op, source="runtime", parent_flow_run_id=parent)


def test_L02_close_voids_a_withdrawn_birth_child_case(tmp_path, monkeypatch):
    """Stage-6 precondition (4a carry) implemented for close: a withdrawn turn's
    born child Case is closed cancelled, its affiliation cleared, the parent is
    closable, and the voided dispatch is not counted by the advancement gate."""
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    monkeypatch.setenv("MANAGER_ADVANCEMENT_GATE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    parent = db.open_case(objective="p", session_id="mgr-x", role="manager")
    ta_ = _birth(o, "ran", parent)
    _pass(db, o)
    tok = _run(db, ta_)
    db.complete_turn(ta_, tok, {"success": True}, status="completed")
    child_a = db.get_task(ta_)["flow_run_id"]
    assert o.close_case(child_a, outcome="closed", actor="operator")["ok"]
    tb = _birth(o, "never-ran", parent)
    child_b = db.get_task(tb)["flow_run_id"]
    assert child_b and child_b != child_a
    assert _sess().current_case_id == child_b
    blocked = o.close_case(parent, outcome="closed", actor="operator")
    assert not blocked["ok"]  # the open child blocks the parent
    assert o.session_service.close_session("sess-1", backends=o._backends).ok
    assert db.get_flow_run(child_b)["status"] == "cancelled"
    assert _sess().current_case_id is None
    assert db.get_task(tb)["lineage_state"] == "voided"
    voided = [e for e in db.list_flow_events(parent) if e["event_type"] == "task.dispatch_voided"]
    assert [e["entity_id"] for e in voided] == [child_b]
    # Two dispatches in the ledger, one voided ⇒ not advanced.
    gate = o.close_case(parent, outcome="closed", actor="manager",
                        continuation_plan="next: A99")
    assert not gate["ok"] and "advancement gate" in gate["reason"]
    assert o.close_case(parent, outcome="closed", actor="operator")["ok"]


def test_L03_pending_lineage_with_live_writer_is_voided_after_its_lease(tmp_path, monkeypatch):
    """A lineage writer that crashed mid-birth still holds its lease at close:
    the void waits for the lease (the writer could still be mid-write), then the
    scheduler sweep voids it — convergent, each event once."""
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    parent = db.open_case(objective="p", session_id="mgr-x", role="manager")
    real = o._record_flow_event

    def crash(fid, et, *a, **k):
        if et == "task.dispatched":
            raise SystemExit("crash mid-lineage")
        return real(fid, et, *a, **k)
    monkeypatch.setattr(o, "_record_flow_event", crash)
    with pytest.raises(SystemExit):
        _birth(o, "b", parent)
    monkeypatch.setattr(o, "_record_flow_event", real)
    [row] = _managed_rows(db)
    tid = row["id"]
    child = db.existing_task_lineage(tid)["flow_run_id"]
    assert row["lineage_state"] == "pending"
    assert o.session_service.close_session("sess-1", backends=o._backends).ok
    r = db.get_task(tid)
    assert r["status"] == "withdrawn" and r["lineage_state"] == "void"
    assert db.get_flow_run(child)["status"] not in db._CLOSED_STATUSES  # deferred
    res = _pass(db, o)
    assert res.void_outstanding == 1 and res.lineage_voided == 0
    # The stale writer finishes its last write group after the close …
    real(parent, "task.dispatched", "system", entity_type="flow", entity_id=child,
         payload={"child_task_id": tid}, strict=True, once=True)
    # … its lease expires; the sweep voids the lineage.
    db._conn().execute("UPDATE mesh_tasks SET lineage_lease_until=? WHERE id=?", (PAST, tid))
    res = _pass(db, o)
    assert res.lineage_voided == 1 and res.void_outstanding == 0
    assert db.get_flow_run(child)["status"] == "cancelled"
    assert db.get_task(tid)["lineage_state"] == "voided"
    assert _sess().current_case_id is None
    _pass(db, o)
    evs = [e["event_type"] for e in db.list_flow_events(parent)]
    assert evs.count("task.dispatch_voided") == 1 and evs.count("task.dispatched") == 1
    assert o.close_case(parent, outcome="closed", actor="operator")["ok"]


def test_L03b_void_crash_midway_rerun_converges(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    parent = db.open_case(objective="p", session_id="mgr-x", role="manager")
    tb = _birth(o, "b", parent)
    child = db.get_task(tb)["flow_run_id"]
    real_close = o.close_case
    monkeypatch.setattr(o, "close_case",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash in void")))
    assert o.session_service.close_session("sess-1", backends=o._backends).ok
    assert db.get_task(tb)["lineage_state"] == "void"  # left for the sweep
    monkeypatch.setattr(o, "close_case", real_close)
    res = _pass(db, o)
    assert res.lineage_voided == 1
    assert db.get_flow_run(child)["status"] == "cancelled"
    evs = [e["event_type"] for e in db.list_flow_events(parent)]
    assert evs.count("task.dispatch_voided") == 1


def test_L04_close_keeps_join_membership_like_legacy(tmp_path, monkeypatch):
    """Join/attach lineage of a withdrawn turn is left as written (legacy close
    parity: close never clears a Case membership/affiliation)."""
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    tid = _submit(o, join_case_id=case_id, operation_id="j", source="runtime")
    assert o.session_service.close_session("sess-1", backends=o._backends).ok
    assert db.get_task(tid)["lineage_state"] == "voided"
    assert db.get_flow_run(case_id)["status"] not in db._CLOSED_STATUSES
    assert _sess().current_case_id == case_id


# --------------------------------------------------------------------------- #
# Telegram surfaces (real TelegramInterface handlers over the real orchestrator)
# --------------------------------------------------------------------------- #
class _Msg:
    def __init__(self) -> None:
        self.replies = []

    async def reply_text(self, text, **_k):
        self.replies.append(text)


class _Upd:
    def __init__(self) -> None:
        self.effective_user = type("U", (), {"id": 1})()
        self.effective_chat = type("C", (), {"id": 100})()
        self.message = _Msg()


class _Ctx:
    def __init__(self, args) -> None:
        self.args = args


def _bot(o):
    from src.telegram.interface import TelegramInterface

    bot = TelegramInterface("", o, allowed_users=[1])
    bot.session_store = o.session_store
    return bot


def test_T01_telegram_session_cancel_targets_the_active_managed_turn(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    t2 = _submit(o, operation_id="b")
    _pass(db, o)
    tok = _run(db, t1)
    bot = _bot(o)
    upd = _Upd()
    asyncio.run(bot._handle_session_cancel(upd, _Ctx(["sess-1"])))
    assert t1 in upd.message.replies[-1]
    assert db.get_task(t1)["cancel_token"] == tok and db.get_task(t2)["status"] == "queued"
    assert _sess().status == SessionStatus.CANCELLED  # stop hold (rework)
    # Explicit task id through /cancel goes through the fenced cancel_task path
    # (after an operator action released the stop hold).
    db.complete_turn(t1, tok, {"success": False}, status="failed")
    _submit(o, operation_id="c")
    _pass(db, o)
    upd = _Upd()
    asyncio.run(bot._handle_cancel_command(upd, _Ctx([t2])))
    assert db.get_task(t2)["status"] == "cancelled"


def test_T02_telegram_compact_reports_the_queued_managed_turn(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    _with_native(db)
    bot = _bot(o)
    upd = _Upd()
    asyncio.run(bot._handle_compact(upd, _Ctx(["sess-1"])))
    assert "queued" in upd.message.replies[-1].lower()
    [row] = _managed_rows(db)
    assert row["turn_kind"] == "compaction"
    assert o._backends["claude"].compacted == []


# --------------------------------------------------------------------------- #
# Guards added for mutation kills
# --------------------------------------------------------------------------- #
def test_L05_admission_and_activation_refused_inside_the_close_window(tmp_path, monkeypatch):
    """The closed state is durable in the SAME transaction as the withdrawal:
    between it and the service's own save, a racing admission or scheduler
    activation already sees the session closed."""
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    _run(db, t1)
    seen = {}
    real = db.request_turn_cancel

    def in_window(*a, **k):
        try:
            _submit(o, operation_id="racer")
            seen["admitted"] = True
        except tq.TurnQueueError as e:
            seen["refused"] = e.status_code
        return real(*a, **k)
    monkeypatch.setattr(db, "request_turn_cancel", in_window)
    assert o.session_service.close_session("sess-1", backends=o._backends).ok
    assert seen == {"refused": 409}
    assert [r["id"] for r in _managed_rows(db)] == [t1]


def test_L02b_void_clears_affiliation_even_if_case_close_did_not(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    parent = db.open_case(objective="p", session_id="mgr-x", role="manager")
    tb = _birth(o, "b", parent)
    child = db.get_task(tb)["flow_run_id"]
    # close_case closes the Case but its (best-effort) affiliation clear fails.
    monkeypatch.setattr(o, "_clear_session_case_affiliation", lambda *a, **k: None)
    assert o.session_service.close_session("sess-1", backends=o._backends).ok
    assert db.get_flow_run(child)["status"] == "cancelled"
    assert _sess().current_case_id is None


def test_S01_unfinished_void_keeps_a_bounded_idle_wake():
    res = ts.SchedulerPassResult(void_outstanding=1)
    assert ts._next_timeout(res, 25, 60.0, 3.0) == ts.FALLBACK_INTERVAL_SEC
    assert ts._next_timeout(ts.SchedulerPassResult(), 25, 60.0, 3.0) is None


# --------------------------------------------------------------------------- #
# Stage 4b rework
# --------------------------------------------------------------------------- #
def test_R01_void_never_wipes_a_newer_case_affiliation(tmp_path, monkeypatch):
    """M3 kill: the strict clear is conditional on the voided child Case."""
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    parent = db.open_case(objective="p", session_id="mgr-x", role="manager")
    tb = _birth(o, "b", parent)
    child = db.get_task(tb)["flow_run_id"]
    newer = db.open_case(objective="newer", session_id="mgr-y", role="manager")
    db.set_session_case("sess-1", newer, "worker")
    assert db.clear_session_case_if("sess-1", child) is False
    assert o.session_service.close_session("sess-1", backends=o._backends).ok
    assert db.get_flow_run(child)["status"] == "cancelled"
    assert _sess().current_case_id == newer


def test_R02_release_of_a_cancelled_attempt_is_node_fenced(tmp_path, monkeypatch):
    """M1 kill: another node cannot end a cancelled attempt via release."""
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    tok = _run(db, t1)
    o.cancel_task(t1)
    assert db.release_turn(t1, tok, backend_not_invoked=True, node_id="intruder") is False
    assert db.get_task(t1)["status"] == "running"
    assert db.release_turn(t1, tok, backend_not_invoked=True, node_id="worker-a") is True
    assert db.get_task(t1)["status"] == "cancelled"


def test_R03_stop_holds_activation_until_an_operator_action(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    t2 = _submit(o, operation_id="b")
    _pass(db, o)
    tok = _run(db, t1)
    assert o.stop_managed_session_turn(_sess()) == (True, t1)
    db.complete_turn(t1, tok, {"success": False}, status="failed")
    assert db.get_task(t1)["status"] == "cancelled"
    res = _pass(db, o)
    assert res.activated == 0 and db.get_task(t2)["status"] == "queued"  # held
    assert _sess().status == SessionStatus.CANCELLED
    t3 = _submit(o, operation_id="c")  # the operator acts again: hold released
    assert _sess().status == SessionStatus.IDLE
    _pass(db, o)
    assert db.get_task(t2)["status"] == "pending" and db.get_task(t3)["status"] == "queued"


def test_R03b_plain_cancel_and_automation_admission_do_not_touch_the_hold(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1 = _submit(o, operation_id="a")
    _pass(db, o)
    _run(db, t1)
    assert o.cancel_task(t1) is True  # Case interrupt / explicit id: no hold
    assert _sess().status == SessionStatus.IDLE
    assert o.stop_managed_session_turn(_sess()) == (True, t1)
    assert _sess().status == SessionStatus.CANCELLED
    db.enqueue_turn(session_id="sess-1", backend="claude", payload={"prompt": "auto"},
                    body="auto", turn_source="system", operation_id="sys-1",
                    idempotency_scope="system:sess-1", machine_id="worker-a")
    assert _sess().status == SessionStatus.CANCELLED  # automation never releases it


def test_R04_stopped_enrolled_manager_is_not_woken_by_automation(tmp_path, monkeypatch):
    """Wake dispatcher and transient resume honour the managed stop hold."""
    import inspect

    from src.core.interfaces import SessionStatus as SS
    from tests.test_case_transient_resume import _Orch, _append_pause, _iso, _now

    class _Auto(_Orch):
        """The duck-typed automation self, falling back to the REAL methods."""

        def __getattr__(self, name):
            static = inspect.getattr_static(TaskOrchestrator, name)
            if isinstance(static, (staticmethod, classmethod)):
                return getattr(TaskOrchestrator, name)
            attr = getattr(TaskOrchestrator, name)
            return attr.__get__(self) if callable(attr) else attr

    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("TRANSIENT_PROVIDER_RESUME_ENABLED", "1")
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)

    def scenario(stop: bool):
        s = _sess()
        s.status = SS.AWAITING_INPUT
        o.session_store.save(s)
        case_id = db.open_case("ship X", "sess-1", role="manager",
                               completion_criteria='{"round_cap": 5}')
        if stop:
            t = _submit(o, operation_id=f"op-{case_id}")
            _pass(db, o)
            _run(db, t)
            assert o.stop_managed_session_turn(_sess())[0] is True
        auto = _Auto(o.session_store)
        db.arm_wait_group(case_id, "g1", "ALL", ["w1"])
        db.append_flow_event(case_id, "task.finished", "worker", entity_type="task",
                             entity_id="w1", payload={"outcome": "success"})
        woke = asyncio.run(auto._continue_case_once(db, case_id))
        _append_pause(db, case_id, "sess-1", retry_at=_iso(_now() - timedelta(seconds=1)))
        asyncio.run(auto._handle_transient_paused_case(db, case_id))
        return woke, auto.deliveries

    woke, deliveries = scenario(stop=False)  # control: automation does act
    assert woke == 1 and deliveries
    woke, deliveries = scenario(stop=True)
    assert woke == 0 and deliveries == []
