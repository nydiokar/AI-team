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


# --------------------------------------------------------------------------- #
# F4 — a definitively missing staged file is a terminal failure, not a loop
# --------------------------------------------------------------------------- #
def _http_error(path, code):
    import urllib.error

    return urllib.error.HTTPError(f"http://gw{path}", code, "err", {}, None)


class _CodeHTTP:
    def __init__(self, code):
        self.code, self.calls = code, []

    def get_bytes(self, path, timeout=60):
        self.calls.append(("GET", path))
        raise _http_error(path, self.code)

    def delete(self, path, timeout=10):
        self.calls.append(("DELETE", path))
        return {"status": "deleted"}


@pytest.mark.parametrize("code, error_class", [(404, "staged_file_missing"), (503, "managed_conflict")])
def test_F4_missing_staged_file_is_terminal_transient_error_still_requeues(tmp_path, code, error_class):
    from src.worker import agent as agent_mod
    from tests.test_turn_queue_precutover import STAGED, _staged_row, _StagedBackend

    repo = tmp_path / "repo"
    repo.mkdir()
    b = _StagedBackend(repo)
    http = _CodeHTTP(code)
    own = tq.ManagedTurnOwnership(task_id="t-1", session_id="s", node_id="n", claim_token="tok")
    out = asyncio.run(agent_mod._execute_task(_staged_row(repo), {"claude": b}, http, ownership=own))
    assert b.seen == [] and out["success"] is False
    assert out["error_class"] == error_class
    assert STAGED["file_id"] in out["errors"][0]
    assert ("DELETE", f"/files/{STAGED['file_id']}") not in http.calls


def test_F4_missing_staged_file_through_real_carrier_fails_the_turn_visibly(tmp_path, monkeypatch):
    """Real task server + carrier ``_handle_task`` with urllib-shaped errors:
    the staged file was never there (``GET /files/{id}`` → 404) ⇒ the turn is
    ``failed`` with a visible ``staged_file_missing`` error — never re-offered."""
    from fastapi.testclient import TestClient

    import src.control.db as db_mod
    import src.control.node_registry as nr_mod
    from src.control import task_server as tsrv
    from tests.test_turn_queue_carrier_integration import NODE, TOKEN, _ClientHTTP, _worker
    from tests.test_turn_queue_precutover import _staged_row, _StagedBackend

    mdb = db_mod.MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(tsrv, "get_db", lambda: mdb)
    monkeypatch.setattr(db_mod, "get_db", lambda: mdb)
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    monkeypatch.setattr(tsrv, "_worker_token", lambda: TOKEN)
    monkeypatch.setattr(tsrv, "_STAGING_ROOT", tmp_path / "state" / "uploads")

    class _HTTP(_ClientHTTP):
        def get_bytes(self, path, timeout=60):
            self.calls.append(("GET", path, None))
            resp = self.client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
            if resp.status_code >= 400:
                raise _http_error(path, resp.status_code)
            return resp.content

    http = _HTTP(TestClient(tsrv.app))
    w = _worker(tmp_path, http)
    repo = tmp_path / "repo"
    repo.mkdir()
    b = _StagedBackend(repo)
    w._backends = {"claude": b}
    mdb.upsert_session(Session(session_id="sess-1", backend="claude", repo_path=str(repo),
                               status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW,
                               machine_id=NODE, backend_session_id="n"))
    mdb.enroll_session("sess-1")
    payload = _staged_row(repo)["payload"]
    payload["task_id"] = "t-1"
    mdb.enqueue_turn(task_id="t-1", session_id="sess-1", backend="claude",
                     action="resume_session", payload=payload, turn_source="human",
                     turn_kind="instruction", machine_id=NODE)
    mdb.activate_turn("t-1")

    async def scenario():
        rows = [r for r in await w._fetch_pending() if r["id"] == "t-1"]
        assert rows
        await w._handle_task(rows[0])

    asyncio.run(scenario())
    row = mdb.get_task("t-1")
    assert b.seen == []
    assert row["status"] == "failed", row["status"]
    assert "staged_file_missing" in (row.get("error") or "") + str(row.get("result") or "")
