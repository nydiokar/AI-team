"""A82 Stage 4d — producer 4 (watched-job notification) and producer 6 (cache
heartbeat, idle-only with expiry) for ENROLLED sessions.

Real pieces: a file-backed ``MeshDB`` as ``get_db()``, REAL bound orchestrator
methods on a bare instance (``_process_terminal_job`` / ``_record_job_session_turn``
/ ``_process_due_cache_heartbeats`` → managed admission → lineage; the durable
heartbeat finalizer), the REAL scheduler pass (activation-time revalidation +
expiry), REAL operator stop / session close / Case interrupt, and the REAL
managed claim/start/complete DB seams. No backend/CLI: the autouse spawn guard
from the producer-1 suite makes ``_SDKSession.start`` / ``ClaudeSDKClient.connect``
/ ``create_subprocess_exec`` raise, and no test reaches a backend.
"""
import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control.db import (
    CACHE_HEARTBEAT_ACTION, CACHE_HEARTBEAT_MACHINE_SENTINEL, producer_turn_id,
)
from src.core.interfaces import Session, SessionStatus as SS
from tests.test_session_cache_heartbeat import _cache_evidence
from tests.test_turn_queue_4b import _pass, _run, _wire
from tests.test_turn_queue_producer1 import (  # noqa: F401
    NOW, _flags, _managed_rows, _no_cli_spawn, _setup, _submit, _sess,
)


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch):
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


class _Notifier:
    def __init__(self):
        self.calls = []

    async def notify_task_outcome(self, task_id, result, *, session=None, chat_id=None, prefix=""):
        self.calls.append(task_id)


def _env(tmp_path, monkeypatch, *, enroll=True, status=SS.AWAITING_INPUT):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    monkeypatch.setenv("CACHE_HEARTBEAT_MIN_CACHE_TOKENS", "100")
    db, o = _setup(tmp_path, monkeypatch, enroll=enroll)
    _wire(o)
    o.notifier = _Notifier()
    o._processed_terminal_jobs = set()
    o.task_results = {}
    o.running = True
    o.legacy_submits = []
    s = _sess()
    s.status = status
    s.backend_session_id = "native-1"
    s.driver_type = "sdk"
    s.driver_status = "live"
    o.session_store.save(s)
    return db, o


def _job(job_id="job_x", *, notify=1, notify_agent=1, status="done"):
    return {"id": job_id, "session_id": "sess-1", "node_id": "worker-a", "label": "npm test",
            "status": status, "exit_code": 0 if status == "done" else 1,
            "tail": "all tests passed", "notify": notify, "notify_agent": notify_agent}


def _process(o, job):
    asyncio.run(o._process_terminal_job(job))


def _kind(db, kind):
    return [r for r in _managed_rows(db) if r["turn_kind"] == kind]


def _legacy_session_rows(db):
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE COALESCE(queue_protocol, 0) = 0 AND session_id = 'sess-1'"
    ).fetchall()]


def _restart(o_like):
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
    o.notifier = _Notifier()
    o._processed_terminal_jobs = set()
    o.task_results = {}
    o.running = True
    return _wire(o)


def _running_operator_turn(db, o, op="op-1"):
    t = _submit(o, operation_id=op)
    _pass(db, o)
    _run(db, t)
    return t


# =========================================================================== #
# Producer 4 — watched-job notification
# =========================================================================== #
def test_W01_job_notification_is_one_durable_managed_turn_replays_collapse(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _process(o, _job())
    rows = _kind(db, "watched_job")
    assert len(rows) == 1
    turn = rows[0]
    assert turn["id"] == producer_turn_id("watched:job_x", "sess-1", 1, prefix="jturn")
    assert turn["turn_source"] == "system"
    assert turn["idempotency_scope"] == "automation:sess-1:watched_job"
    assert turn["idempotency_key"] == "watched:job_x"
    assert turn["coalesce_key"] == "watched:job_x"
    assert "npm test" in turn["prompt"]
    assert o.notifier.calls == ["job_x"]  # Telegram notification unchanged
    assert _legacy_session_rows(db) == []  # no synthetic/legacy row into the enrolled session
    # a second poll in this process, a restarted gateway, and a failed-status
    # re-report all collapse to the same ONE turn
    o._processed_terminal_jobs = set()
    _process(o, _job())
    _process(_restart(o), _job())
    _process(_restart(o), _job(status="lost"))
    assert [r["id"] for r in _kind(db, "watched_job")] == [turn["id"]]
    # even after the turn is terminal (permanent idempotency, not active coalesce)
    _pass(db, o)
    tok = _run(db, turn["id"])
    db.complete_turn(turn["id"], tok, {"success": True})
    _process(_restart(o), _job())
    assert [r["id"] for r in _kind(db, "watched_job")] == [turn["id"]]


def test_W02_job_turn_attaches_to_the_sessions_open_case_once(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = db.open_case("ship X", "sess-1", role="manager")
    _process(o, _job())
    turn = _kind(db, "watched_job")[0]
    assert turn["flow_run_id"] == cid
    links = db.list_flow_links(flow_run_id=cid, entity_type="task")
    assert [lk["entity_id"] for lk in links] == [turn["id"]]
    attached = [e for e in db.list_flow_events(cid)
                if e["event_type"] == "task.attached" and e.get("entity_id") == turn["id"]]
    assert len(attached) == 1
    # no Case was BORN for the notification and the Manager was not relabelled
    assert len(db.list_open_cases()) == 1
    assert db.case_manager_session_id(cid) == "sess-1"
    _process(_restart(o), _job())  # replay writes no second link/event
    assert len(db.list_flow_links(flow_run_id=cid, entity_type="task")) == 1
    assert len([e for e in db.list_flow_events(cid) if e["event_type"] == "task.attached"]) == 1


def test_W03_job_turn_honours_and_never_releases_the_operator_stop_hold(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    t = _running_operator_turn(db, o)
    assert o.stop_managed_session_turn(_sess())[0] is True  # REAL operator stop
    db.complete_turn(t, db.get_task(t)["claim_token"], {"success": False}, status="failed")
    _process(o, _job())
    job_turn = _kind(db, "watched_job")[0]
    assert db.operator_stop_hold("sess-1") == "operator_stop"  # not released
    assert db.get_session("sess-1")["status"] == "cancelled"
    assert _pass(db, o).activated == 0
    assert db.get_task(job_turn["id"])["status"] == "queued"  # held, not dropped
    # the operator's next send releases the hold; FIFO: the notification runs first
    human = _submit(o, operation_id="op-after-stop")
    assert db.operator_stop_hold("sess-1") is None
    assert _pass(db, o).activated == 1
    assert db.get_task(job_turn["id"])["status"] == "pending"
    assert db.get_task(human)["status"] == "queued"


def test_W04_notify_only_record_is_audit_only_and_never_undoes_a_hold(tmp_path, monkeypatch):
    """4b MINOR 4: `_record_job_session_turn` did a stale whole-row session save.
    On the managed path it writes one already-terminal audit row and NO session
    save — a held (cancelled) session stays held even with a stale snapshot."""
    db, o = _env(tmp_path, monkeypatch)
    stale = _sess()  # AWAITING_INPUT snapshot taken BEFORE the stop
    t = _running_operator_turn(db, o)
    assert o.stop_managed_session_turn(_sess())[0] is True
    db.complete_turn(t, db.get_task(t)["claim_token"], {"success": False}, status="failed")
    o.session_store.get = lambda sid: stale  # the poller holds a stale snapshot

    def _no_pending_window(*a, **k):
        raise AssertionError("audit row must not pass through a claimable pending state")
    monkeypatch.setattr(db, "enqueue_task", _no_pending_window)
    _process(o, _job(notify_agent=0))
    row = db.get_task("job_x")
    assert row["status"] == "completed" and row["action"] == "watched_job"
    assert int(row["queue_protocol"] or 0) == 0 and row["reply_text"]
    assert db.get_session("sess-1")["status"] == "cancelled"
    assert db.operator_stop_hold("sess-1") == "operator_stop"
    assert _kind(db, "watched_job") == []  # a notify-only job wakes nobody
    # replay: still one audit row, no error
    o._processed_terminal_jobs = set()
    _process(o, _job(notify_agent=0))
    assert len(_legacy_session_rows(db)) == 1


def test_W05_refused_admission_falls_back_to_audit_without_legacy_execution(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    db._conn().execute("DELETE FROM nodes")  # no managed carrier ⇒ typed 503 refusal
    db._conn().commit()
    before = db.get_session("sess-1")["status"]
    _process(o, _job())
    assert _kind(db, "watched_job") == []
    rows = _legacy_session_rows(db)
    assert [(r["id"], r["status"]) for r in rows] == [("job_x", "completed")]
    assert db.get_session("sess-1")["status"] == before  # no whole-row save


def test_W06_job_turn_of_an_interrupted_case_is_withdrawn_at_activation(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = db.open_case("ship X", "sess-1", role="manager")
    t = _running_operator_turn(db, o)
    _process(o, _job())
    job_turn = _kind(db, "watched_job")[0]["id"]
    out = asyncio.run(o.interrupt_case(cid))  # REAL kill path
    assert out["ok"]
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": True})
    _pass(db, o)
    assert db.get_task(job_turn)["status"] == "withdrawn"
    assert any("case_blocked" in str(a.get("actor")) for a in db.get_turn_revisions(job_turn))


# =========================================================================== #
# Producer 6 — cache heartbeat (idle-only, expiry, durable finalization)
# =========================================================================== #
def _arm_heartbeat(db):
    _cache_evidence(db, session_id="sess-1", cache_read=1000)
    hb = db.ensure_cache_heartbeat_owner(
        "sess-1", reason="manual", owner_type="operator", owner_id="manual",
    )
    assert hb is not None and hb["status"] == "active"
    _make_due(db, hb["id"])
    return hb["id"]


def _make_due(db, hb_id):
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    with db._write() as conn:  # test clock: the controller is due now
        conn.execute("UPDATE session_cache_heartbeats SET next_due_at = ? WHERE id = ?",
                     (past, hb_id))


def _beat(o, db):
    return asyncio.run(o._process_due_cache_heartbeats(db))


def _hb_rows(db):
    return _kind(db, "heartbeat")


def _lease(db):
    rows = [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE action = ?", (CACHE_HEARTBEAT_ACTION,)).fetchall()]
    assert len(rows) <= 1
    return rows[0] if rows else None


def test_H01_idle_session_gets_one_heartbeat_turn_per_window(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    hb_id = _arm_heartbeat(db)
    assert _beat(o, db) == 1
    rows = _hb_rows(db)
    assert len(rows) == 1
    turn = rows[0]
    lease = _lease(db)
    assert turn["id"] == producer_turn_id(lease["id"], "sess-1", 1, prefix="hturn")
    assert turn["turn_source"] == "system"  # never operator activity
    assert turn["idempotency_scope"] == "automation:sess-1:heartbeat"
    assert turn["idempotency_key"] == lease["id"]
    assert turn["expires_at"]  # optional work carries a deadline
    assert lease["status"] == "claimed" and lease["claimed_at"] is None
    assert lease["producer_turn_id"] == turn["id"]
    assert lease["claimed_by"] == CACHE_HEARTBEAT_MACHINE_SENTINEL
    # same window: a second tick (and a restarted gateway) admits nothing new
    _make_due(db, hb_id)
    assert _beat(o, db) == 0
    assert _beat(_restart(o), db) == 0
    assert len(_hb_rows(db)) == 1


def test_H02_not_idle_sessions_get_no_heartbeat(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _arm_heartbeat(db)
    t = _running_operator_turn(db, o)  # real work in flight
    assert _beat(o, db) == 0 and _hb_rows(db) == []
    assert db.heartbeat_eligible("sess-1") is False
    # queued human work alone also makes it ineligible
    q = _submit(o, operation_id="op-2")
    assert db.get_task(q)["status"] == "queued"
    db.complete_turn(t, db.get_task(t)["claim_token"], {"success": True})
    assert _beat(o, db) == 0 and _hb_rows(db) == []


def test_H03_held_session_gets_no_heartbeat_and_the_hold_is_kept(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _arm_heartbeat(db)
    t = _running_operator_turn(db, o)
    assert o.stop_managed_session_turn(_sess())[0] is True
    db.complete_turn(t, db.get_task(t)["claim_token"], {"success": False}, status="failed")
    assert db.get_active_turn("sess-1") is None  # the ledger is idle ...
    assert _beat(o, db) == 0 and _hb_rows(db) == []  # ... but held ⇒ no heartbeat
    assert db.operator_stop_hold("sess-1") == "operator_stop"
    # the in-txn idle guard refuses even a direct admission racing the stop
    with pytest.raises(tq.OwnershipConflictError):
        db.enqueue_turn(session_id="sess-1", body="hb", turn_kind="heartbeat",
                        operation_id="k", idempotency_scope="automation:sess-1:heartbeat",
                        machine_id="worker-a", idle_only=True, require_enrolled=True)
    assert db.operator_stop_hold("sess-1") == "operator_stop"


def test_H04_human_work_arriving_behind_a_queued_heartbeat_withdraws_it(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _arm_heartbeat(db)
    assert _beat(o, db) == 1
    hb_turn = _hb_rows(db)[0]["id"]
    human = _submit(o, operation_id="op-human")  # queued behind the heartbeat
    _pass(db, o)
    assert db.get_task(hb_turn)["status"] == "withdrawn"
    assert any("session_not_idle" in str(a.get("actor")) for a in db.get_turn_revisions(hb_turn))
    _pass(db, o)
    assert db.get_task(human)["status"] == "pending"  # real work runs, not the heartbeat


def test_H05_expired_heartbeat_is_withdrawn_at_the_head(tmp_path, monkeypatch):
    from src import orchestrator as orch_mod

    db, o = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(orch_mod, "MANAGED_HEARTBEAT_TTL_SEC", -1)
    _arm_heartbeat(db)
    assert _beat(o, db) == 1
    hb_turn = _hb_rows(db)[0]["id"]
    res = _pass(db, o)
    assert res.activated == 0 and db.get_task(hb_turn)["status"] == "withdrawn"
    assert any("expired" in str(a.get("actor")) for a in db.get_turn_revisions(hb_turn))


def test_H05b_expired_activated_heartbeat_is_never_claimed_late(tmp_path, monkeypatch):
    """A heartbeat activated in time but not started within its window (e.g.
    released not-invoked while the backend was busy) is withdrawn at claim."""
    from src import orchestrator as orch_mod

    db, o = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(orch_mod, "MANAGED_HEARTBEAT_TTL_SEC", 1)
    _arm_heartbeat(db)
    assert _beat(o, db) == 1
    hb_turn = _hb_rows(db)[0]["id"]
    assert _pass(db, o).activated == 1
    assert db.get_task(hb_turn)["status"] == "pending"
    import time as _time
    _time.sleep(1.2)
    with pytest.raises(tq.OwnershipConflictError):
        db.claim_turn(hb_turn, "worker-a", "worker_daemon", "inc-1")
    assert db.get_task(hb_turn)["status"] == "withdrawn"
    assert db.get_active_turn("sess-1") is None  # the slot is free for real work
    # a HUMAN turn never expires at claim
    h = _submit(o, operation_id="op-h")
    _pass(db, o)
    assert db.claim_turn(h, "worker-a", "worker_daemon", "inc-1")


def test_H06_activation_revalidates_quota(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _arm_heartbeat(db)
    assert _beat(o, db) == 1
    turn = _hb_rows(db)[0]["id"]
    o._cache_heartbeat_quota_available = lambda: False  # quota spent after admission
    _pass(db, o)
    assert db.get_task(turn)["status"] == "withdrawn"
    assert any("quota_exhausted" in str(a.get("actor")) for a in db.get_turn_revisions(turn))


def test_H06b_activation_revalidates_the_owner(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    hb_id = _arm_heartbeat(db)
    assert _beat(o, db) == 1
    turn = _hb_rows(db)[0]["id"]
    assert db.stop_cache_heartbeat(hb_id, "operator_disabled")  # REAL operator disable
    _pass(db, o)
    assert db.get_task(turn)["status"] == "withdrawn"
    assert any("heartbeat_stopped" in str(a.get("actor")) for a in db.get_turn_revisions(turn))


def test_H07_completed_heartbeat_is_finalized_durably_exactly_once(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    hb_id = _arm_heartbeat(db)
    assert _beat(o, db) == 1
    turn = _hb_rows(db)[0]["id"]
    _pass(db, o)
    tok = _run(db, turn)
    db.complete_turn(turn, tok, {"success": True, "output": "ok", "usage": {
        "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 0}})
    # a RESTARTED gateway (no in-memory finalizer) records the beat
    o2 = _restart(o)
    _beat(o2, db)
    hb = db.get_cache_heartbeat(hb_id)
    assert hb["beat_count"] == 1 and hb["last_beat_task_id"] == turn
    assert hb["last_cache_read_tokens"] == 5000 and hb["status"] == "active"
    lease = _lease(db)
    assert lease["status"] == "completed"
    assert json.loads(lease["result"])["turn_status"] == "completed"
    # re-running the finalizer never double-counts
    _beat(_restart(o), db)
    assert db.reconcile_heartbeat_finalizers() == []
    assert db.get_cache_heartbeat(hb_id)["beat_count"] == 1


def test_H08_withdrawn_or_closed_heartbeat_finalizes_without_a_beat(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    hb_id = _arm_heartbeat(db)
    assert _beat(o, db) == 1
    turn = _hb_rows(db)[0]["id"]
    assert o.session_service.close_session("sess-1", backends=o._backends).ok  # REAL close
    assert db.get_task(turn)["status"] == "withdrawn"
    done = db.reconcile_heartbeat_finalizers()
    assert [d["turn_id"] for d in done] == [turn]
    assert _lease(db)["status"] == "completed"
    hb = db.get_cache_heartbeat(hb_id)
    assert int(hb["beat_count"] or 0) == 0  # nothing ran ⇒ no beat counted


def test_H09_failed_heartbeat_stops_the_controller_like_legacy(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    hb_id = _arm_heartbeat(db)
    assert _beat(o, db) == 1
    turn = _hb_rows(db)[0]["id"]
    _pass(db, o)
    tok = _run(db, turn)
    db.complete_turn(turn, tok, {"success": False, "error_class": "usage_limit"}, status="failed")
    assert [d["turn_id"] for d in db.reconcile_heartbeat_finalizers()] == [turn]
    hb = db.get_cache_heartbeat(hb_id)
    assert hb["status"] == "stopped" and hb["circuit_reason"] == "heartbeat_usage_limit"


def test_SYS06_db_level_eligibility_matches_the_ledger(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    assert db.heartbeat_eligible("sess-1") is True
    t = _submit(o, operation_id="op-1")
    assert db.heartbeat_eligible("sess-1") is False
    assert db.heartbeat_eligible("sess-1", exclude_turn_id=t) is True
    assert db.heartbeat_eligible("nope") is False


# =========================================================================== #
# Unenrolled: byte-identical legacy — no managed/enrollment SQL on any thread
# =========================================================================== #
def test_U01_unenrolled_producers_touch_no_managed_state(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch, enroll=False)
    _arm_heartbeat(db)
    submits = []

    async def _legacy_submit(description, **kw):
        submits.append(kw)
        return "task_legacy"
    o.submit_instruction = _legacy_submit
    stmts, threads = [], set()
    real_conn = type(db)._conn

    def traced(self):
        c = real_conn(self)
        c.set_trace_callback(lambda sql: (stmts.append(sql), threads.add(threading.get_ident())))
        return c
    monkeypatch.setattr(type(db), "_conn", traced)
    _process(o, _job())
    _process(o, _job("job_y", notify_agent=0))
    delivered = _beat(o, db)

    async def _drain():  # the legacy in-memory finalizer task was scheduled
        await asyncio.sleep(0)
    asyncio.run(_drain())
    monkeypatch.setattr(type(db), "_conn", real_conn)
    assert stmts
    forbidden = ("turn_queue_enrolled", "queue_protocol = 1", "turn_queue_hold",
                 "producer_turn_id", "idx_mesh_tasks_producer_link",
                 "idx_mesh_turns_session_open", "mesh_turn_revisions")
    assert not [q for q in stmts if any(f in q for f in forbidden)]
    # legacy deliveries exactly as before
    assert [k["source"] for k in submits] == ["watched_job", "cache_heartbeat"]
    assert submits[0]["extra_metadata"] == {"job_id": "job_x", "source": "watched_job"}
    assert delivered == 1
    assert _managed_rows(db) == []
    assert db.get_task("job_y")["status"] == "completed"  # legacy synthetic turn
