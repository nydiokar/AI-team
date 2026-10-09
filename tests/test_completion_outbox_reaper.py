"""A84 TASK 6 — the lost-carrier reaper (bounded liveness backstop).

A REQUESTED Case worker child whose carrier dies before reporting terminal would
strand its requester forever. The reaper detects such a child by the EXACT
claim-lease / node-truth predicate the protocol-0 claim reaper already uses
(``_claim_staleness_reason``), and synthesizes its terminal outcome through the
SAME atomic terminal seam — so a LOST carrier still produces exactly one
completion message and wakes the requester exactly once.

[A104] Scope is REQUESTED children (``mesh_tasks.sender_session_id`` set — an
agent's inbox waits on them) of any non-terminal Case, regardless of the Case's
birth mode and with no outbox flag; the synthesized terminal writes the
``agent_inbox`` row addressed to the requester.

Proven here against a REAL file-backed ``MeshDB`` (no mocks), and the full
reaper→delivery path driven through the genuine ``TaskOrchestrator`` (H3 harness).

Detection (``list_stale_managed_children``):
  R01 lost carrier (node_missing, lease-expired) is detected
  R02 a fresh claim (within lease) is NOT detected
  R03 a healthy ONLINE carrier (matching incarnation, no live_state) is NOT detected
  R04 an incarnation-mismatch (restarted-in-place) carrier IS detected
  R05 an UNREQUESTED child is never detected; a requested child is (scope is
      the requester — there is no Case birth mode any more)
  R06 a closed Case child is NEVER detected (open-only)

Synthesis + fence (``synthesize_managed_terminal``):
  R10 lost carrier → synth → EXACTLY ONE inbox message to the requester, outcome
      'failed', terminal committed, effects_state='pending'
  R11 late real result after synth (carrier's own token) → idempotent replay,
      NO second message, status unchanged (FENCED)
  R12 real result BEFORE synth → synth returns 'already_terminal', one message (FENCED)
  R13 idempotent re-scan: a second synth returns 'already_terminal', one message;
      the terminal row drops out of the scan
  R14 a missing / non-managed row → 'skipped', no row

Orchestrator reaper (``_reap_lost_carriers``) + delivery:
  R20b a long-running child its LIVE carrier still reports active is never reaped
  R21 reaper synthesizes the lost carrier → the real Wake-Dispatcher wakes the
      requester EXACTLY ONCE presenting the reaped child; ack; re-tick no wake; a
      late real result is fenced (no second message, no second wake)

Deleted with A104: R20 (reaper inert when CASE_COMPLETION_OUTBOX_ENABLED is OFF) —
the reaper is no longer flag- or mode-gated (requested children, any Case).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator

from tests.inbox_seed import seed_child
from tests.test_agent_inbox_delivery import (  # noqa: F401 — autouse fixtures
    _drive, _env, _fresh_allowance, _wakes,
)
from tests.test_case_continuation import _FakeOrch, _FakeStore, _FakeSession
from tests.test_turn_queue_producer1 import _flags, _no_cli_spawn  # noqa: F401


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _stale_ts(seconds_ago: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _db(tmp_path: Any) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _open_outbox_case(db: MeshDB, monkeypatch: pytest.MonkeyPatch, session_id="mgr-sess") -> str:
    # Name kept from A84; A104 Phase 5 deleted the outbox flag + birth-mode marker.
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
    requester: Optional[str] = "mgr-sess",
) -> None:
    """A protocol-1 managed Case worker child, REQUESTED by ``requester`` (its
    ``sender_session_id``), that was claimed by a carrier which is now gone/stale:
    ``claimed_at`` is ``age_sec`` in the past and ``claimed_by`` is (by default) a
    node that does not exist ⇒ 'node_missing'."""
    seed_child(
        db, case_id, task_id, requester=requester, status=status, token=token,
        link=link_as_child,
    )
    conn = db._conn()
    conn.execute(
        "UPDATE mesh_tasks SET claimed_by = ?, claimed_at = ?, claimer_incarnation = ? "
        "WHERE id = ?",
        (claimed_by, _stale_ts(age_sec), claimer_incarnation, task_id),
    )
    conn.commit()


def _inbox_rows(db: MeshDB, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM agent_inbox WHERE case_id = ? ORDER BY about_task_id",
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


def test_R05_unrequested_child_never_detected_any_mode(tmp_path, monkeypatch):
    """[A104] Scope is the requester (A104 Phase 5 deleted the Case birth-mode
    marker): an unrequested child (no inbox waits on it) is never reaped; a
    requested child of the same Case is."""
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-sess", role="manager")
    _seed_lost_child(db, "w0", case_id, token="t0", requester=None)
    assert db.list_stale_managed_children() == []
    _seed_lost_child(db, "w1", case_id, token="t1")
    assert [r["id"] for r in db.list_stale_managed_children()] == ["w1"]


def test_R06_closed_case_child_never_detected(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id)
    db.update_flow_run(case_id, status="closed")
    assert db.list_stale_managed_children() == []


# --------------------------------------------------------------------------- #
# Synthesis + fence                                                            #
# --------------------------------------------------------------------------- #
def test_R10_synth_writes_exactly_one_inbox_message(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id)
    assert db.synthesize_managed_terminal("w1", reason="node_missing") == "synthesized"
    rows = _inbox_rows(db, case_id)
    assert len(rows) == 1
    assert rows[0]["about_task_id"] == "w1"
    assert rows[0]["recipient_session_id"] == "mgr-sess"
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["state"] == "pending"
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
    assert len(_inbox_rows(db, case_id)) == 1
    assert _inbox_rows(db, case_id)[0]["outcome"] == "failed"
    assert _status(db, "w1") == "failed"


def test_R12_real_result_before_synth_is_fenced(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id, token="carrier-tok")
    # The carrier actually finished first.
    db.complete_turn("w1", "carrier-tok", {"output": "real"}, status="completed")
    assert _inbox_rows(db, case_id)[0]["outcome"] == "success"
    # A racing reaper then tries to synthesize — fenced by the terminal guard.
    assert db.synthesize_managed_terminal("w1") == "already_terminal"
    rows = _inbox_rows(db, case_id)
    assert len(rows) == 1 and rows[0]["outcome"] == "success"
    assert _status(db, "w1") == "completed"


def test_R13_idempotent_rescan(tmp_path, monkeypatch):
    db = _db(tmp_path)
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_lost_child(db, "w1", case_id)
    assert db.synthesize_managed_terminal("w1") == "synthesized"
    # Second pass: the row is terminal now ⇒ no-op, no second row.
    assert db.synthesize_managed_terminal("w1") == "already_terminal"
    assert len(_inbox_rows(db, case_id)) == 1
    # And it has dropped out of the stale scan.
    assert db.list_stale_managed_children() == []


def test_R14_missing_or_nonmanaged_row_is_skipped(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _open_outbox_case(db, monkeypatch)
    assert db.synthesize_managed_terminal("no-such-task") == "skipped"


# --------------------------------------------------------------------------- #
# Orchestrator reaper + drain                                                  #
# --------------------------------------------------------------------------- #
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
    db, o = _env(tmp_path, monkeypatch)
    case_id = db.open_case("obj", "sess-1", role="manager")  # any birth mode, no flag
    _seed_lost_child(db, "w1", case_id, token="carrier-tok", requester="sess-1")

    # The reaper runs and synthesizes the lost carrier's terminal.
    assert _reap(o, db) == 1
    assert "case_worker_carrier_reaped" in o.events
    (msg,) = _inbox_rows(db, case_id)
    assert (msg["recipient_session_id"], msg["outcome"], msg["state"]) == ("sess-1", "failed", "pending")

    # The real Wake-Dispatcher wakes the requester EXACTLY ONCE, presenting the
    # reaped child; the wake's completion acks the message.
    ran = _drive(db, o)
    assert len(ran) == 1
    (wake,) = _wakes(db)
    assert wake["session_id"] == "sess-1" and "w1" in wake["prompt"]
    assert [r["state"] for r in _inbox_rows(db, case_id)] == ["acked"]
    assert db.pending_for("sess-1", case_id=case_id).messages == []

    # A late REAL result from the carrier is fenced: no second message, no re-wake.
    res = db.complete_turn("w1", "carrier-tok", {"output": "late"}, status="completed")
    assert res.status == "failed"
    assert len(_inbox_rows(db, case_id)) == 1
    assert _drive(db, o) == []
    assert len(_wakes(db)) == 1
