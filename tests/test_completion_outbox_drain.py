"""A84 carry (o) — the completion DRAIN through the real Wake-Dispatcher tick.

[A104] The Case-addressed ``completion_outbox`` drain is superseded by the agent
inbox: a REQUESTED child's terminal writes ONE message addressed to its requester
(``mesh_tasks.sender_session_id``) in the same txn, and the Wake-Dispatcher admits
ONE managed wake turn carrying the ready messages. The surviving guarantees are
proven here on the REAL H3 managed harness (``tests/test_agent_inbox_delivery``):
file-backed ``MeshDB`` as ``get_db()``, real admission, real scheduler pass, real
claim/start/complete. Messages are produced end-to-end by the REAL atomic terminal
write (``complete_turn``), never hand-inserted.

D01 two terminal children → ONE coalesced wake presenting both; the wake's own
    completion acks both messages (resolution 'wake'); next tick is a no-op.
D02 a second tick before the wake completes does NOT double-wake (one wake in
    flight per recipient/Case).
D03 an out-of-band reviewed child is consumed (acked 'reviewed') with no wake.
D05 two Cases with two Managers each drain EXACTLY once, to their own
    requester, presenting only their own children (no cross-path).

Deleted with A104: D04 (a legacy-mode Case drains via its wait-group and owns no
outbox rows) — it pinned the outbox-vs-legacy mode routing, which no longer exists.
A104 Phase 5 deleted the Case ``continuation_mode`` birth marker and the
``CASE_COMPLETION_OUTBOX_ENABLED`` flag: D05 no longer asserts birth modes (there
are none); its surviving per-requester isolation guarantee is kept.
"""
from __future__ import annotations

from src.control.db import MeshDB
from src.core.interfaces import SessionStatus as SS

from tests.inbox_seed import seed_finished_child
from tests.test_agent_inbox_delivery import (  # noqa: F401 — autouse fixtures
    _add_session, _drive, _env, _fresh_allowance, _tick, _wakes,
)
from tests.test_case_continuation import _reviewed
from tests.test_turn_queue_producer1 import _flags, _no_cli_spawn  # noqa: F401


def _open_case(db: MeshDB, session_id="sess-1") -> str:
    return db.open_case("obj", session_id, role="manager")


def _inbox(db: MeshDB, case_id: str) -> dict:
    return {r["about_task_id"]: dict(r) for r in db._conn().execute(
        "SELECT * FROM agent_inbox WHERE case_id = ?", (case_id,)).fetchall()}


# --- D01: coalesced single wake + exactly-once drain ---------------------- #
def test_two_children_coalesce_to_one_wake_drained_once(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    case_id = _open_case(db)
    seed_finished_child(db, case_id, "w1", requester="sess-1")
    seed_finished_child(db, case_id, "w2", requester="sess-1")
    assert {m.about_task_id for m in db.pending_for("sess-1", case_id=case_id).messages} == {"w1", "w2"}

    ran = _drive(db, o)
    # exactly ONE coalesced wake presenting BOTH children
    assert len(ran) == 1
    (wake,) = _wakes(db)
    assert "w1" in wake["prompt"] and "w2" in wake["prompt"]

    # The wake's own terminal (the harness, crash-safe in the terminal txn)
    # acked both messages.
    out = _inbox(db, case_id)
    for t in ("w1", "w2"):
        assert out[t]["state"] == "acked" and out[t]["resolution"] == "wake"
        assert out[t]["delivery_turn_id"] == wake["id"]
    assert db.pending_for("sess-1", case_id=case_id).messages == []

    # next tick: nothing pending ⇒ no wake
    assert _drive(db, o) == []
    assert len(_wakes(db)) == 1


# --- D02: no double wake before the ACK ----------------------------------- #
def test_second_tick_before_ack_does_not_double_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    case_id = _open_case(db)
    seed_finished_child(db, case_id, "w1", requester="sess-1")
    assert _tick(o) == 1
    # Further ticks while the wake is in flight (not yet completed): the message
    # is 'delivered' on it ⇒ no second wake.
    assert _tick(o) == 0 and _tick(o) == 0
    assert len(_wakes(db)) == 1
    assert _inbox(db, case_id)["w1"]["state"] == "delivered"


# --- D03: out-of-band review suppression ---------------------------------- #
def test_reviewed_child_is_suppressed_without_a_wake(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    case_id = _open_case(db)
    seed_finished_child(db, case_id, "w1", requester="sess-1")
    _reviewed(db, case_id, "w1", verdict="accepted")  # Manager already adjudicated
    assert _drive(db, o) == []
    assert _wakes(db) == []
    row = _inbox(db, case_id)["w1"]
    assert row["state"] == "acked" and row["resolution"] == "reviewed"


# --- D05: two Cases / two Managers each drain exactly once ---------------- #
def test_cutover_legacy_and_outbox_each_drain_once_no_cross_path(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    _add_session(db, "sess-2", status=SS.AWAITING_INPUT)

    # Names kept from the pre-A104 cutover test; there is no birth mode any more.
    legacy = db.open_case("legacy", "sess-1", role="manager")
    outbox = _open_case(db, session_id="sess-2")

    seed_finished_child(db, legacy, "lt1", requester="sess-1")
    seed_finished_child(db, legacy, "lt2", requester="sess-1")
    seed_finished_child(db, outbox, "ow1", requester="sess-2")
    seed_finished_child(db, outbox, "ow2", requester="sess-2")

    ran = _drive(db, o)

    # Each Case drains EXACTLY once, to its own requester, presenting only its own children.
    assert len(ran) == 2
    by_session = {w["session_id"]: w for w in _wakes(db)}
    assert set(by_session) == {"sess-1", "sess-2"}
    assert by_session["sess-1"]["flow_run_id"] == legacy
    assert "lt1" in by_session["sess-1"]["prompt"] and "ow1" not in by_session["sess-1"]["prompt"]
    assert by_session["sess-2"]["flow_run_id"] == outbox
    assert "ow1" in by_session["sess-2"]["prompt"] and "lt1" not in by_session["sess-2"]["prompt"]

    # Neither re-wakes (exactly-once, no stranding).
    assert _drive(db, o) == []
    assert {r["state"] for r in _inbox(db, legacy).values()} == {"acked"}
    assert {r["state"] for r in _inbox(db, outbox).values()} == {"acked"}
