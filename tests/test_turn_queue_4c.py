"""A82 Stage 4c — producer 3: Case continuation (wake) turns for ENROLLED Manager
sessions — re-seeded for A104: what is waiting for the Manager is its agent inbox
(a completion message per requested child), delivered as ONE coalesced managed
wake turn (``wake_<hex>``) and settled in the wake's own terminal txn.

Real pieces: a file-backed ``MeshDB`` as ``get_db()``, REAL bound orchestrator
methods on a bare instance (``_continue_case_once`` → ``_deliver_inbox`` →
managed admission → lineage; ``interrupt_case`` / ``close_case`` /
``record_review`` / ``stop_managed_session_turn``), the REAL scheduler pass
(activation-time revalidation) and the REAL managed claim/start/complete DB
seams. No backend/CLI (autouse spawn guard from the producer-1 suite).

A104 removed on purpose (tests deleted, not weakened): re-arming a withdrawn /
stopped ``cont:`` token with ``attempt+1`` (Q10b, Q20), ``worker.wait_resolved``
group resolution + token finalization crash window (Q03b), the unenrolled legacy
``submit_instruction`` wake (Q13, Q17).
A104 Phase 5 (the pre-inbox token finalizer is deleted): Q15 (finalizer CAS
fenced to the linked turn) is deleted with it; the settlement fence is now the
inbox's ``delivery_turn_id``-conditioned transitions (test_agent_inbox IB08).
"""
import asyncio

import pytest

from src.control import agent_inbox as ib
from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control.db import continuation_task_id
from src.core.interfaces import Session, SessionStatus as SS
from tests.inbox_seed import seed_child, seed_finished_child
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


def _inbox_case(db, *, finished=("w1",), requester="sess-1"):
    """[A104] A Case whose Manager has one completion waiting in its agent inbox
    per task in ``finished`` (children it requested, driven terminal through the
    real terminal seam)."""
    cid = db.open_case("ship X", "sess-1", role="manager",
                       completion_criteria='{"round_cap": 5}')
    for t in finished:
        seed_finished_child(db, cid, t, requester=requester)
    return cid


def _tick(o, db, cid):
    return asyncio.run(o._continue_case_once(db, cid))


def _cont_rows(db):
    return [r for r in _managed_rows(db) if r["turn_kind"] == "continuation"]


def _complete(db, tid, *, status="completed"):
    tok = _run(db, tid)
    return db.complete_turn(tid, tok, {"success": status == "completed"}, status=status)


def _msg_states(db):
    return {r["about_task_id"]: r["state"] for r in db._conn().execute(
        "SELECT about_task_id, state FROM agent_inbox").fetchall()}


def _expire_backoff(db):
    with db._write() as conn:
        conn.execute("UPDATE agent_inbox SET next_attempt_at = '1970-01-01T00:00:00+00:00' "
                     "WHERE state = 'pending'")


def _expected_wake_id(db, sid="sess-1"):
    return ib.wake_turn_id(sid, db.pending_for(sid).deliverable(ib.now_iso()))


def _running_operator_turn(db, o, op="op-1"):
    t = _submit(o, operation_id=op)
    _pass(db, o)
    _run(db, t)
    return t


# --------------------------------------------------------------------------- #
# Busy Manager: admitted as ONE durable turn, queued behind, no interrupt
# --------------------------------------------------------------------------- #
def test_Q01_busy_manager_gets_one_durable_continuation_queued_behind(tmp_path, monkeypatch):
    """A104: the busy Manager's inbox message is delivered (claimed) on ONE
    deterministic ``wake_`` turn queued behind the active turn — no token row,
    no interrupt — and further ticks never open another admission."""
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _inbox_case(db)
    t = _running_operator_turn(db, o)
    expect = _expected_wake_id(db)
    assert _tick(o, db, cid) == 1
    rows = _cont_rows(db)
    assert [r["id"] for r in rows] == [expect] and expect.startswith("wake_")
    c = rows[0]
    assert c["status"] == "queued" and c["turn_source"] == "system"
    assert c["idempotency_scope"] == "automation:sess-1:continuation"
    assert c["flow_run_id"] == cid  # provenance only (I6): no lineage, no task link
    assert c["lineage_state"] is None
    assert not db.list_flow_links(flow_run_id=cid, entity_type="task", entity_id=c["id"])
    (m,) = db.pending_for("sess-1").messages
    assert (m.state, m.delivery_turn_id, m.attempts) == ("delivered", expect, 1)
    assert db.get_task(continuation_task_id(cid, 1)) is None  # no cont: token at all
    # the active turn is untouched: no cancel, no interrupt control row
    active = db.get_task(t)
    assert active["status"] == "running" and not active["cancel_token"]
    assert not db._conn().execute(
        "SELECT 1 FROM mesh_tasks WHERE action = 'cancel_managed'").fetchone()
    # coalesced: further ticks never mint a second turn — and never even open
    # an admission txn (the in-flight delivery short-circuits them)
    real_enqueue = type(db).enqueue_turn
    admissions = []
    monkeypatch.setattr(type(db), "enqueue_turn",
                        lambda self, *a, **k: admissions.append(k) or real_enqueue(self, *a, **k))
    assert _tick(o, db, cid) == 0 and _tick(o, db, cid) == 0
    assert admissions == []
    assert len(_cont_rows(db)) == 1


def test_Q01b_concurrent_ticks_collapse_to_one_turn(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)

    async def both():
        return await asyncio.gather(o._continue_case_once(db, cid), o._continue_case_once(db, cid))
    assert sum(asyncio.run(both())) == 1
    assert len(_cont_rows(db)) == 1
    (m,) = db.pending_for("sess-1").messages
    assert m.state == "delivered" and m.attempts == 1


# --------------------------------------------------------------------------- #
# Crash windows
# --------------------------------------------------------------------------- #
def test_Q02_crash_between_token_write_and_admission_replays_same_id(tmp_path, monkeypatch):
    """A104: the claim (pending → delivered) is inside the admission txn, so a
    crash before admission leaves the message pending and unclaimed; a fresh
    process replays to the SAME deterministic wake id."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    expect = _expected_wake_id(db)
    real = type(db).enqueue_turn

    def crash(self, *a, **k):
        raise RuntimeError("process died before admission (injected)")
    monkeypatch.setattr(type(db), "enqueue_turn", crash)
    with pytest.raises(RuntimeError):
        _tick(o, db, cid)
    (m,) = db.pending_for("sess-1").messages
    assert (m.state, m.attempts, m.delivery_turn_id) == ("pending", 0, None)
    assert _cont_rows(db) == []
    monkeypatch.setattr(type(db), "enqueue_turn", real)
    o2 = _fresh(o, monkeypatch)
    assert _tick(o2, db, cid) == 1
    assert [r["id"] for r in _cont_rows(db)] == [expect]


def test_Q02b_crash_after_admission_commit_never_mints_a_second_turn(tmp_path, monkeypatch):
    """A104: a wake writes no lineage (I6), so the post-commit window is the
    delivery report; dying there leaves ONE wake carrying the claimed message,
    which a fresh process never duplicates and the scheduler activates."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    real_emit = o._emit_event

    def die(name, task, data=None):
        if name == "case_continuation_delivered":
            raise RuntimeError("process died after the admission commit (injected)")
        return real_emit(name, task, data)
    o._emit_event = die
    with pytest.raises(RuntimeError):
        _tick(o, db, cid)
    rows = _cont_rows(db)
    assert len(rows) == 1 and rows[0]["status"] == "queued"
    (m,) = db.pending_for("sess-1").messages
    assert m.state == "delivered" and m.delivery_turn_id == rows[0]["id"]  # claimed in the same txn
    o2 = _fresh(o, monkeypatch)
    assert _tick(o2, db, cid) == 0
    assert len(_cont_rows(db)) == 1
    res = _pass(db, o2)
    assert res.activated == 1
    assert db.get_task(rows[0]["id"])["status"] == "pending"


# --------------------------------------------------------------------------- #
# Settlement in the wake's own terminal txn (restart-safe), one round per wake
# --------------------------------------------------------------------------- #
def _drive_to_completion(db, o, cid, status="completed"):
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    assert _pass(db, o).activated == 1
    _complete(db, c, status=status)
    return c


def test_Q03_completed_wake_finalized_after_restart_counts_one_round(tmp_path, monkeypatch):
    """A104: the completed wake acks its message in its own terminal txn — a
    process that never saw the admission finds one counted round and nothing
    to finalize or re-admit."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    _drive_to_completion(db, o, cid)
    o2 = _fresh(o, monkeypatch)  # the in-memory world that admitted it is gone
    assert _msg_states(db) == {"w1": "acked"}
    assert db.inbox_rounds_used(cid) == 1
    assert db.pending_for("sess-1").messages == []
    # exactly once: no legacy token exists to finalize, ticks admit nothing
    assert not db._conn().execute(
        "SELECT 1 FROM mesh_tasks WHERE action = 'manager_continuation'").fetchone()
    assert _tick(o2, db, cid) == 0
    assert db.inbox_rounds_used(cid) == 1
    assert len(_cont_rows(db)) == 1


def test_Q04_failed_wake_returns_its_messages_with_backoff(tmp_path, monkeypatch):
    """A104 (was: failed wake consumed like legacy): a failed wake counts no
    round; its message returns to pending with backoff and is redelivered on a
    fresh wake once the backoff elapses."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    c = _drive_to_completion(db, o, cid, status="failed")
    (m,) = db.pending_for("sess-1").messages
    assert (m.state, m.attempts) == ("pending", 1) and m.ready_at > ib.now_iso()
    assert db.inbox_rounds_used(cid) == 0
    assert _tick(o, db, cid) == 0  # backoff holds
    _expire_backoff(db)
    assert _tick(o, db, cid) == 1
    assert [r["id"] for r in _cont_rows(db)][0] == c and len(_cont_rows(db)) == 2


def test_Q05_operator_stopped_wake_rearms_without_a_round_and_respects_the_hold(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    _pass(db, o)
    tok = _run(db, c)
    assert o.stop_managed_session_turn(_sess())[0] is True  # REAL operator stop
    db.complete_turn(c, tok, {"success": False}, status="failed")  # interrupted result
    assert db.get_task(c)["status"] == "cancelled"
    (m,) = db.pending_for("sess-1").messages  # returned, not consumed
    assert (m.state, m.attempts) == ("pending", 1)
    assert db.inbox_rounds_used(cid) == 0  # no round lost/counted
    _expire_backoff(db)
    # held: automation neither admits nor releases — and a HELD Manager is not
    # a dead one (its message is neither killed nor handed to respawn)
    assert _tick(o, db, cid) == 0 and len(_cont_rows(db)) == 1
    assert db.operator_stop_hold("sess-1") == "operator_stop"
    assert _msg_states(db) == {"w1": "pending"}
    # the operator's next send releases the hold; the message is re-admitted
    _submit(o, operation_id="op-after-stop")
    expect = _expected_wake_id(db)
    assert _tick(o, db, cid) == 1
    ids = [r["id"] for r in _cont_rows(db)]
    assert ids == [c, expect] and expect != c
    (m,) = db.pending_for("sess-1").messages
    assert (m.state, m.delivery_turn_id, m.attempts) == ("delivered", expect, 2)


# --------------------------------------------------------------------------- #
# Activation-time revalidation / obsolete withdrawal
# --------------------------------------------------------------------------- #
def test_Q06_intervening_review_withdraws_the_queued_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _inbox_case(db)
    t = _running_operator_turn(db, o)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    # during the operator's turn the Manager reviews w1 out-of-band
    assert o.record_review(cid, verdict="accepted", task_id="w1")["ok"]
    assert _msg_states(db) == {"w1": "acked"}  # the tagged review acks in-txn
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": True})
    res = _pass(db, o)
    assert res.withdrawn == 1 and res.activated == 0
    assert db.get_task(c)["status"] == "withdrawn"
    audit = db.get_turn_revisions(c)
    assert any("scheduler:obsolete:reviewed" in str(a.get("actor")) for a in audit)
    assert db.inbox_rounds_used(cid) == 0
    assert _tick(o, db, cid) == 0  # nothing left to present → no paid turn
    assert len(_cont_rows(db)) == 1


def test_Q07_interrupt_withdraws_queued_automation_but_keeps_human_work(tmp_path, monkeypatch):
    """4b residual 4: queued managed AUTOMATION turns of a blocked Case are
    withdrawn at activation; a human instruction in the same Case still runs."""
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _inbox_case(db)
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
    assert any("case_blocked" in str(a.get("actor")) for a in db.get_turn_revisions(c))
    assert db.get_task(human)["status"] == "pending"  # humans never silently disappear
    assert db.get_task(runtime)["status"] == "pending"  # only the automation principal is withdrawn
    assert any("case_blocked" in str(a.get("actor")) for a in db.get_turn_revisions(w))
    # the blocked Case's message is returned for later, never consumed
    assert _msg_states(db) == {"w1": "pending"}


def test_Q07b_closed_case_withdraws_the_queued_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _inbox_case(db)
    t = _running_operator_turn(db, o)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    out = o.close_case(cid, outcome="cancelled", force=True)  # REAL close
    assert out.get("ok"), out
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": True})
    res = _pass(db, o)
    assert res.withdrawn == 1 and db.get_task(c)["status"] == "withdrawn"
    # A104: the Case-status check runs before the carried-message check, so the
    # withdrawal is audited as the close; the close also killed the message.
    assert any("scheduler:obsolete:case_closed" == str(a.get("actor")) for a in db.get_turn_revisions(c))
    assert db._conn().execute("SELECT state, last_error FROM agent_inbox").fetchone()[:] \
        == ("dead", "case_closed")
    assert _tick(o, db, cid) == 0 and len(_cont_rows(db)) == 1


def test_Q08_manager_rebinding_withdraws_the_stale_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _inbox_case(db)
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
    cid = _inbox_case(db)
    assert _tick(o, db, cid) == 1
    res = _pass(db, o)
    assert res.activated == 1 and res.withdrawn == 0


# --------------------------------------------------------------------------- #
# DB contract: in-txn link, reaper exclusion, index use
# --------------------------------------------------------------------------- #
def test_Q10_link_failure_rolls_the_admission_back(tmp_path, monkeypatch):
    """A104: the wake claims its inbox messages INSIDE the admission txn — a
    claim conflict (another wake already carries the message) rolls the whole
    admission back, leaving the first claim intact. The producer-token link
    (still used by the A82 recovery producers) rolls back the same way when the
    token is finalized or missing."""
    db, _o = _env(tmp_path, monkeypatch)
    _inbox_case(db)
    (m,) = db.pending_for("sess-1").messages
    first = db.enqueue_turn(session_id="sess-1", body="wake", turn_kind="continuation",
                            operation_id="wake-a", inbox_message_ids=[m.message_id],
                            require_enrolled=True)
    with pytest.raises(tq.OwnershipConflictError):
        db.enqueue_turn(session_id="sess-1", body="wake", turn_kind="continuation",
                        operation_id="wake-b", inbox_message_ids=[m.message_id],
                        require_enrolled=True)
    assert [r["id"] for r in _cont_rows(db)] == [str(first)]
    (m,) = db.pending_for("sess-1").messages
    assert (m.state, m.delivery_turn_id, m.attempts) == ("delivered", str(first), 1)
    # producer-token link: a finalized token refuses the link ⇒ nothing admitted
    db.enqueue_task("cont:x:1", session_id=None, machine_id="__manager_continuation__",
                    backend="claude", action="manager_continuation", payload={})
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET status = 'completed' WHERE id = 'cont:x:1'")
    with pytest.raises(tq.OwnershipConflictError):
        db.enqueue_turn(session_id="sess-1", body="wake", turn_kind="continuation",
                        operation_id="cont:x:1#1", producer_token="cont:x:1",
                        require_enrolled=True)
    with pytest.raises(tq.TurnNotFoundError):
        db.enqueue_turn(session_id="sess-1", body="wake", operation_id="k2",
                        producer_token="cont:missing:1", require_enrolled=True)
    assert [r["id"] for r in _managed_rows(db) if r["id"] != "w1"] == [str(first)]


def test_Q11_inbox_wake_writes_no_token_and_is_invisible_to_the_stale_claim_reaper(tmp_path, monkeypatch):
    """A104: the wake is a protocol-1 managed turn; no protocol-0 ``cont:`` token
    is written, so the legacy stale-claim reaper has nothing to re-offer."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    stale = [r["id"] for r in db.list_stale_claims(lease_sec=-1)]
    assert c not in stale and continuation_task_id(cid, 1) not in stale
    assert not db._conn().execute(
        "SELECT 1 FROM mesh_tasks WHERE action = 'manager_continuation'").fetchone()


def test_Q12_reconcile_and_link_lookup_use_the_partial_index(tmp_path, monkeypatch):
    """A104: the hot inbox reads are index-served — the pending read (producer +
    activation check) via ``idx_agent_inbox_pending``, the terminal-txn
    settlement lookup by wake turn via ``idx_agent_inbox_turn``, and the
    activation-time legacy-token lookup via the partial producer-link index."""
    db, _o = _env(tmp_path, monkeypatch)

    def plan(sql, args):
        return " ".join(str(tuple(r)) for r in db._conn().execute("EXPLAIN QUERY PLAN " + sql, args).fetchall())
    pending = plan(ib.PENDING_SQL, ("sess-1", None, None, ib.PENDING_LIMIT))
    assert "idx_agent_inbox_pending" in pending and "SCAN agent_inbox" not in pending
    settle = plan("SELECT message_id, attempts FROM agent_inbox "
                  "WHERE delivery_turn_id = ? AND state = 'delivered' ORDER BY message_id", ("wake_x",))
    assert "idx_agent_inbox_turn" in settle and "SCAN agent_inbox" not in settle
    link = plan("SELECT * FROM mesh_tasks INDEXED BY idx_mesh_tasks_producer_link "
                "WHERE producer_turn_id = ? AND status = 'claimed'", ("wake_x",))
    assert "idx_mesh_tasks_producer_link" in link
    assert db.continuation_token_for_turn("wake_x") is None  # the INDEXED BY query itself is valid


def test_Q14_wake_dispatcher_tick_finalizes_before_evaluating(tmp_path, monkeypatch):
    """A104: after a completed round the REAL dispatcher tick admits nothing
    (settled in the wake's own txn); a NEW requested completion ⇒ round 2."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    _drive_to_completion(db, o, cid)
    o2 = _fresh(o, monkeypatch)
    assert asyncio.run(o2._wake_dispatcher_tick_once()) == 0  # nothing new to wake
    assert _msg_states(db) == {"w1": "acked"} and db.inbox_rounds_used(cid) == 1
    # a NEW finish ⇒ round 2 through the same tick
    seed_finished_child(db, cid, "w2", requester="sess-1")
    assert asyncio.run(o2._wake_dispatcher_tick_once()) == 1
    new = _cont_rows(db)[-1]["id"]
    assert [m.about_task_id for m in db.pending_for("sess-1").carried_by(new)] == ["w2"]


def test_Q16_admission_racing_a_stop_never_releases_or_runs_through_the_hold(tmp_path, monkeypatch):
    """The automation principal, A104 form: a stop that lands between
    ``_deliver_inbox``'s hold check and the wake admission (admission driven
    directly past the check) — the wake is admitted, the hold stays, and the
    wake waits queued after the stopped turn ends."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    t = _running_operator_turn(db, o)
    assert o.stop_managed_session_turn(_sess())[0] is True
    ready = db.pending_for("sess-1", case_id=cid).deliverable(ib.now_iso())
    assert asyncio.run(o._admit_inbox_wake(db, "sess-1", cid, _sess(), ready, 1)) == 1
    assert db.operator_stop_hold("sess-1") == "operator_stop"
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": False}, status="failed")
    assert _pass(db, o).activated == 0
    assert _cont_rows(db)[0]["status"] == "queued"
    assert db.operator_stop_hold("sess-1") == "operator_stop"
    assert _msg_states(db) == {"w1": "delivered"}


# --------------------------------------------------------------------------- #
# Stage 4c rework (A87 review): adopted probes P1/P2/P3b/P5/P6 + kill tests
# --------------------------------------------------------------------------- #
def _rebind_setup(tmp_path, monkeypatch, *, enroll_new):
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _inbox_case(db)
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
    else:  # [A82 Stage 8a] born managed: model the unenrolled S2
        db.unenroll_session_drained("sess-9")
    db.create_flow_link(cid, "session", "sess-9", "manager", created_by="system")
    return db, o, cid, c


def test_Q17b_rebound_enrolled_manager_gets_the_managed_wake(tmp_path, monkeypatch):
    db, o, cid, c = _rebind_setup(tmp_path, monkeypatch, enroll_new=True)
    assert _tick(o, db, cid) == 1
    rows = {r["session_id"]: r for r in _cont_rows(db)}
    assert set(rows) == {"sess-1", "sess-9"}
    assert rows["sess-1"]["status"] == "withdrawn" and rows["sess-9"]["status"] == "queued"
    assert [m.about_task_id for m in db.pending_for("sess-9").carried_by(rows["sess-9"]["id"])] == ["w1"]
    assert db.operator_stop_hold("sess-1") == "operator_stop"
    assert _tick(o, db, cid) == 0 and len(_cont_rows(db)) == 2


def test_Q17c_running_wake_on_the_old_manager_is_not_withdrawn(tmp_path, monkeypatch):
    """A104: a RUNNING wake on the replaced seat keeps its in-flight message —
    no re-address, no second wake — and settles it when it completes."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    _pass(db, o)
    tok = _run(db, c)
    db.upsert_session(Session(session_id="sess-9", backend="claude", repo_path="/tmp/repo",
                              status=SS.AWAITING_INPUT, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    db.enroll_session("sess-9")
    db.create_flow_link(cid, "session", "sess-9", "manager", created_by="system")
    assert _tick(o, db, cid) == 0
    _pass(db, o)
    assert db.get_task(c)["status"] == "running" and len(_cont_rows(db)) == 1
    assert db.complete_turn(c, tok, {"success": True}, status="completed")
    assert _msg_states(db) == {"w1": "acked"}


def test_Q18_multicase_manager_wake_lineage_pinned_to_the_woken_case(tmp_path, monkeypatch):
    """P2 adopted: Manager on open Cases A and B; the wake for A is provenance-
    pinned to A (A104 I6: no task link/attach in either Case) and does not move
    the session's current Case; the round counts for A only."""
    db, o = _env(tmp_path, monkeypatch)
    cid_a = _inbox_case(db)
    cid_b = db.open_case("other", "sess-1", role="manager", completion_criteria='{"round_cap": 5}')
    before = (db.get_session("sess-1") or {}).get("current_case_id")
    assert _tick(o, db, cid_a) == 1
    c = _cont_rows(db)[0]
    assert c["flow_run_id"] == cid_a

    def attached(cid):
        return [e for e in db.list_flow_events(cid)
                if e["event_type"] == "task.attached" and e.get("entity_id") == c["id"]]
    assert attached(cid_a) == [] and attached(cid_b) == []
    for cid in (cid_a, cid_b):
        assert not db.list_flow_links(flow_run_id=cid, entity_type="task", entity_id=c["id"])
    assert (db.get_session("sess-1") or {}).get("current_case_id") == before
    _pass(db, o)
    _complete(db, c["id"])
    assert db.inbox_rounds_used(cid_a) == 1
    assert db.inbox_rounds_used(cid_b) == 0


def test_Q19_finalizer_appends_nothing_after_flow_closed(tmp_path, monkeypatch):
    """P3b adopted: the Manager closes the Case during its wake turn — the
    close kills the in-flight message and the wake's completion appends no
    Case event after ``flow.closed`` (nothing acked, no round counted)."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    _pass(db, o)
    tok = _run(db, c)
    assert o.close_case(cid, outcome="cancelled", force=True).get("ok")
    db.complete_turn(c, tok, {"success": True})
    evs = db.list_flow_events(cid)
    closed_at = max(i for i, e in enumerate(evs)
                    if e["event_type"] in ("flow.closed", "flow.status_changed"))
    assert [e["event_type"] for e in evs[closed_at + 1:]] == []
    assert _msg_states(db) == {"w1": "dead"} and db.inbox_rounds_used(cid) == 0


def test_Q21_rearm_links_the_fresh_presented_set(tmp_path, monkeypatch):
    """P6 adopted (kills MA), A104 form: after a stopped wake returns w1, a
    completion that arrived meanwhile (w2) is carried by the SAME redelivery —
    one wake for both, one round."""
    db, o = _env(tmp_path, monkeypatch)
    cid = db.open_case("x", "sess-1", role="manager", completion_criteria='{"round_cap": 5}')
    seed_child(db, cid, "w2", requester="sess-1", token="t2")
    db.arm_wait_group(cid, "g1", "ANY", ["w1", "w2"])  # ANY holds nothing
    seed_finished_child(db, cid, "w1", requester="sess-1")
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    assert [m.about_task_id for m in db.pending_for("sess-1").carried_by(c)] == ["w1"]
    _pass(db, o)
    tok = _run(db, c)
    o.stop_managed_session_turn(_sess())
    db.complete_turn(c, tok, {"success": False}, status="failed")
    assert db.complete_turn("w2", "t2", {"success": True, "output": "ok"}, status="completed")
    _submit(o, operation_id="rel")
    rel = [r for r in _managed_rows(db) if r["turn_kind"] != "continuation"][-1]["id"]
    _pass(db, o)
    t2 = _run(db, rel)
    db.complete_turn(rel, t2, {"success": True})
    _expire_backoff(db)
    assert _tick(o, db, cid) == 1
    c2 = _cont_rows(db)[-1]["id"]
    assert sorted(m.about_task_id for m in db.pending_for("sess-1").carried_by(c2)) == ["w1", "w2"]
    _pass(db, o)
    _complete(db, c2)
    assert _msg_states(db) == {"w1": "acked", "w2": "acked"}
    assert db.inbox_rounds_used(cid) == 1
    assert _tick(o, db, cid) == 0


def test_Q22_node_offline_wake_returns_its_messages(tmp_path, monkeypatch):
    """Kills MB, A104 form (was: consumed even if never run, legacy parity): a
    wake that never ran (node offline) settles NOT completed ⇒ its message
    returns to pending, no round counted."""
    db, o = _env(tmp_path, monkeypatch)
    cid = _inbox_case(db)
    _drive_to_completion(db, o, cid, status="failed_node_offline")
    (m,) = db.pending_for("sess-1").messages
    assert (m.state, m.attempts) == ("pending", 1)
    assert db.inbox_rounds_used(cid) == 0


def test_Q23_partial_review_does_not_withdraw_the_wake(tmp_path, monkeypatch):
    """Kills MC: the Manager reviewed w1 but w2 is still unresolved ⇒ activate."""
    db, o = _env(tmp_path, monkeypatch, status=SS.BUSY)
    cid = _inbox_case(db, finished=("w1", "w2"))
    t = _running_operator_turn(db, o)
    assert _tick(o, db, cid) == 1
    c = _cont_rows(db)[0]["id"]
    assert sorted(m.about_task_id for m in db.pending_for("sess-1").carried_by(c)) == ["w1", "w2"]
    assert o.record_review(cid, verdict="accepted", task_id="w1")["ok"]
    assert _msg_states(db) == {"w1": "acked", "w2": "delivered"}
    tok = db.get_task(t)["claim_token"]
    db.complete_turn(t, tok, {"success": True})
    res = _pass(db, o)
    assert res.withdrawn == 0 and res.activated == 1
    assert db.get_task(c)["status"] == "pending"


def test_Q18c_pinned_wake_whose_case_closed_before_lineage_runs_standalone(tmp_path, monkeypatch):
    """Reviewer probe P4 (round 2) adopted, A104 form: a wake writes no lineage
    (I6), so the window is admission → activation. The pinned Case A closes
    while A's wake is queued ⇒ withdrawn at activation, never re-routed to Case
    B; nothing written to A after its close or to B at all, no task link in
    either, provenance stays A and the session's current Case is untouched."""
    db, o = _env(tmp_path, monkeypatch)
    cid_a = _inbox_case(db)
    cid_b = db.open_case("other", "sess-1", role="manager", completion_criteria='{"round_cap": 5}')
    before_cur = (db.get_session("sess-1") or {}).get("current_case_id")
    ev_b = len(db.list_flow_events(cid_b))
    assert _tick(o, db, cid_a) == 1
    c = _cont_rows(db)[0]["id"]
    assert db.get_task(c)["status"] == "queued"
    assert o.close_case(cid_a, outcome="cancelled", force=True).get("ok")  # REAL close
    ev_a = len(db.list_flow_events(cid_a))
    o2 = _fresh(o, monkeypatch)
    _pass(db, o2)
    _pass(db, o2)
    assert db.get_task(c)["status"] == "withdrawn"
    assert _tick(o2, db, cid_a) == 0 and _tick(o2, db, cid_b) == 0
    assert len(db.list_flow_events(cid_a)) == ev_a
    assert len(db.list_flow_events(cid_b)) == ev_b
    assert not db.list_flow_links(flow_run_id=cid_a, entity_type="task", entity_id=c)
    assert not db.list_flow_links(flow_run_id=cid_b, entity_type="task", entity_id=c)
    assert db.get_task(c)["flow_run_id"] == cid_a
    assert (db.get_session("sess-1") or {}).get("current_case_id") in (before_cur, None)
    assert _msg_states(db) == {"w1": "dead"}
    assert len(_cont_rows(db)) == 1
