"""A101 — session-window block-state read-model (``_session_block_state`` surfaced
through ``list_turn_requests``).

Real file-backed ``MeshDB``; no network / paid CLI. Every hold is produced with
the SAME ledger shape the scheduler reads (flow_events / flow_links / managed
rows) and asserted two ways:
  * the projection reports the right ``blocked`` / ``pause_reason`` /
    ``resume_case_id`` in ``list_turn_requests`` (the read-model the session
    window consumes), and
  * for the gate-covered holds, ``select_eligible_turn_heads`` AGREES — the head
    is withheld exactly when the projection says "blocked" and eligible when it
    says "clear". This ties the read-only mirror to the real gate, so a future
    gate edit that diverged from the projection breaks a test.

No managed-path behaviour is exercised or changed here; this is a pure read.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc).isoformat()
SID = "sess-a101"
CASE = "case-a101"


def _db(tmp_path: Any) -> MeshDB:
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(Session(
        session_id=SID, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a",
    ))
    db.enroll_session(SID)
    return db


def _enqueue(db: MeshDB, task_id: str = "turn-1", **kw: Any) -> str:
    adm = db.enqueue_turn(
        task_id=task_id, session_id=SID, backend="claude", action="resume_session",
        payload={"task_id": task_id, "prompt": "continue"},
        turn_source="human", turn_kind="instruction", **kw,
    )
    return getattr(adm, "task_id", None) or task_id


def _head_ids(db: MeshDB) -> set[str]:
    return {r["id"] for r in db.select_eligible_turn_heads(limit=25, now=NOW)}


def _state(db: MeshDB) -> dict[str, Any]:
    page = db.list_turn_requests(SID)
    return {k: page[k] for k in ("blocked", "pause_reason", "resume_case_id")}


# --------------------------------------------------------------------------- #
# Baseline — a plain queued head is NOT blocked and IS eligible.
# --------------------------------------------------------------------------- #
def test_clear_head_not_blocked(tmp_path: Any) -> None:
    db = _db(tmp_path)
    tid = _enqueue(db, flow_run_id=CASE)
    assert _state(db) == {"blocked": False, "pause_reason": None, "resume_case_id": None}
    assert tid in _head_ids(db)  # the gate would activate it


# --------------------------------------------------------------------------- #
# 1. Quota pause — the incident. Carries the resume decision (resume_case_id).
# --------------------------------------------------------------------------- #
def test_quota_pause_surfaces_decision(tmp_path: Any) -> None:
    db = _db(tmp_path)
    db.create_flow_link(CASE, "session", SID, "manager")
    tid = _enqueue(db, flow_run_id=CASE)
    db.append_flow_event(CASE, "flow.quota_paused", "system",
                         entity_type="task", entity_id="cturn-x")
    assert _state(db) == {"blocked": True, "pause_reason": "quota", "resume_case_id": CASE}
    assert tid not in _head_ids(db)  # the real gate withholds it


def test_quota_resumed_clears(tmp_path: Any) -> None:
    db = _db(tmp_path)
    db.create_flow_link(CASE, "session", SID, "manager")
    tid = _enqueue(db, flow_run_id=CASE)
    db.append_flow_event(CASE, "flow.quota_paused", "system")
    db.append_flow_event(CASE, "flow.quota_resumed", "system")  # latest wins
    assert _state(db)["blocked"] is False
    assert tid in _head_ids(db)


# --------------------------------------------------------------------------- #
# 2. Transient provider pause — auto-clears, no resume affordance.
# --------------------------------------------------------------------------- #
def test_transient_pause(tmp_path: Any) -> None:
    db = _db(tmp_path)
    db.create_flow_link(CASE, "session", SID, "manager")
    tid = _enqueue(db, flow_run_id=CASE)
    db.append_flow_event(CASE, "flow.transient_paused", "system")
    assert _state(db) == {"blocked": True, "pause_reason": "transient", "resume_case_id": None}
    assert tid not in _head_ids(db)


# --------------------------------------------------------------------------- #
# 3. Pending retry-pause on a managed row.
# --------------------------------------------------------------------------- #
def test_retry_pause(tmp_path: Any) -> None:
    db = _db(tmp_path)
    tid = _enqueue(db, flow_run_id=CASE)
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET retry_pause_state = 'pending' WHERE id = ?", (tid,))
    assert _state(db) == {"blocked": True, "pause_reason": "retry", "resume_case_id": None}
    assert tid not in _head_ids(db)


# --------------------------------------------------------------------------- #
# 4. Manager rebind — a newer manager-role link for the Case supersedes this
#    session; the queued turn must not run as stale work here.
# --------------------------------------------------------------------------- #
def test_manager_rebound(tmp_path: Any) -> None:
    db = _db(tmp_path)
    db.create_flow_link(CASE, "session", SID, "manager")
    tid = _enqueue(db, flow_run_id=CASE)
    db.create_flow_link(CASE, "session", "sess-new-manager", "manager")  # newer seat
    assert _state(db) == {"blocked": True, "pause_reason": "manager_rebound",
                          "resume_case_id": None}
    assert tid not in _head_ids(db)


# --------------------------------------------------------------------------- #
# 5. Carrier offline — per-row blocked_reason set by admission/scheduler.
# --------------------------------------------------------------------------- #
def test_carrier_offline(tmp_path: Any) -> None:
    db = _db(tmp_path)
    tid = _enqueue(db, flow_run_id=CASE)
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET blocked_reason = 'carrier_offline:horse' WHERE id = ?",
                     (tid,))
    assert _state(db) == {"blocked": True, "pause_reason": "carrier_offline",
                          "resume_case_id": None}


# --------------------------------------------------------------------------- #
# 6. Backoff / not-before — a future deadline holds the head.
# --------------------------------------------------------------------------- #
def test_backoff_blocked_until(tmp_path: Any) -> None:
    db = _db(tmp_path)
    tid = _enqueue(db, flow_run_id=CASE)
    future = "2099-01-01T00:00:00+00:00"  # future regardless of the real clock
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET blocked_until = ? WHERE id = ?", (future, tid))
    assert _state(db) == {"blocked": True, "pause_reason": "backoff", "resume_case_id": None}
    assert tid not in _head_ids(db)


# --------------------------------------------------------------------------- #
# 7. Legacy work draining — a still-live protocol-0 EXECUTION row owns the
#    session process; no managed turn starts beside it.
# --------------------------------------------------------------------------- #
def test_legacy_draining(tmp_path: Any) -> None:
    db = _db(tmp_path)
    _enqueue(db, flow_run_id=CASE)
    with db._write() as conn:
        conn.execute(
            "INSERT INTO mesh_tasks(id,session_id,backend,action,payload,status,"
            "queue_protocol,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("legacy-x", SID, "claude", "resume_session", "{}", "claimed", 0, NOW, NOW),
        )
    assert _state(db) == {"blocked": True, "pause_reason": "legacy_draining",
                          "resume_case_id": None}


# --------------------------------------------------------------------------- #
# 8. Lineage not yet committed.
# --------------------------------------------------------------------------- #
def test_lineage_pending(tmp_path: Any) -> None:
    db = _db(tmp_path)
    tid = _enqueue(db, flow_run_id=CASE)
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET lineage_state = 'pending' WHERE id = ?", (tid,))
    assert _state(db) == {"blocked": True, "pause_reason": "lineage", "resume_case_id": None}
    assert tid not in _head_ids(db)


# --------------------------------------------------------------------------- #
# Operator-column holds unify into pause_reason too (already in the overlay).
# --------------------------------------------------------------------------- #
def test_operator_pause(tmp_path: Any) -> None:
    db = _db(tmp_path)
    _enqueue(db, flow_run_id=CASE)
    db.set_turn_queue_paused(SID, True)
    st = _state(db)
    assert st["blocked"] is True and st["pause_reason"] == "operator_pause"


def test_no_queued_row_not_blocked(tmp_path: Any) -> None:
    db = _db(tmp_path)  # enrolled, but nothing queued
    db.create_flow_link(CASE, "session", SID, "manager")
    db.append_flow_event(CASE, "flow.quota_paused", "system")
    # A quota pause with no queued message does not render a held-message banner.
    assert _state(db) == {"blocked": False, "pause_reason": None, "resume_case_id": None}


# --------------------------------------------------------------------------- #
# Integration: the fields reach the HTTP JSON through the real route + Pydantic
# model (TurnRequestPageOut) — the exact payload the session window consumes.
# --------------------------------------------------------------------------- #
def test_turn_requests_route_exposes_block_state(tmp_path: Any, monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient
    import src.control.db as db_mod
    from src.control import control_api
    from src.orchestrator import TaskOrchestrator

    db = _db(tmp_path)
    db.create_flow_link(CASE, "session", SID, "manager")
    _enqueue(db, flow_run_id=CASE)
    db.append_flow_event(CASE, "flow.quota_paused", "system")

    token = "a101-admin"
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    monkeypatch.setattr(control_api, "_db", lambda: db)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: token)
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    client = TestClient(control_api.build_control_api(orch))

    resp = client.get(f"/api/sessions/{SID}/turn-requests",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["blocked"] is True
    assert body["pause_reason"] == "quota"
    assert body["resume_case_id"] == CASE
