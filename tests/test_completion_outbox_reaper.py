"""A84 TASK 6 — the lost-carrier reaper (bounded liveness backstop).

A managed Case worker child whose carrier dies before reporting terminal would
strand its Case Manager forever. The reaper detects such a child by the EXACT
claim-lease / node-truth predicate the protocol-0 claim reaper already uses
(``_claim_staleness_reason``), and synthesizes its terminal outcome through the
SAME atomic outbox seam — so a LOST carrier still produces exactly one
``completion_outbox`` row and wakes the Manager exactly once.

Proven here against a REAL file-backed ``MeshDB`` (no mocks), and the full
reaper→drain path driven through the genuine ``TaskOrchestrator`` methods.

Detection (``list_stale_managed_children``):
  R01 lost carrier (node_missing, lease-expired) is detected
  R02 a fresh claim (within lease) is NOT detected
  R03 a healthy ONLINE carrier (matching incarnation, no live_state) is NOT detected
  R04 an incarnation-mismatch (restarted-in-place) carrier IS detected
  R05 a legacy-mode Case child is NEVER detected (outbox-only)
  R06 a closed Case child is NEVER detected (open-only)

Synthesis + fence (``synthesize_managed_terminal``):
  R10 lost carrier → synth → EXACTLY ONE outbox row, outcome 'failed', terminal
      committed, effects_state='pending'
  R11 late real result after synth (carrier's own token) → idempotent replay,
      NO second outbox row, status unchanged (FENCED)
  R12 real result BEFORE synth → synth returns 'already_terminal', one row (FENCED)
  R13 idempotent re-scan: a second synth returns 'already_terminal', one row;
      the terminal row drops out of the scan
  R14 a missing / non-managed row → 'skipped', no row

Orchestrator reaper (``_reap_lost_carriers``) + drain:
  R20 flag OFF → reaper is inert (no synth, no row)
  R21 flag ON → reaper synthesizes the lost carrier → the real drain wakes the
      Manager EXACTLY ONCE presenting the reaped child; ACK; re-tick no wake; a
      late real result is fenced (no second row, no second wake)
"""
from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

import src.control.db as db_mod
from src.control.db import MeshDB, continuation_task_id
from src.orchestrator import TaskOrchestrator

from tests.test_case_continuation import _FakeStore, _FakeSession, _continue, _reviewed
from tests.test_completion_outbox import _seed_running_child
from tests.test_completion_outbox_drain import _FakeOrch


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _stale_ts(seconds_ago: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _db(tmp_path: Any) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _open_outbox_case(db: MeshDB, monkeypatch: pytest.MonkeyPatch, session_id="mgr-sess") -> str:
    monkeypatch.setenv("CASE_COMPLETION_OUTBOX_ENABLED", "1")
    return db.open_case("obj", session_id, role="manager")


def _seed_lost_child(
    db: MeshDB,
    task_id: str,
    case_id: Optional[str],
    *,
    token: str = "tok-1",
    status: str = "running",
    claimed_by: str = "ghost-node",
    claimer_incarnation: str = "inc-1",
    age_sec: int = 400,
    link_as_child: bool = True,
) -> None:
    """A protocol-1 managed Case worker child that was claimed by a carrier which
    is now gone/stale: ``claimed_at`` is ``age_sec`` in the past and ``claimed_by``
    is (by default) a node that does not exist ⇒ 'node_missing'."""
    _seed_running_child(
        db, task_id, case_id, token=token, status=status, link_as_child=link_as_child,
    )
    conn = db._conn()
    conn.execute(
        "UPDATE mesh_tasks SET claimed_by = ?, claimed_at = ?, claimer_incarnation = ? "
        "WHERE id = ?",
        (claimed_by, _stale_ts(age_sec), claimer_incarnation, task_id),
    )
    conn.commit()


def _outbox_rows(db: MeshDB, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM completion_outbox WHERE case_id = ? ORDER BY child_task_id",
        (case_id,),
    ).fetchall()]


def _status(db: MeshDB, task_id: str) -> str:
    return db._conn().execute(
        "SELECT status FROM mesh_tasks WHERE id = ?", (task_id,)).fetchone()["status"]


def _reap(orch, db) -> int:
    return asyncio.run(TaskOrchestrator._reap_lost_carriers(orch, db))


# --------------------------------------------------------------------------- #
# Detection                                                                    #
# --------------------------------------------------------------------------- #
def test_R01_lost_carrier_is_detected(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id)
    stale = db.list_stale_managed_children()
    assert [r["id"] for r in stale] == ["w1"]
    assert stale[0]["_stale_reason"] == "node_missing"
    assert stale[0]["flow_run_id"] == case_id


def test_R02_fresh_claim_not_detected(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id, age_sec=5)  # within the 300s lease
    assert db.list_stale_managed_children() == []


def test_R03_healthy_online_carrier_not_detected(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    inc = db.upsert_node("node-A", "", 9001, ["claude"], 2, incarnation_id="inc-A")
    # lease-expired but the node is online with a matching incarnation and no
    # live_state ⇒ the compat path returns "not stale" (never over-reap).
    _seed_lost_child(db, "w1", case_id, claimed_by="node-A", claimer_incarnation=inc)
    assert db.list_stale_managed_children() == []


def test_R04_incarnation_mismatch_is_detected(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    db.upsert_node("node-A", "", 9001, ["claude"], 2, incarnation_id="inc-NEW")
    # claimed by an OLD incarnation of a now-restarted-in-place node.
    _seed_lost_child(db, "w1", case_id, claimed_by="node-A", claimer_incarnation="inc-OLD")
    stale = db.list_stale_managed_children()
    assert [r["id"] for r in stale] == ["w1"]
    assert stale[0]["_stale_reason"] == "incarnation_mismatch"


def test_R05_legacy_case_child_never_detected(tmp_path, monkeypatch):
    monkeypatch.delenv("CASE_COMPLETION_OUTBOX_ENABLED", raising=False)
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-sess", role="manager")
    assert db.case_continuation_mode(case_id) is None
    _seed_lost_child(db, "w1", case_id)
    assert db.list_stale_managed_children() == []


def test_R06_closed_case_child_never_detected(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id)
    db.update_flow_run(case_id, status="closed")
    assert db.list_stale_managed_children() == []


# --------------------------------------------------------------------------- #
# Synthesis + fence                                                            #
# --------------------------------------------------------------------------- #
def test_R10_synth_writes_exactly_one_outbox_row(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id)
    assert db.synthesize_managed_terminal("w1", reason="node_missing") == "synthesized"
    rows = _outbox_rows(db, case_id)
    assert len(rows) == 1
    assert rows[0]["child_task_id"] == "w1"
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["delivered_at"] is None
    t = db._conn().execute(
        "SELECT status, error_class, effects_state FROM mesh_tasks WHERE id='w1'").fetchone()
    assert t["status"] == "failed"
    assert t["error_class"] == "carrier_lost"
    assert t["effects_state"] == "pending"


def test_R11_late_real_result_after_synth_is_fenced(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id, token="carrier-tok")
    assert db.synthesize_managed_terminal("w1") == "synthesized"
    # The real carrier reports late with its OWN token ⇒ idempotent replay
    # (OWN06): no exception, no re-write, NO second outbox row.
    res = db.complete_turn("w1", "carrier-tok", {"output": "late"}, status="completed")
    assert res.status == "failed"  # the synthesized terminal stands
    assert len(_outbox_rows(db, case_id)) == 1
    assert _outbox_rows(db, case_id)[0]["outcome"] == "failed"
    assert _status(db, "w1") == "failed"


def test_R12_real_result_before_synth_is_fenced(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id, token="carrier-tok")
    # The carrier actually finished first.
    db.complete_turn("w1", "carrier-tok", {"output": "real"}, status="completed")
    assert _outbox_rows(db, case_id)[0]["outcome"] == "success"
    # A racing reaper then tries to synthesize — fenced by the terminal guard.
    assert db.synthesize_managed_terminal("w1") == "already_terminal"
    rows = _outbox_rows(db, case_id)
    assert len(rows) == 1 and rows[0]["outcome"] == "success"
    assert _status(db, "w1") == "completed"


def test_R13_idempotent_rescan(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id)
    assert db.synthesize_managed_terminal("w1") == "synthesized"
    # Second pass: the row is terminal now ⇒ no-op, no second row.
    assert db.synthesize_managed_terminal("w1") == "already_terminal"
    assert len(_outbox_rows(db, case_id)) == 1
    # And it has dropped out of the stale scan.
    assert db.list_stale_managed_children() == []


def test_R14_missing_or_nonmanaged_row_is_skipped(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _open_outbox_case(db, monkeypatch)
    assert db.synthesize_managed_terminal("no-such-task") == "skipped"


# --------------------------------------------------------------------------- #
# Orchestrator reaper + drain                                                  #
# --------------------------------------------------------------------------- #
def test_R20_reaper_inert_when_flag_off(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)  # Case born outbox-mode
    _seed_lost_child(db, "w1", case_id)
    monkeypatch.delenv("CASE_COMPLETION_OUTBOX_ENABLED", raising=False)  # flag now OFF
    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _reap(orch, db) == 0
    assert _outbox_rows(db, case_id) == []
    assert _status(db, "w1") == "running"


def test_R20b_long_running_child_of_a_live_carrier_is_never_reaped(tmp_path, monkeypatch):
    """Regression: the reaper used a hardcoded 30 min runtime cap, so a Case
    worker turn still ACTIVE on a live carrier was synthesized `failed` after
    30 min. The server cap is now the shared turn hard cap (turn_liveness)."""
    import json

    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    inc = db.upsert_node("node-A", "", 9001, ["opencode-server"], 2, incarnation_id="inc-A")
    _seed_lost_child(db, "w1", case_id, claimed_by="node-A", claimer_incarnation=inc, age_sec=7200)
    live = {"active_tasks": ["w1"], "active_task_details": {"w1": {"started_at": _stale_ts(7200)}}}
    conn = db._conn()
    conn.execute("UPDATE nodes SET live_state = ?, live_state_updated_at = ? WHERE node_id = 'node-A'",
                 (json.dumps(live), _stale_ts(0)))
    conn.commit()
    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _reap(orch, db) == 0
    assert _status(db, "w1") == "running"


def test_R21_reaper_synthesizes_then_drain_wakes_once_and_fences_late(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id, token="carrier-tok")

    # The reaper runs (flag ON) and synthesizes the lost carrier's terminal.
    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _reap(orch, db) == 1
    assert ("case_worker_carrier_reaped", {
        "task_id": "w1", "case_id": case_id, "reason": "node_missing"}) in orch.emitted
    assert len(_outbox_rows(db, case_id)) == 1

    # The real drain wakes the Manager EXACTLY ONCE, presenting the reaped child.
    drain = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(drain, db, case_id) == 1
    assert len(drain.deliveries) == 1
    assert "w1" in drain.deliveries[0]["description"]
    cont_id = continuation_task_id(case_id, 1)

    # ACK (crash-safe consumption) marks the outbox row delivered(reason='wake').
    db.record_continuation_consumed(case_id, cont_id, 1, ["w1"])
    assert db.pending_case_outbox(case_id) == []

    # A late REAL result from the carrier is fenced: no second row, no re-wake.
    res = db.complete_turn("w1", "carrier-tok", {"output": "late"}, status="completed")
    assert res.status == "failed"
    assert len(_outbox_rows(db, case_id)) == 1
    retick = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(retick, db, case_id) == 0
    assert retick.deliveries == []
