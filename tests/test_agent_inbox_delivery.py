"""A104 Gate 3 — delivery through the agent inbox: ONE read (``pending_for``) for
the wake producer AND the activation check, a bounded delivery state machine,
never-run turns traceless, no state from truncated windows.

Real pieces (the H3 managed harness): a file-backed ``MeshDB`` as ``get_db()``,
the REAL Wake-Dispatcher tick, REAL managed admission + lineage, the REAL
scheduler pass (activation-time revalidation), the REAL claim/start/complete
seams. No backend/CLI (autouse spawn guard).

IR01 the 2026-10-09 incident end to end: a Manager-own turn, operator messages, a
     worker finish, >500 Case events, >1,000 session rows ⇒ exactly ONE wake that
     presents only the worker task, zero withdrawals, zero self-addressed rows, and
     the latest reply is in the chat window.
"""
import asyncio
import json

import pytest

from src.control import turn_admission as ta
from src.core.interfaces import Session, SessionStatus as SS
from tests.test_turn_queue_4b import _pass, _run, _wire
from tests.test_turn_queue_producer1 import (  # noqa: F401
    NOW, _flags, _no_cli_spawn, _setup, _submit,
)


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch):
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("CASE_RESPAWN_REQUIRES_APPROVAL", "0")
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    s = o.session_store.get("sess-1")
    s.status = SS.AWAITING_INPUT
    o.session_store.save(s)
    return db, o


def _add_session(db, sid, *, status=SS.IDLE):
    db.upsert_session(Session(
        session_id=sid, backend="claude", repo_path="/tmp/repo",
        status=status, created_at=NOW, updated_at=NOW, machine_id="worker-a",
    ))
    db.enroll_session(sid)


def _complete(db, tid, *, status="completed"):
    tok = _run(db, tid)
    assert db.complete_turn(tid, tok, {"success": status == "completed", "output": "ok"}, status=status)


def _finish(db, o, tid, *, status="completed"):
    _pass(db, o)
    _complete(db, tid, status=status)


def _tick(o):
    return asyncio.run(o._wake_dispatcher_tick_once())


def _rows(db, sql, *a):
    return [dict(r) for r in db._conn().execute(sql, a).fetchall()]


def _wakes(db):
    return _rows(db, "SELECT * FROM mesh_tasks WHERE queue_protocol = 1 AND turn_kind = 'continuation' "
                     "ORDER BY queue_sequence")


def _drive(db, o, rounds=8):
    """Run the dispatcher like production: tick, activate, run whatever the
    scheduler activated, repeat. Returns the wakes that RAN."""
    ran = []
    for _ in range(rounds):
        _tick(o)
        _pass(db, o)
        for w in _wakes(db):
            if w["status"] == "pending":
                _complete(db, w["id"])
                ran.append(w["id"])
    return ran


def _bulk_noise(db, cid, *, events=520, session_rows=1050):
    """>500 Case events (old junk first) and >1,000 Manager-session rows."""
    with db._write() as conn:
        conn.executemany(
            "INSERT INTO flow_events (flow_run_id, event_type, actor, entity_type, entity_id, "
            "payload_json, created_at) VALUES (?, 'task.attached', 'system', 'task', ?, '{}', ?)",
            [(cid, f"junk-{i}", f"2026-10-09T00:{i // 60:02d}:{i % 60:02d}+00:00") for i in range(events)],
        )
        conn.executemany(
            "INSERT INTO mesh_tasks (id, session_id, machine_id, backend, action, payload, prompt, status, "
            "created_at, updated_at, completed_at, queue_protocol, turn_source, turn_kind, reply_text) "
            "VALUES (?, 'sess-1', 'worker-a', 'claude', 'resume_session', '{}', ?, 'completed', ?, ?, ?, "
            "0, 'human', 'instruction', 'old reply')",
            [(f"old-{i}", f"old prompt {i}", f"2026-01-01T00:00:{i % 60:02d}.{i:06d}+00:00",
              "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00") for i in range(session_rows)],
        )


# IR01 ---------------------------------------------------------------------- #
def test_IR01_incident_2026_10_09_wakes_exactly_once_with_only_the_worker(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "w-1")
    # The incident Cases were born in outbox mode; the inbox must not care.
    monkeypatch.setenv("CASE_COMPLETION_OUTBOX_ENABLED", "1")
    cid = db.open_case("gpu-enable", "sess-1", role="manager")
    monkeypatch.delenv("CASE_COMPLETION_OUTBOX_ENABLED")
    # 1. The Manager's own boot turn.
    boot = _submit(o, description="boot", source="manager_invoke", operation_id="boot")
    _finish(db, o, boot)
    # 2. An operator message; DURING it the Manager dispatches the worker (an
    #    un-redeployed mcp_manager: no requester id on the wire).
    op1 = _submit(o, description="operator: enable the gpu", operation_id="op-1")
    _pass(db, o)
    tok = _run(db, op1)
    child = _submit(o, description="worker: do gpu-enable", session_id="w-1",
                    source="automation_session", join_case_id=cid, operation_id="d-1")
    assert db.complete_turn(op1, tok, {"success": True, "output": "dispatched"}, status="completed")
    # 3. History far past every oldest-N window.
    _bulk_noise(db, cid)
    # 4. The worker finishes; then the operator interleaves another message.
    _finish(db, o, child)
    op2 = _submit(o, description="operator: status?", operation_id="op-2")
    _finish(db, o, op2)

    ran = _drive(db, o)

    wakes = _wakes(db)
    assert len(wakes) == 1, [(w["id"], w["status"]) for w in wakes]
    (wake,) = wakes
    assert wake["status"] == "completed" and ran == [wake["id"]]
    assert str(child) in wake["prompt"]
    for t in (boot, op1, op2):
        assert str(t) not in wake["prompt"]
    assert _rows(db, "SELECT id FROM mesh_tasks WHERE status = 'withdrawn'") == []
    inbox = _rows(db, "SELECT * FROM agent_inbox")
    assert [(m["about_task_id"], m["recipient_session_id"], m["state"]) for m in inbox] \
        == [(str(child), "sess-1", "acked")]
    assert not [m for m in inbox if m["recipient_session_id"] == m["sender_session_id"]]
    assert db.pending_for("sess-1").messages == []
    # The chat window holds the latest reply (newest window, not the oldest 1,000).
    turns = db.get_session_turns("sess-1", limit=1000)
    assert turns[-1]["task_id"] == wake["id"]
    assert str(op2) in {t["task_id"] for t in turns}
    assert json  # (json kept for payload assertions in later scenarios)


# --------------------------------------------------------------------------- #
# Gate 3 scenario matrix — every scenario asserts pending_for AND wakes admitted
# --------------------------------------------------------------------------- #
from src.control import agent_inbox as ib  # noqa: E402
from tests.inbox_seed import finish_child, seed_child, seed_finished_child  # noqa: E402


def _case(db, sid="sess-1"):
    return db.open_case("ship X", sid, role="manager")


def _msg_states(db):
    return {r["about_task_id"]: r["state"] for r in _rows(db, "SELECT about_task_id, state FROM agent_inbox")}


def _admitted(db):
    return len(_wakes(db))


def _withdrawn(db):
    return [w for w in _wakes(db) if w["status"] == "withdrawn"]


def _close(o, sid):
    s = o.session_store.get(sid)
    s.status = SS.CLOSED
    o.session_store.save(s)


def _expire_backoff(db):
    with db._write() as conn:
        conn.execute("UPDATE agent_inbox SET next_attempt_at = '1970-01-01T00:00:00+00:00' "
                     "WHERE state = 'pending'")


def test_M01_idle_recipient_is_woken_once_and_acked(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    assert [m.about_task_id for m in db.pending_for("sess-1").messages] == ["task_w1"]
    ran = _drive(db, o)
    assert _admitted(db) == 1 and len(ran) == 1
    assert _msg_states(db) == {"task_w1": "acked"}
    assert db.pending_for("sess-1").messages == []
    assert _drive(db, o) == [] and _admitted(db) == 1  # nothing re-admits


def test_M02_busy_recipient_gets_one_wake_queued_behind_the_operator_turn(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    op = _submit(o, description="operator: long task", operation_id="op-1")
    _pass(db, o)
    op_tok = _run(db, op)                      # the recipient is busy
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    for _ in range(4):
        _tick(o)
        _pass(db, o)
    assert _admitted(db) == 1                  # admitted once, queued behind, never interrupts
    (wake,) = _wakes(db)
    assert wake["status"] == "queued"
    assert db.pending_for("sess-1").carried_by(wake["id"])
    assert db.complete_turn(op, op_tok, {"success": True, "output": "ok"}, status="completed")
    ran = _drive(db, o)
    assert ran == [wake["id"]] and _admitted(db) == 1 and _withdrawn(db) == []
    assert _msg_states(db) == {"task_w1": "acked"}


def test_M03_dead_recipient_follows_recorded_lineage_to_its_successor(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    # sess-1 dies; it was continued as sess-2 (recorded lineage).
    _close(o, "sess-1")
    db.upsert_session(Session(session_id="sess-2", backend="claude", repo_path="/tmp/repo",
                              status=SS.AWAITING_INPUT, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a", continued_from="sess-1"))
    db.enroll_session("sess-2")
    ran = _drive(db, o)
    assert _admitted(db) == 1 and len(ran) == 1
    (wake,) = _wakes(db)
    assert wake["session_id"] == "sess-2"
    assert _rows(db, "SELECT recipient_session_id, state FROM agent_inbox") == [
        {"recipient_session_id": "sess-2", "state": "acked"}]


def test_M03b_dead_recipient_without_successor_dies_with_one_alert(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "w-1")
    cid = _case(db)
    # A worker asked another worker; the asking worker is gone, no successor.
    seed_finished_child(db, cid, "task_w2", requester="w-1")
    _close(o, "w-1")
    alerts = []

    class _N:
        async def notify_inbox_dead_letter(self, **kw):
            alerts.append(kw)

    o.notifier = _N()
    for _ in range(4):
        _tick(o)
    assert _admitted(db) == 0
    (row,) = _rows(db, "SELECT state, last_error, alerted_at FROM agent_inbox")
    assert (row["state"], row["last_error"]) == ("dead", "recipient_gone") and row["alerted_at"]
    assert len(alerts) == 1 and alerts[0]["reason"] == "recipient_gone"
    assert db.pending_for("w-1").messages == []


def test_M04_gateway_restart_mid_delivery_never_double_delivers(tmp_path, monkeypatch):
    from tests.test_turn_queue_4c import _fresh

    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    _tick(o)                                     # admitted + claimed (delivered)
    assert _admitted(db) == 1
    o2 = _fresh(o, monkeypatch)                  # a new gateway process, same DB
    for _ in range(3):
        _tick(o2)
    assert _admitted(db) == 1                    # in flight ⇒ no second wake
    ran = _drive(db, o2)
    assert len(ran) == 1 and _admitted(db) == 1
    assert _msg_states(db) == {"task_w1": "acked"}


def test_M05_two_workers_under_ALL_arrive_in_one_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_child(db, cid, "task_a", requester="sess-1", token="ta")
    seed_child(db, cid, "task_b", requester="sess-1", token="tb")
    db.arm_wait_group(cid, "batch", "ALL", ["task_a", "task_b"])
    finish_child(db, "task_a", token="ta")
    for _ in range(3):
        _tick(o)
    assert _admitted(db) == 0
    view = db.pending_for("sess-1")
    assert [(m.about_task_id, m.held) for m in view.messages] == [("task_a", True)]
    assert view.outstanding_task_ids == ["task_b"]
    finish_child(db, "task_b", token="tb")
    ran = _drive(db, o)
    assert _admitted(db) == 1 and len(ran) == 1
    (wake,) = _wakes(db)
    assert "task_a" in wake["prompt"] and "task_b" in wake["prompt"]
    assert _msg_states(db) == {"task_a": "acked", "task_b": "acked"}


def test_M06_out_of_band_tagged_review_acks_without_a_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    assert o.record_review(cid, verdict="accepted", task_id="task_w1")["ok"]
    assert _drive(db, o) == [] and _admitted(db) == 0
    assert _msg_states(db) == {"task_w1": "acked"}
    assert db.pending_for("sess-1").messages == []


def test_M06b_review_while_the_wake_is_queued_withdraws_it_once_and_never_readmits(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    op = _submit(o, description="operator: busy", operation_id="op-1")
    _pass(db, o)
    op_tok = _run(db, op)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    _tick(o)
    assert _admitted(db) == 1
    assert o.record_review(cid, verdict="accepted", task_id="task_w1")["ok"]
    assert db.complete_turn(op, op_tok, {"success": True, "output": "ok"}, status="completed")
    _drive(db, o)
    assert _admitted(db) == 1 and len(_withdrawn(db)) == 1  # the activation check saw it consumed
    assert _msg_states(db) == {"task_w1": "acked"}


def test_M07_operator_message_interleaving_before_the_wake_keeps_one_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    op = _submit(o, description="operator: quick question", operation_id="op-1")  # queued first
    _tick(o)                                     # wake queued behind the operator turn
    _pass(db, o)
    _complete(db, op)
    ran = _drive(db, o)
    assert _admitted(db) == 1 and len(ran) == 1 and _withdrawn(db) == []
    turns = [t["task_id"] for t in db.get_session_turns("sess-1")]
    assert turns.index(str(op)) < turns.index(ran[0])
    assert _msg_states(db) == {"task_w1": "acked"}


def test_M08_case_closed_with_pending_messages_kills_them_without_a_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    assert o.close_case(cid, actor="operator", force=True)["closed"]
    assert _drive(db, o) == [] and _admitted(db) == 0
    (row,) = _rows(db, "SELECT state, last_error, alerted_at FROM agent_inbox")
    assert (row["state"], row["last_error"]) == ("dead", "case_closed")
    assert row["alerted_at"]                     # deliberate close ⇒ no operator alert
    assert db.pending_for("sess-1").messages == []


def test_M09_rebound_recipient_messages_follow_the_rebind_record(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    db.upsert_session(Session(session_id="sess-9", backend="claude", repo_path="/tmp/repo",
                              status=SS.AWAITING_INPUT, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    db.enroll_session("sess-9")
    db.create_flow_link(cid, "session", "sess-9", "manager", created_by="system")  # rebind
    assert db.pending_for("sess-1").messages == []
    assert [m.about_task_id for m in db.pending_for("sess-9").messages] == ["task_w1"]
    ran = _drive(db, o)
    assert _admitted(db) == 1 and _wakes(db)[0]["session_id"] == "sess-9" and len(ran) == 1


def test_M10_wake_withdrawn_N_times_is_admitted_exactly_N_times_then_dead_and_alerted(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    op = _submit(o, description="operator: hold the session", operation_id="op-1")
    _pass(db, o)
    _run(db, op)                                 # keeps every wake queued
    alerts = []

    class _N:
        async def notify_inbox_dead_letter(self, **kw):
            alerts.append(kw)

    o.notifier = _N()
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    for attempt in range(1, ib.MAX_ATTEMPTS + 3):
        _expire_backoff(db)
        _tick(o)
        queued = [w for w in _wakes(db) if w["status"] == "queued"]
        if queued:
            (w,) = queued
            assert db.withdraw_turn(w["id"], actor="operator:test")
    assert _admitted(db) == ib.MAX_ATTEMPTS
    assert len(_withdrawn(db)) == ib.MAX_ATTEMPTS
    (row,) = _rows(db, "SELECT state, last_error, attempts FROM agent_inbox")
    assert (row["state"], row["last_error"], row["attempts"]) == ("dead", "attempts_exhausted", ib.MAX_ATTEMPTS)
    assert len(alerts) == 1 and alerts[0]["reason"] == "attempts_exhausted"
    assert db.pending_for("sess-1").messages == []


def test_M11_backoff_holds_redelivery(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    _tick(o)
    (w,) = _wakes(db)
    assert db.withdraw_turn(w["id"], actor="operator:test")
    for _ in range(3):
        _tick(o)
    assert _admitted(db) == 1                    # backoff (30 s) not elapsed
    (m,) = db.pending_for("sess-1").messages
    assert m.state == "pending" and m.attempts == 1 and m.ready_at > ib.now_iso()


def test_M12_producer_and_activation_read_the_same_function(tmp_path, monkeypatch):
    """The wake producer AND the activation check call ``agent_inbox.pending_for``
    — one function, so they cannot disagree (the 2026-10-09 root cause)."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    phase = {"now": ""}
    calls = []
    real = ib.pending_for

    def spy(conn, recipient, **kw):
        calls.append(phase["now"])
        return real(conn, recipient, **kw)

    monkeypatch.setattr(ib, "pending_for", spy)
    phase["now"] = "producer"        # only the wake producer runs in a tick
    _tick(o)
    assert _admitted(db) == 1
    phase["now"] = "activation"      # only the activation check runs in a pass
    _pass(db, o)
    assert "producer" in calls and "activation" in calls
    (w,) = _wakes(db)
    assert w["status"] == "pending"  # activated: both read the same, non-empty answer


def test_M13_worker_to_worker_wake_reaches_the_requesting_worker(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "w-1", status=SS.AWAITING_INPUT)
    s = o.session_store.get("w-1")
    s.status = SS.AWAITING_INPUT
    o.session_store.save(s)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w2", requester="w-1")
    ran = _drive(db, o)
    assert len(ran) == 1 and _wakes(db)[0]["session_id"] == "w-1"
    assert db.pending_for("sess-1").messages == []


def test_M14_pre_inbox_token_wake_is_superseded_and_never_rearmed(tmp_path, monkeypatch):
    """A legacy cont: token wake queued at deploy time is withdrawn at activation
    and its token discharged — the unbounded re-arm path is gone."""
    from src.control.db import continuation_task_id
    from tests.test_turn_queue_4c import _case as _legacy_case

    db, o = _env(tmp_path, monkeypatch)
    cid = _legacy_case(db)
    # Build the pre-inbox token + linked wake exactly as the A82 producer did.
    asyncio.run(o._continue_case_managed(
        db, cid, o.session_store.get("sess-1"), 1,
        {"presented_task_ids": ["w1"], "satisfied_groups": []},
    ))
    assert _admitted(db) == 1
    for _ in range(3):
        _tick(o)
        _pass(db, o)
    (w,) = _wakes(db)
    assert w["status"] == "withdrawn" and _admitted(db) == 1
    tok = db.get_task(continuation_task_id(cid, 1))
    assert tok["status"] == "cancelled" and tok["error"] == "superseded_by_agent_inbox"


def test_M15_pause_handlers_run_for_every_open_case_even_with_an_empty_inbox(tmp_path, monkeypatch):
    """The quota / transient pause handlers are time-based (they propose/drive the
    resume themselves) — they must keep running for every open Case each tick,
    not only for Cases whose inbox holds something; a paused Case gets no wake."""
    db, o = _env(tmp_path, monkeypatch)
    quiet = _case(db)                         # nothing in its inbox
    busy = db.open_case("other", "sess-1", role="manager")
    seed_finished_child(db, busy, "task_w1", requester="sess-1")
    seen = []

    async def quota(db_, cid):
        seen.append(cid)
        return cid == busy                    # the Case with mail is quota-paused

    async def transient(db_, cid):
        return False

    monkeypatch.setattr(o, "_handle_quota_paused_case", quota)
    monkeypatch.setattr(o, "_handle_transient_paused_case", transient)
    _tick(o)
    assert quiet in seen and busy in seen
    assert _admitted(db) == 0                  # paused ⇒ no wake
    assert [m.state for m in db.pending_for("sess-1").messages] == ["pending"]


def test_M16_retry_of_a_failed_wake_carries_its_messages_no_second_wake(tmp_path, monkeypatch):
    import types

    db, o = _env(tmp_path, monkeypatch)
    cid = _case(db)
    seed_finished_child(db, cid, "task_w1", requester="sess-1")
    _tick(o)
    _pass(db, o)
    (wake,) = _wakes(db)
    assert wake["status"] == "pending", wake["status"]
    _complete(db, wake["id"], status="failed")          # e.g. a usage-limit failure
    (m,) = db.pending_for("sess-1").messages
    assert m.state == "pending" and m.attempts == 1
    # The A82 recovery producer admits retry R of the failed wake.
    monkeypatch.setattr(db, "retry_decision", lambda sid, parent: types.SimpleNamespace(action="retry"))
    r = db.enqueue_turn(
        "rturn-1", "sess-1", "claude", "resume_session", {"task": {}}, body="retry",
        operation_id="retry-1", turn_source="system", turn_kind="retry",
        idempotency_scope="automation:sess-1:retry", admission_hash="h-retry",
        parent_task_id=wake["id"], flow_run_id=cid, machine_id="worker-a",
    )
    (m,) = db.pending_for("sess-1").messages
    assert m.state == "delivered" and m.delivery_turn_id == str(r) and m.attempts == 2
    _expire_backoff(db)
    for _ in range(3):
        _tick(o)
    assert _admitted(db) == 1                            # no second wake beside R
    with db._write() as conn:                            # R activates (its token/pause are
        conn.execute("UPDATE mesh_tasks SET status = 'pending' WHERE id = ?", (str(r),))  # A82's)
    _complete(db, str(r))
    assert _msg_states(db) == {"task_w1": "acked"}
