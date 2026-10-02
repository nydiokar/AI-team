"""[A82 Stage 4e] A83 managed-respawn safety checks, end to end.

Producer 7's respawn turn is driven by the GENUINE pieces only:
``TaskOrchestrator._respawn_manager_managed`` (admission + durable link) on a
real file-backed ``MeshDB``, the REAL scheduler pass (``_prepare_managed_turn``
revalidation → activation), and the GENUINE worker carrier
(``WorkerAgent._handle_task`` → ``_claim_and_start_managed`` →
``/claim-managed`` / ``/start-managed`` on the in-process task server). Only the
backend execution (``_execute_task``) is a counting fake and the transport is
the in-process TestClient with injected faults:

  (a) a LOST start acknowledgement (the server committed ``running``; the
      response never arrived) grants NO second backend invocation — neither
      through the carrier's start retry nor a producer re-tick;
  (b) a carrier crash between claim and start, then a restart (new
      incarnation), refuses the old grant at start — even presented with the
      old incarnation — and the prompt runs exactly once under a fresh claim.

No paid CLI (autouse spawn guard from the producer-1 suite).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import src.control.db as db_mod
import src.control.node_registry as nr_mod
import src.control.task_server as ts_server
import src.worker.agent as agent_mod
from src.control import turn_admission as ta
from src.control.db import MeshDB, respawn_task_id
from src.core.interfaces import Session, SessionStatus
from tests.test_turn_queue_4b import _pass, _wire
from tests.test_turn_queue_carrier_integration import (
    NODE, TOKEN, _ClientHTTP, _FakeBackend, _row, _worker,
)
from tests.test_turn_queue_producer1 import NOW, _no_cli_spawn  # noqa: F401  (autouse)

DEAD: str = "dead-mgr"


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


class _LossyHTTP(_ClientHTTP):
    """Server-side effects COMMIT, then the response is lost (``lose_start``
    times) — or the carrier process dies right before sending (``crash``)."""

    def __init__(self, client: TestClient) -> None:
        super().__init__(client)
        self.lose_start: int = 0
        self.crash_before_start: bool = False

    def post(self, path: str, body: Any = None, timeout: int = 10) -> Any:
        if path.endswith("/start-managed"):
            if self.crash_before_start:
                raise _CarrierCrash("carrier died between claim and start")
            if self.lose_start > 0:
                self.lose_start -= 1
                super().post(path, body, timeout)  # the server commits ...
                raise ConnectionError("start acknowledgement lost")  # ... the ack never arrives
        return super().post(path, body, timeout)


class _CarrierCrash(BaseException):
    """Not an ``Exception``: models process death (nothing in-process handles it)."""


class _Counting(_FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.invocations: int = 0

    async def __call__(self, *a: Any, **k: Any) -> Dict[str, Any]:
        self.invocations += 1
        return await super().__call__(*a, **k)


def _env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[MeshDB, Any, str]:
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    monkeypatch.setattr(ts_server, "get_db", lambda: db)
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    monkeypatch.setattr(ts_server, "_worker_token", lambda: TOKEN)
    db.upsert_session(Session(
        session_id=DEAD, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.CLOSED, created_at=NOW, updated_at=NOW, machine_id=NODE,
    ))
    db.enroll_session(DEAD)
    from src.core.session_task_queue import SessionTaskQueue
    from src.orchestrator import TaskOrchestrator
    from src.services.session_store import SessionStore

    o = TaskOrchestrator.__new__(TaskOrchestrator)
    o.session_store = SessionStore()
    o.task_queue = SessionTaskQueue(50, lambda _t: "")
    o.active_tasks = {}
    o._compact_injected_ids = set()
    o.events = []
    o._emit_event = lambda name, task, data=None: o.events.append(name)
    o._emit_turn_telemetry = lambda name, task, data=None, **k: o.events.append(name)
    _wire(o)
    case_id: str = db.open_case("ship X", DEAD, role="manager",
                                completion_criteria='{"round_cap": 5}')
    return db, o, case_id


def _respawn(o: Any, db: MeshDB, case_id: str) -> bool:
    return asyncio.run(o._respawn_manager_managed(db, case_id, 1, DEAD, "ship X"))


def _respawn_turns(db: MeshDB) -> List[Dict[str, Any]]:
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE queue_protocol = 1 AND turn_kind = 'respawn'").fetchall()]


def _session_count(db: MeshDB) -> int:
    return int(db._conn().execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


def _admit_and_activate(o: Any, db: MeshDB, case_id: str) -> str:
    assert _respawn(o, db, case_id) is True
    turns = _respawn_turns(db)
    assert len(turns) == 1
    tid: str = str(turns[0]["id"])
    _pass(db, o)  # real revalidation (bound Manager) + activation to the carrier
    assert _row(db, tid)["status"] == "pending", _row(db, tid)
    return tid


def _handle(w: Any, tid: str) -> None:
    async def go() -> None:
        rows = await w._fetch_pending()
        await w._handle_task([r for r in rows if r["id"] == tid][0])

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# (a) lost start acknowledgement
# --------------------------------------------------------------------------- #
def test_respawn_lost_start_ack_grants_no_second_invocation(tmp_path, monkeypatch):
    db, o, case_id = _env(tmp_path, monkeypatch)
    backend = _Counting()
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    http = _LossyHTTP(TestClient(ts_server.app))
    w = _worker(tmp_path, http)
    tid = _admit_and_activate(o, db, case_id)
    sessions_before = _session_count(db)

    http.lose_start = 1  # first start commits server-side; its ack is lost
    _handle(w, tid)

    starts = [c for c in http.calls if c[0] == "POST" and c[1].endswith("/start-managed")]
    assert len(starts) == 2, "carrier did not retry the unconfirmed start"
    assert backend.invocations == 1
    assert _row(db, tid)["status"] == "completed"

    # A producer re-tick after the lost ack converges on the SAME turn/session.
    assert _respawn(o, db, case_id) is True
    assert len(_respawn_turns(db)) == 1
    assert _session_count(db) == sessions_before
    _pass(db, o)
    assert asyncio.run(w._fetch_pending_managed({"node_id": NODE})) == []
    assert backend.invocations == 1


def test_respawn_start_never_acked_releases_not_invoked_then_runs_once(tmp_path, monkeypatch):
    db, o, case_id = _env(tmp_path, monkeypatch)
    backend = _Counting()
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    http = _LossyHTTP(TestClient(ts_server.app))
    w = _worker(tmp_path, http)
    tid = _admit_and_activate(o, db, case_id)

    http.lose_start = 99  # every start ack lost: outcome unknown after bounded retries
    _handle(w, tid)
    assert backend.invocations == 0
    row = _row(db, tid)
    assert row["status"] == "pending" and not row["claim_token"], row
    old_tokens = {c[2]["claim_token"] for c in http.calls
                  if c[0] == "POST" and c[1].endswith("/start-managed")}
    assert len(old_tokens) == 1

    http.lose_start = 0
    _handle(w, tid)
    assert backend.invocations == 1
    assert _row(db, tid)["status"] == "completed"
    # The superseded grant can start nothing.
    old = old_tokens.pop()
    with pytest.raises(Exception):
        db.start_turn(tid, old, incarnation_id="inc-1")
    assert backend.invocations == 1


# --------------------------------------------------------------------------- #
# (b) carrier crash between claim and start, then restart
# --------------------------------------------------------------------------- #
def test_respawn_carrier_crash_between_claim_and_start_refuses_old_grant(tmp_path, monkeypatch):
    db, o, case_id = _env(tmp_path, monkeypatch)
    backend = _Counting()
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    http = _LossyHTTP(TestClient(ts_server.app))
    w1 = _worker(tmp_path, http, incarnation="inc-1")
    tid = _admit_and_activate(o, db, case_id)

    http.crash_before_start = True
    with pytest.raises(_CarrierCrash):
        _handle(w1, tid)
    claimed = _row(db, tid)
    assert claimed["status"] == "claimed" and claimed["claim_incarnation"] == "inc-1"
    old_token: str = str(claimed["claim_token"])
    assert backend.invocations == 0

    # Restart: same durable carrier state, NEW process incarnation registered.
    http.crash_before_start = False
    w2 = _worker(tmp_path, http, incarnation="inc-2")
    rec = w2._claim_store.get(tid)
    assert rec and rec["claim_token"] == old_token and rec["invoked"] is False
    # Re-registration under inc-2 killed the never-started inc-1 grant: the
    # prompt is back to pending (preserved) with no live token.
    row = _row(db, tid)
    assert row["status"] == "pending" and not row["claim_token"], row
    assert row["prompt"] == claimed["prompt"]

    # The old grant is refused at start under the new incarnation AND when the
    # stale incarnation is replayed from the durable claim record.
    for inc in ("inc-2", "inc-1"):
        r = http.client.post(f"/tasks/{tid}/start-managed", json={
            "node_id": NODE, "claim_token": old_token, "incarnation_id": inc,
        }, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 409, (inc, r.status_code, r.text)
    assert _row(db, tid)["status"] == "pending"

    # Boot reconcile: the dead grant's durable record is dropped, nothing replayed.
    asyncio.run(w2._reconcile_managed_claims())
    assert w2._claim_store.get(tid) is None
    assert backend.invocations == 0

    _handle(w2, tid)
    done = _row(db, tid)
    assert done["status"] == "completed" and done["claim_token"] != old_token
    assert done["claim_incarnation"] == "inc-2"
    assert backend.invocations == 1
    assert len(_respawn_turns(db)) == 1
