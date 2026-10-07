"""A84 carry (o) — the outbox DRAIN through the real Wake-Dispatcher tick.

Drives the genuine ``TaskOrchestrator._continue_case_once`` (via the duck-typed
``_FakeOrch`` harness from ``test_case_continuation``) against a REAL file-backed
``MeshDB``. The outbox rows are produced end-to-end by the REAL atomic terminal
write (``complete_turn``), never hand-inserted — so these prove the full path
from "worker child recorded terminal" to "one coalesced Manager wake, drained
exactly once".

D01 two terminal children → ONE coalesced wake presenting both; ACK marks both
    outbox rows delivered(reason='wake'); next tick is a no-op.
D02 a second tick before the ACK does NOT double-wake (deterministic-id claim).
D03 an out-of-band reviewed child is suppressed (delivered 'reviewed_in_turn'),
    no wake owed.
D04 a legacy-mode Case still drains via its wait-group and owns NO outbox rows
    (cross-path isolation).
D05 cutover: a legacy wait-group Case AND a new outbox Case each drain EXACTLY
    once with no cross-path duplicate wake or stranding (ACCEPTANCE 3).
"""
from __future__ import annotations

import socket

import pytest

from src.control.db import MeshDB, continuation_task_id
from src.orchestrator import TaskOrchestrator

from tests.test_case_continuation import (
    _FakeOrch, _FakeStore, _FakeSession, _continue, _finished, _reviewed, _events,
)
from tests.test_completion_outbox import _seed_running_child


class _FakeOrch(_FakeOrch):  # type: ignore[no-redef]
    """Extends the continuation harness with the real outbox-tick method, which
    ``_continue_case_once`` calls on ``self`` for an outbox-mode Case."""

    def _compute_outbox_tick(self, db, case_id):
        return TaskOrchestrator._compute_outbox_tick(self, db, case_id)


def _db(tmp_path) -> MeshDB:
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    return db


def _open_outbox_case(db: MeshDB, monkeypatch, session_id="mgr-sess") -> str:
    monkeypatch.setenv("CASE_COMPLETION_OUTBOX_ENABLED", "1")
    return db.open_case("obj", session_id, role="manager")


def _finish_child(db: MeshDB, case_id: str, task_id: str, token="tok") -> None:
    """Terminal a managed Case worker child through the REAL atomic write, so its
    outbox row is produced by complete_turn (not hand-inserted)."""
    _seed_running_child(db, task_id, case_id, token=token)
    db.complete_turn(task_id, token, {"output": "x"}, status="completed")


def _pending(db: MeshDB, case_id: str):
    return db.pending_case_outbox(case_id)


# --- D01: coalesced single wake + exactly-once drain ---------------------- #
def test_two_children_coalesce_to_one_wake_drained_once(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _finish_child(db, case_id, "w1", token="t-w1")
    _finish_child(db, case_id, "w2", token="t-w2")
    assert {r["child_task_id"] for r in _pending(db, case_id)} == {"w1", "w2"}

    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, case_id) == 1
    # exactly ONE coalesced wake presenting BOTH children
    assert len(orch.deliveries) == 1
    desc = orch.deliveries[0]["description"]
    assert "w1" in desc and "w2" in desc
    cont_id = continuation_task_id(case_id, 1)
    rows = db.list_continuation_rows(case_id)
    assert len(rows) == 1 and rows[0]["id"] == cont_id and rows[0]["status"] == "claimed"

    # Simulate the crash-safe ACK (the stubbed _finalize_continuation would call
    # this): both outbox rows flip to delivered(reason='wake') in that txn.
    db.record_continuation_consumed(case_id, cont_id, 1, ["w1", "w2"])
    out = {r["child_task_id"]: dict(r) for r in db._conn().execute(
        "SELECT * FROM completion_outbox WHERE case_id=?", (case_id,)).fetchall()}
    assert out["w1"]["delivered_at"] and out["w1"]["delivery_reason"] == "wake"
    assert out["w2"]["delivered_at"] and out["w2"]["delivery_reason"] == "wake"
    assert _pending(db, case_id) == []

    # next tick: nothing pending ⇒ no wake
    orch2 = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch2, db, case_id) == 0
    assert orch2.deliveries == []


# --- D02: no double wake before the ACK ----------------------------------- #
def test_second_tick_before_ack_does_not_double_wake(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _finish_child(db, case_id, "w1", token="t-w1")
    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, case_id) == 1
    # A second tick, the ACK not yet applied: same generation ⇒ same cont-id ⇒
    # the atomic claim is lost ⇒ no second wake.
    orch2 = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch2, db, case_id) == 0
    assert orch2.deliveries == []
    assert len(db.list_continuation_rows(case_id)) == 1


# --- D03: out-of-band review suppression ---------------------------------- #
def test_reviewed_child_is_suppressed_without_a_wake(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _finish_child(db, case_id, "w1", token="t-w1")
    _reviewed(db, case_id, "w1", verdict="accepted")  # Manager already adjudicated
    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, case_id) == 0
    assert orch.deliveries == []
    row = db._conn().execute(
        "SELECT * FROM completion_outbox WHERE child_task_id='w1'").fetchone()
    assert row["delivered_at"] and row["delivery_reason"] == "reviewed_in_turn"


# --- D04: legacy Case keeps the wait-group path, owns no outbox rows ------- #
def test_legacy_case_drains_via_wait_group_and_has_no_outbox(tmp_path, monkeypatch):
    monkeypatch.delenv("CASE_COMPLETION_OUTBOX_ENABLED", raising=False)
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("CASE_RESPAWN_REQUIRES_APPROVAL", "0")
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-sess", role="manager")
    assert db.case_continuation_mode(case_id) is None
    db.arm_wait_group(case_id, "g1", "ALL", ["t1", "t2"])
    _finished(db, case_id, "t1")
    _finished(db, case_id, "t2")
    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, case_id) == 1
    assert len(orch.deliveries) == 1
    # legacy Case never writes/reads the outbox
    assert db._conn().execute(
        "SELECT COUNT(*) FROM completion_outbox WHERE case_id=?", (case_id,)
    ).fetchone()[0] == 0


# --- D05: cutover — both paths coexist, each drains exactly once ---------- #
def test_cutover_legacy_and_outbox_each_drain_once_no_cross_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("CASE_RESPAWN_REQUIRES_APPROVAL", "0")
    db = _db(tmp_path)

    # Legacy Case (flag OFF at birth) with a satisfied wait-group.
    monkeypatch.delenv("CASE_COMPLETION_OUTBOX_ENABLED", raising=False)
    legacy = db.open_case("legacy", "mgr-legacy", role="manager")
    db.arm_wait_group(legacy, "g1", "ALL", ["lt1", "lt2"])
    _finished(db, legacy, "lt1")
    _finished(db, legacy, "lt2")

    # New outbox Case (flag ON at birth) with two terminal children.
    outbox = _open_outbox_case(db, monkeypatch, session_id="mgr-outbox")
    _finish_child(db, outbox, "ow1", token="t-ow1")
    _finish_child(db, outbox, "ow2", token="t-ow2")

    assert db.case_continuation_mode(legacy) is None
    assert db.case_continuation_mode(outbox) == "outbox"

    store = _FakeStore(_FakeSession("mgr-legacy"), _FakeSession("mgr-outbox"))

    # Each Case drains EXACTLY once, via its OWN path.
    o1 = _FakeOrch(store)
    assert _continue(o1, db, legacy) == 1
    assert o1.deliveries[0]["session_id"] == "mgr-legacy"

    o2 = _FakeOrch(store)
    assert _continue(o2, db, outbox) == 1
    assert o2.deliveries[0]["session_id"] == "mgr-outbox"
    d = o2.deliveries[0]["description"]
    assert "ow1" in d and "ow2" in d

    # No cross-path bleed: legacy owns no outbox rows; outbox owns no wait groups.
    assert db._conn().execute(
        "SELECT COUNT(*) FROM completion_outbox WHERE case_id=?", (legacy,)).fetchone()[0] == 0
    assert _events(db, outbox, "worker.wait_pending") == []

    # ACK both and re-tick: neither re-wakes (exactly-once, no stranding).
    db.record_continuation_consumed(legacy, continuation_task_id(legacy, 1), 1, ["lt1", "lt2"],
                                    retired_group_ids=["g1"])
    db.record_continuation_consumed(outbox, continuation_task_id(outbox, 1), 1, ["ow1", "ow2"])
    assert _continue(_FakeOrch(store), db, legacy) == 0
    assert _continue(_FakeOrch(store), db, outbox) == 0
    assert _pending(db, outbox) == []
