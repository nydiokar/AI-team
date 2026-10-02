"""A82 pre-cutover rework (review round 1) — F2/F3/F4/F6 + the file-only
control-row evidence gap. Real pieces as in ``test_turn_queue_precutover``:
file-backed ``MeshDB`` as ``get_db()``, REAL bound orchestrator methods on a
bare instance, the REAL control API app, the REAL task server + carrier. No
CLI / network (autouse spawn guard).
"""
import asyncio
import itertools
import types

import pytest

from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.core.interfaces import Session, SessionStatus
from src.services.session_service import SessionService
from tests.test_turn_queue_producer1 import (  # noqa: F401
    NOW, _client, _flags, _managed_rows, _no_cli_spawn, _setup,
)


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch):
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


# --------------------------------------------------------------------------- #
# F2 — a refused Manager invoke leaves nothing open; retries never multiply
# --------------------------------------------------------------------------- #
def _manager_orch(tmp_path, monkeypatch, machine):
    """Born-managed Manager sessions (``mgr-1``, ``mgr-2`` …) created by a REAL
    SessionService whose close path is the orchestrator's managed close."""
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    o._manager_role_enabled = lambda: True
    svc = SessionService(o.session_store, remote_close_dispatcher=o._dispatch_remote_close,
                         managed_close=o._close_managed_session)
    ids = itertools.count(1)

    def create_session(**kw):
        sid = f"mgr-{next(ids)}"
        s = Session(session_id=sid, backend=kw.get("backend") or "claude", repo_path=kw["repo_path"],
                    status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id=machine)
        db.upsert_session(s)
        db.enroll_session(sid)
        return types.SimpleNamespace(ok=True, session=o.session_store.get(sid))

    svc.create_session = create_session
    o.session_service = svc
    return db, o


def _cases(db):
    return [dict(r) for r in db._conn().execute("SELECT flow_run_id, status FROM flow_runs").fetchall()]


def _open_sessions(db):
    return [r["session_id"] for r in db._conn().execute(
        "SELECT session_id, status FROM sessions WHERE session_id LIKE 'mgr-%'").fetchall()
        if r["status"] != SessionStatus.CLOSED.value]


def test_F2_unregistered_carrier_refuses_before_any_case_and_leaves_no_open_session(tmp_path, monkeypatch):
    db, o = _manager_orch(tmp_path, monkeypatch, machine="ghost")
    with pytest.raises(tq.CarrierUnavailableError):
        asyncio.run(o.invoke_manager("ship X", repo_path="/tmp/repo", node_id="ghost"))
    assert _cases(db) == [], "the refusal happens before open_case"
    assert _open_sessions(db) == [], "the created Manager session is closed, never orphaned"
    assert _managed_rows(db) == []


def test_F2_turn_queue_refusal_after_case_opened_cancels_the_case_and_closes_the_session(tmp_path, monkeypatch):
    db, o = _manager_orch(tmp_path, monkeypatch, machine="worker-a")

    async def refused(**_kw):
        raise tq.CapacityError("fleet waiting cap reached", retry_after=5)

    o.submit_instruction = refused
    with pytest.raises(tq.CapacityError):
        asyncio.run(o.invoke_manager("ship X", repo_path="/tmp/repo", node_id="worker-a"))
    cases = _cases(db)
    assert len(cases) == 1 and cases[0]["status"] == "cancelled"
    assert _open_sessions(db) == []


def test_F2_api_manager_maps_refusal_to_structured_503_and_retries_never_multiply(tmp_path, monkeypatch):
    db, o = _manager_orch(tmp_path, monkeypatch, machine="ghost")
    client = _client(monkeypatch, o)
    headers = {"Authorization": "Bearer tok", "Idempotency-Key": "inv-1"}
    body = {"objective": "ship X", "repo_path": "/tmp/repo", "node_id": "ghost"}
    for _ in range(3):
        r = client.post("/api/manager", headers=headers, json=body)
        assert r.status_code == 503, r.text
        assert r.json()["detail"]["reason"] == "carrier_unavailable"
    assert _cases(db) == []
    assert _open_sessions(db) == []
