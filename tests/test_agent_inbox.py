"""A104 Gate 2 — the agent inbox: schema, agent addressing, ``pending_for`` and the
message state machine.

Real pieces: a file-backed ``MeshDB`` as ``get_db()``, the REAL
``submit_instruction`` → managed admission → lineage path on a bare orchestrator,
the REAL managed claim/start/complete DB seams. No backend/CLI (autouse spawn
guard from the producer-1 suite).

The recipient of a completion is whoever REQUESTED the work (the child turn's
``sender_session_id``), never a role: a human-requested turn, a session's own
turns and system/automation turns produce no inbox row.

IB01 the 2026-10-09 shape: Manager-own turn, operator messages and a wake turn
     produce ZERO rows; the dispatched worker produces exactly ONE, to its requester
IB02 worker→worker dispatch addresses the requesting worker (role-free)
IB03 a human-dispatched task produces no row
IB04 a terminal-write rollback leaves no row (atomic with the terminal flip)
IB05 an un-redeployed dispatcher (no explicit requester) is resolved server-side to
     the Case member executing a turn; ambiguity ⇒ no row (never a guess)
IB06 a session never addresses itself
IB07 ``pending_for`` = undelivered/in-flight messages + outstanding requests, bounded
IB08 state transitions are conditional (idempotent) and attempts are bounded (D3)
IB09 an agent→agent send is a request: its completion lands in the sender's inbox
IB10 migration keeps every ``completion_outbox`` row (none silently dropped)
"""
import sqlite3

import pytest

from src.control import agent_inbox as ib
from src.control import turn_admission as ta
from src.control.db import MeshDB
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
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    s = o.session_store.get("sess-1")
    s.status = SS.AWAITING_INPUT
    o.session_store.save(s)
    return db, o


def _add_session(db, sid):
    db.upsert_session(Session(
        session_id=sid, backend="claude", repo_path="/tmp/repo",
        status=SS.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a",
    ))
    db.enroll_session(sid)


def _finish(db, o, tid, *, status="completed"):
    """Drive one admitted turn to a terminal state through the real seams."""
    _pass(db, o)
    tok = _run(db, tid)
    assert db.complete_turn(tid, tok, {"success": status == "completed"}, status=status)


def _inbox(db):
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM agent_inbox ORDER BY created_at, message_id").fetchall()]


def _dispatch(o, target, cid, *, requester=None, op):
    kw = dict(description=f"work for {target}", session_id=target, source="automation_session",
              join_case_id=cid, operation_id=op)
    if requester is not None:
        kw["requester_session_id"] = requester
    return _submit(o, **kw)


# IB01 ---------------------------------------------------------------------- #
def test_IB01_incident_shape_only_the_dispatched_worker_reaches_the_requester(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "w-1")
    cid = db.open_case("ship X", "sess-1", role="manager")
    # Manager-own turn (the boot turn shape) and two operator messages, all on the
    # Manager session and all Case-linked (the junk the A84 writer addressed).
    boot = _submit(o, description="boot", source="manager_invoke", operation_id="boot")
    _finish(db, o, boot)
    op1 = _submit(o, description="operator says hi", operation_id="op-1")
    _finish(db, o, op1)
    # The real worker child, requested by the Manager session.
    child = _dispatch(o, "w-1", cid, requester="sess-1", op="d-1")
    _finish(db, o, child)
    op2 = _submit(o, description="operator again", operation_id="op-2")
    _finish(db, o, op2)

    rows = _inbox(db)
    assert len(rows) == 1, rows
    (row,) = rows
    assert row["about_task_id"] == str(child)
    assert row["recipient_session_id"] == "sess-1"
    assert row["sender_session_id"] == "w-1"
    assert row["case_id"] == cid
    assert row["kind"] == "completion" and row["state"] == "pending"
    assert row["outcome"] == "success"
    # The child carries its requester; the Manager's own/operator turns carry none.
    tasks = {r["id"]: r for r in db._conn().execute("SELECT id, sender_session_id FROM mesh_tasks")}
    assert tasks[str(child)]["sender_session_id"] == "sess-1"
    for t in (boot, op1, op2):
        assert tasks[str(t)]["sender_session_id"] is None
    assert not [r for r in rows if r["recipient_session_id"] == r["sender_session_id"]]


def test_IB01b_a_wake_turn_completion_produces_no_row(tmp_path, monkeypatch):
    """The wake turn that DELIVERS a message is system work, not a request: its
    completion acks the message it carried and adds NO new inbox row."""
    import asyncio

    from tests.inbox_seed import seed_finished_child

    db, o = _env(tmp_path, monkeypatch)
    cid = db.open_case("ship X", "sess-1", role="manager")
    seed_finished_child(db, cid, "child-1", requester="sess-1")
    (msg,) = _inbox(db)
    assert asyncio.run(o._wake_dispatcher_tick_once()) == 1
    wakes = [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE queue_protocol = 1 AND turn_kind = 'continuation'").fetchall()]
    (wake,) = wakes
    assert _inbox(db)[0]["state"] == "delivered"
    _finish(db, o, wake["id"])
    rows = _inbox(db)
    assert [r["message_id"] for r in rows] == [msg["message_id"]]  # no new row
    assert rows[0]["state"] == "acked" and rows[0]["delivery_turn_id"] == wake["id"]


# IB02 ---------------------------------------------------------------------- #
def test_IB02_worker_to_worker_dispatch_addresses_the_requesting_worker(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    for sid in ("w-1", "w-2"):
        _add_session(db, sid)
    cid = db.open_case("ship X", "sess-1", role="manager")
    first = _dispatch(o, "w-1", cid, requester="sess-1", op="d-1")
    # w-1 (a worker) asks w-2 for work; the reply belongs to w-1, not the Manager.
    second = _dispatch(o, "w-2", cid, requester="w-1", op="d-2")
    _finish(db, o, second)
    rows = _inbox(db)
    assert [(r["about_task_id"], r["recipient_session_id"]) for r in rows] == [(str(second), "w-1")]
    _finish(db, o, first)
    got = {r["about_task_id"]: r["recipient_session_id"] for r in _inbox(db)}
    assert got == {str(second): "w-1", str(first): "sess-1"}


# IB03 ---------------------------------------------------------------------- #
def test_IB03_human_dispatched_task_produces_no_row(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "w-1")
    cid = db.open_case("ship X", "sess-1", role="manager")
    # The operator types into the worker session directly (web): no requester.
    t = _submit(o, description="human ask", session_id="w-1", join_case_id=cid, operation_id="h-1")
    _finish(db, o, t)
    assert _inbox(db) == []


# IB04 ---------------------------------------------------------------------- #
def test_IB04_terminal_rollback_leaves_no_row(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "w-1")
    cid = db.open_case("ship X", "sess-1", role="manager")
    child = _dispatch(o, "w-1", cid, requester="sess-1", op="d-1")
    _pass(db, o)
    tok = _run(db, child)

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error (injected after the inbox write)")

    monkeypatch.setattr(db, "_commit_completion_identity", _boom)
    with pytest.raises(Exception):
        db.complete_turn(child, tok, {"success": True}, status="completed")
    assert _inbox(db) == []
    assert db.get_task(child)["status"] == "running"


# IB05 ---------------------------------------------------------------------- #
def test_IB05_requester_resolved_server_side_from_the_executing_case_member(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "w-1")
    cid = db.open_case("ship X", "sess-1", role="manager")
    # The Manager is executing a turn (the one that calls dispatch_worker); an old
    # mcp_manager sends no requester id.
    mgr_turn = _submit(o, description="operator: go", operation_id="op-1")
    _pass(db, o)
    _run(db, mgr_turn)
    child = _dispatch(o, "w-1", cid, op="d-1")
    row = db._conn().execute("SELECT sender_session_id FROM mesh_tasks WHERE id = ?", (str(child),)).fetchone()
    assert row["sender_session_id"] == "sess-1"


def test_IB05b_ambiguous_or_absent_executor_resolves_to_nobody(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    for sid in ("w-1", "w-2"):
        _add_session(db, sid)
    cid = db.open_case("ship X", "sess-1", role="manager")
    # Nobody executing ⇒ no requester (never a role lookup).
    child = _dispatch(o, "w-1", cid, op="d-1")
    row = db._conn().execute("SELECT sender_session_id FROM mesh_tasks WHERE id = ?", (str(child),)).fetchone()
    assert row["sender_session_id"] is None
    events = [e for e in db.list_flow_events(cid) if e["event_type"] == "inbox.requester_unresolved"]
    assert len(events) == 1 and events[0]["entity_id"] == str(child)


# IB06 ---------------------------------------------------------------------- #
def test_IB06_a_session_never_addresses_itself(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    cid = db.open_case("ship X", "sess-1", role="manager")
    t = _submit(o, description="self", session_id="sess-1", source="automation_session",
                join_case_id=cid, operation_id="s-1", requester_session_id="sess-1")
    _finish(db, o, t)
    assert _inbox(db) == []
    row = db._conn().execute("SELECT sender_session_id FROM mesh_tasks WHERE id = ?", (str(t),)).fetchone()
    assert row["sender_session_id"] is None


# IB07 ---------------------------------------------------------------------- #
def test_IB07_pending_for_returns_messages_and_outstanding_requests(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    for sid in ("w-1", "w-2"):
        _add_session(db, sid)
    cid = db.open_case("ship X", "sess-1", role="manager")
    done = _dispatch(o, "w-1", cid, requester="sess-1", op="d-1")
    running = _dispatch(o, "w-2", cid, requester="sess-1", op="d-2")
    _finish(db, o, done)
    view = db.pending_for("sess-1")
    assert isinstance(view, ib.PendingView)
    assert [m.about_task_id for m in view.messages] == [str(done)]
    assert view.outstanding_task_ids == [str(running)]
    assert db.pending_for("sess-1", case_id="other-case").messages == []
    assert db.pending_for("w-1").messages == [] and db.pending_for("w-1").outstanding_task_ids == []
    # Index-served: the pending read never scans the table.
    plan = " ".join(r[3] for r in db._conn().execute(
        "EXPLAIN QUERY PLAN " + ib.PENDING_SQL, ("sess-1", None, None, ib.PENDING_LIMIT)).fetchall())
    assert "idx_agent_inbox_pending" in plan and "SCAN agent_inbox" not in plan


# IB08 ---------------------------------------------------------------------- #
def test_IB08_transitions_are_conditional_and_bounded(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "w-1")
    cid = db.open_case("ship X", "sess-1", role="manager")
    child = _dispatch(o, "w-1", cid, requester="sess-1", op="d-1")
    _finish(db, o, child)
    (msg,) = db.pending_for("sess-1").messages
    mid = msg.message_id
    for attempt in range(1, ib.MAX_ATTEMPTS + 1):
        with db._write() as conn:
            assert ib.deliver(conn, [mid], f"wake-{attempt}", ib.now_iso()) == 1
            assert ib.deliver(conn, [mid], "wake-dup", ib.now_iso()) == 0  # single-flight
        with db._write() as conn:
            settled = ib.settle_turn(conn, f"wake-{attempt}", "withdrawn", ib.now_iso())
        (row,) = _inbox(db)
        assert row["attempts"] == attempt
        if attempt < ib.MAX_ATTEMPTS:
            assert row["state"] == "pending" and settled.returned == [mid] and settled.dead == []
            assert row["next_attempt_at"] > row["updated_at"]  # backoff
            assert db.pending_for("sess-1").messages[0].ready_at == row["next_attempt_at"]
        else:
            assert row["state"] == "dead" and row["last_error"] == "attempts_exhausted"
            assert settled.dead == [mid] and settled.returned == []
    # Settling the same turn again is a no-op (conditional update).
    with db._write() as conn:
        again = ib.settle_turn(conn, f"wake-{ib.MAX_ATTEMPTS}", "withdrawn", ib.now_iso())
    assert again.returned == [] and again.dead == [] and again.acked == []


def test_IB08b_completed_wake_acks_and_tagged_review_acks_without_a_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    for sid in ("w-1", "w-2"):
        _add_session(db, sid)
    cid = db.open_case("ship X", "sess-1", role="manager")
    a = _dispatch(o, "w-1", cid, requester="sess-1", op="d-1")
    b = _dispatch(o, "w-2", cid, requester="sess-1", op="d-2")
    _finish(db, o, a)
    _finish(db, o, b)
    ma, mb = (m.message_id for m in db.pending_for("sess-1").messages)
    with db._write() as conn:
        ib.deliver(conn, [ma], "wake-1", ib.now_iso())
        assert ib.settle_turn(conn, "wake-1", "completed", ib.now_iso()).acked == [ma]
        assert ib.ack_about_task(conn, str(b), ib.now_iso(), reason="reviewed") == 1
        assert ib.ack_about_task(conn, str(b), ib.now_iso(), reason="reviewed") == 0
    assert {r["state"] for r in _inbox(db)} == {"acked"}
    assert db.pending_for("sess-1").messages == []


def test_IB08c_case_close_kills_pending_with_a_reason(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "w-1")
    cid = db.open_case("ship X", "sess-1", role="manager")
    child = _dispatch(o, "w-1", cid, requester="sess-1", op="d-1")
    _finish(db, o, child)
    with db._write() as conn:
        assert ib.kill_case(conn, cid, "case_closed", ib.now_iso()) == 1
    (row,) = _inbox(db)
    assert row["state"] == "dead" and row["last_error"] == "case_closed"


# IB09 ---------------------------------------------------------------------- #
def test_IB09_completion_is_addressed_by_the_persisted_requester_only(tmp_path, monkeypatch):
    """The terminal writer reads the child's persisted requester column, whichever
    path set it (dispatch or an agent send): role and Case membership play no part."""
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "w-1")
    t = _submit(o, description="w-1 asked the Manager", session_id="sess-1", operation_id="a-1")
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET sender_session_id = 'w-1' WHERE id = ?", (str(t),))
    _finish(db, o, t)
    (row,) = _inbox(db)
    assert (row["about_task_id"], row["recipient_session_id"], row["sender_session_id"]) == (str(t), "w-1", "sess-1")
    assert row["case_id"] is None  # Case is provenance only; none here


# IB10 ---------------------------------------------------------------------- #
def test_IB10_migration_keeps_every_outbox_row(tmp_path):
    """A DB at schema 44 with outbox rows: opening it migrates every row into the
    inbox (delivered → acked; undelivered → dead(unaddressed_pre_inbox), to be
    re-seeded by the Phase 4 migration) — nothing is dropped."""
    path = str(tmp_path / "mesh.db")
    db = MeshDB(path)
    with db._write() as conn:
        conn.execute("DELETE FROM agent_inbox")
        conn.execute("DELETE FROM schema_version WHERE version >= 45")
        for tid, delivered in (("t-a", None), ("t-b", "2026-10-09T15:00:00+00:00")):
            conn.execute(
                "INSERT INTO completion_outbox (child_task_id, case_id, outcome, created_at, delivered_at, delivery_reason) "
                "VALUES (?, 'case-1', 'success', '2026-10-09T14:00:00+00:00', ?, ?)",
                (tid, delivered, "wake" if delivered else None))
        conn.execute("DROP TABLE IF EXISTS inbox_wait_filters")
        conn.execute("DROP TABLE agent_inbox")
    reopened = MeshDB(path)
    rows = {r["about_task_id"]: dict(r) for r in reopened._conn().execute("SELECT * FROM agent_inbox")}
    assert set(rows) == {"t-a", "t-b"}
    assert rows["t-a"]["state"] == "dead" and rows["t-a"]["last_error"] == "unaddressed_pre_inbox"
    assert rows["t-b"]["state"] == "acked"


# IB11 ---------------------------------------------------------------------- #
def test_IB11_http_requester_field_is_honoured_only_for_automation(monkeypatch):
    """`/api/instructions` forwards ``requester_session_id`` only for the automation
    principal (dispatch_worker); an operator request never names a requester."""
    import asyncio

    from src.control import control_api as api

    seen = []

    class _Orch:
        async def submit_instruction(self, **kw):
            seen.append(kw.get("requester_session_id"))
            return "t-1"

    body = api.InstructionBody(description="x", session_id="w-1", case_id="c-1",
                               requester_session_id="mgr-1")
    session = type("S", (), {"session_id": "w-1", "repo_path": "/tmp"})()
    asyncio.run(api._submit_managed_instruction(_Orch(), body, session, None, principal="automation"))
    asyncio.run(api._submit_managed_instruction(_Orch(), body, session, None, principal=None))
    assert seen == ["mgr-1", None]
