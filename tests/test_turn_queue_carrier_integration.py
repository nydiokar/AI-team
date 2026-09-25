"""A82 Stage 3 rework — fake-carrier END-TO-END integration (packet §7 gate).

Real pieces: a file-backed SQLite `MeshDB`, the REAL task-server FastAPI app
(in-process via TestClient), and the REAL `WorkerAgent` bookkeeping
(`_fetch_pending` → `_handle_task` → claim/reserve/start → result spool →
`/result-managed` → receipt-matched prune; drain; boot replay through `run()`).
Only the backend execution (`_execute_task`) is faked, so no paid CLI is ever
invoked. Also covers the m1/m2 route hardening and flag-OFF legacy isolation.
"""
import asyncio
import json
import urllib.error
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import src.control.task_server as ts
import src.worker.agent as agent_mod
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from src.worker.agent import WorkerAgent
from src.worker.managed_result_spool import MAX_ENVELOPE_BYTES, ManagedResultSpool

NODE = "Horse"
TOKEN = "tok"
NOW = datetime(2026, 9, 25, 12, 0, 0).isoformat()


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path, monkeypatch) -> MeshDB:
    mdb = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(ts, "get_db", lambda: mdb)
    monkeypatch.setattr(ts, "_worker_token", lambda: TOKEN)
    return mdb


class _ClientHTTP:
    """The worker's `_HTTP` contract (post/get returning parsed JSON, raising
    `urllib.error.HTTPError` on >=400) backed by the in-process task server.
    `faults` maps a path substring to a list of injected failures consumed in
    order: an int status (HTTPError), "timeout" (TimeoutError), or a dict
    (returned as a 2xx body WITHOUT calling the server)."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.calls: List[tuple] = []
        self.faults: Dict[str, List[Any]] = {}

    def _fault(self, path: str) -> Any:
        for frag, queue in self.faults.items():
            if frag in path and queue:
                return queue.pop(0)
        return None

    def _raise_or_return(self, path: str, resp) -> Any:
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(path, resp.status_code, resp.text, None, None)
        return resp.json()

    def post(self, path: str, body: Any = None, timeout: int = 10) -> Any:
        self.calls.append(("POST", path, body))
        fault = self._fault(path)
        if isinstance(fault, int):
            raise urllib.error.HTTPError(path, fault, "injected", None, None)
        if fault == "timeout":
            raise TimeoutError("injected timeout")
        if isinstance(fault, dict):
            return fault
        resp = self.client.post(path, json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        return self._raise_or_return(path, resp)

    def get(self, path: str, params: Optional[Dict[str, str]] = None, timeout: int = 10) -> Any:
        self.calls.append(("GET", path, params))
        resp = self.client.get(path, params=params or {}, headers={"Authorization": f"Bearer {TOKEN}"})
        return self._raise_or_return(path, resp)


def _worker(tmp_path, http, *, managed: bool = True, incarnation: str = "inc-1",
            max_retained: Optional[int] = None) -> WorkerAgent:
    w = WorkerAgent.__new__(WorkerAgent)
    w.cfg = SimpleNamespace(
        node_id=NODE, backends=["claude"], max_concurrent=2, accept_unpinned=True,
        managed_turns=managed, tailscale_ip="127.0.0.1", api_port=0,
        projects_root="", controller_url="http://test", list_repos=lambda: [],
    )
    w._http = http
    w._incarnation_id = incarnation
    w._active, w._active_meta = {}, {}
    w._slots_used = 0
    w._inflight_sessions = set()
    w._semaphore = asyncio.Semaphore(2)
    w._codex_control_semaphore = asyncio.Semaphore(1)
    w._heartbeat_now, w._poll_now, w._shutdown = asyncio.Event(), asyncio.Event(), asyncio.Event()
    w._backends, w._telemetry_sink, w._canary = {}, None, True
    w._model_capabilities = {}
    kw = {} if max_retained is None else {"max_retained_bytes": max_retained}
    w._result_spool = ManagedResultSpool(str(tmp_path / "carrier_state"), **kw)
    w._pending_result_delivery = set()
    w._managed_claims = {}
    w._result_delivery_semaphore = asyncio.Semaphore(2)
    w._delivering = set()
    w._managed_claims_blocked = None
    return w


def _seed_turn(db: MeshDB, task_id: str = "t-1", session_id: str = "sess-1",
               prompt: str = "frozen prompt") -> None:
    db.upsert_session(Session(
        session_id=session_id, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id=NODE,
    ))
    db.enroll_session(session_id)
    db.enqueue_turn(
        task_id=task_id, session_id=session_id, backend="claude",
        action="resume_session", payload={"task_id": task_id, "prompt": prompt},
        turn_source="human", turn_kind="instruction", machine_id=NODE,
    )
    db.activate_turn(task_id)


class _FakeBackend:
    """Stands in for `_execute_task`: records the row it was asked to run and
    returns a remote-carrier result carrying a NATIVE backend session id."""

    def __init__(self, native_id: str = "native-remote-abc") -> None:
        self.rows: List[Dict[str, Any]] = []
        self.native_id = native_id

    async def __call__(self, task_row, backends, http=None, telemetry_sink=None, node_id=""):
        self.rows.append(dict(task_row))
        return {
            "success": True, "output": f"answer to {task_row['payload']['prompt']}",
            "errors": [], "files_modified": [], "execution_time": 0.01,
            "timestamp": NOW, "return_code": 0,
            "backend_session_id": self.native_id,
        }


def _spooled(w: WorkerAgent) -> List[str]:
    return [tid for tid, _tok, _env in w._result_spool.list_spooled()]


def _row(db: MeshDB, task_id: str) -> Dict[str, Any]:
    return dict(db._conn().execute("SELECT * FROM mesh_tasks WHERE id = ?", (task_id,)).fetchone())


def _sess(db: MeshDB, sid: str) -> Dict[str, Any]:
    return dict(db._conn().execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone())


# --------------------------------------------------------------------------- #
# INT01 — full managed path + remote native-id commit
# --------------------------------------------------------------------------- #
def test_INT01_managed_turn_end_to_end_remote_native_id(db, tmp_path, monkeypatch):
    backend = _FakeBackend("native-remote-abc")
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    _seed_turn(db, "t-1", "sess-1", prompt="frozen prompt")

    async def scenario():
        rows = await w._fetch_pending()
        managed = [r for r in rows if r["id"] == "t-1"]
        assert managed and managed[0]["queue_protocol"] == 1
        assert "claim_token" not in managed[0], "poll view leaked the execution credential"
        # Poll snapshot is NOT authoritative: tamper it; the carrier must run the
        # frozen payload from the claim RESPONSE.
        managed[0]["payload"] = {"prompt": "POLL SNAPSHOT"}
        await w._handle_task(managed[0])

    asyncio.run(scenario())

    # Polled managed, claimed via the managed route with a token, started fenced.
    posted = [c[1] for c in http.calls if c[0] == "POST"]
    assert posted[:3] == ["/tasks/t-1/claim-managed", "/tasks/t-1/start-managed", "/tasks/t-1/result-managed"]
    assert "/tasks/t-1/claim" not in posted and "/tasks/t-1/result" not in posted
    assert any(c[1] == "/tasks/pending-managed" for c in http.calls)
    # Executed the claim response (frozen payload + token), not the poll snapshot.
    assert len(backend.rows) == 1
    ran = backend.rows[0]
    assert ran["payload"]["prompt"] == "frozen prompt"
    assert ran["queue_protocol"] == 1 and ran["claim_token"]
    # Atomic complete_turn: terminal + result + native id + active identity.
    row = _row(db, "t-1")
    assert row["status"] == "completed"
    assert row["claim_token"] == ran["claim_token"]
    assert row["claim_carrier_kind"] == "worker_daemon" and row["claim_incarnation"] == "inc-1"
    assert row["started_at"]
    assert json.loads(row["result"])["output"] == "answer to frozen prompt"
    sess = _sess(db, "sess-1")
    assert sess["backend_session_id"] == "native-remote-abc"
    assert sess["last_task_id"] == "t-1"
    # Durable receipt matched → spool pruned → ownership released everywhere.
    assert _spooled(w) == []
    assert w._pending_result_delivery == set()
    assert w._managed_claims == {}
    assert w._result_spool.reserved_bytes() == 0
    assert db.get_active_turn("sess-1") is None
    # Token never published in the heartbeat-visible task details.
    assert "claim_token" not in json.dumps(w._active_meta)


# --------------------------------------------------------------------------- #
# INT02 — failed delivery keeps spool + ownership; boot replay re-delivers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fault", [503, "timeout", {"status": "ok"}], ids=["5xx", "timeout", "2xx-unmatched"])
def test_INT02_failed_delivery_held_until_boot_replay_receipt(db, tmp_path, monkeypatch, fault):
    backend = _FakeBackend("native-remote-xyz")
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    client = TestClient(ts.app)
    http = _ClientHTTP(client)
    http.faults["/result-managed"] = [fault]
    w = _worker(tmp_path, http, incarnation="inc-old")
    _seed_turn(db, "t-2", "sess-2")

    async def first_life():
        rows = await w._fetch_pending()
        await w._handle_task([r for r in rows if r["id"] == "t-2"][0])

    asyncio.run(first_life())

    # Delivery failed: spool kept, ownership held (row still running = slot held).
    assert _spooled(w) == ["t-2"]
    assert "t-2" in w._pending_result_delivery
    assert _row(db, "t-2")["status"] == "running"
    assert db.get_active_turn("sess-2") is not None
    assert _sess(db, "sess-2")["backend_session_id"] in (None, "")

    # Carrier restart: NEW process incarnation, same carrier state dir. __init__
    # marks the spool (`_replay_result_spool`); `run()` re-delivers at boot.
    w2 = _worker(tmp_path, _ClientHTTP(client), incarnation="inc-new")
    assert w2._replay_result_spool() == 1
    posted_before = len(w2._http.calls)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(w2, "_reap_stale_backend_children", lambda: None, raising=False)
    monkeypatch.setattr(w2, "_install_signal_handler", lambda: None, raising=False)
    monkeypatch.setattr(w2, "_register_until_success", _noop, raising=False)
    monkeypatch.setattr(w2, "_heartbeat_loop", _noop, raising=False)
    monkeypatch.setattr(w2, "_quota_observe_loop", _noop, raising=False)
    monkeypatch.setattr(w2, "_deregister", lambda: None, raising=False)
    monkeypatch.setattr(agent_mod, "_run_nudge_listener", _noop)

    async def second_life():
        asyncio.get_running_loop().call_later(0.2, w2._shutdown.set)
        await w2.run()

    asyncio.run(second_life())

    replay_posts = [c[1] for c in w2._http.calls[posted_before:] if c[0] == "POST"]
    assert "/tasks/t-2/result-managed" in replay_posts
    # The old token still owns the held task → accepted despite the new
    # incarnation (design §7: reject only if superseded).
    row = _row(db, "t-2")
    assert row["status"] == "completed"
    assert _sess(db, "sess-2")["backend_session_id"] == "native-remote-xyz"
    assert _spooled(w2) == []
    assert w2._pending_result_delivery == set()
    assert db.get_active_turn("sess-2") is None
    assert len(backend.rows) == 1, "replay must not re-execute the backend"


# --------------------------------------------------------------------------- #
# INT03 — per-carrier envelope budget reserved BEFORE start (M2)
# --------------------------------------------------------------------------- #
def test_INT03_no_envelope_allowance_leaves_turn_pending_not_run(db, tmp_path, monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http, max_retained=MAX_ENVELOPE_BYTES)
    # Another in-flight attempt already holds the whole per-carrier budget.
    assert w._result_spool.reserve("other", "tok0", MAX_ENVELOPE_BYTES) is not None
    _seed_turn(db, "t-3", "sess-3")

    async def scenario():
        rows = await w._fetch_pending()
        await w._handle_task([r for r in rows if r["id"] == "t-3"][0])

    asyncio.run(scenario())

    assert backend.rows == [], "turn ran without a result-envelope reservation"
    posted = [c[1] for c in http.calls if c[0] == "POST"]
    assert "/tasks/t-3/start-managed" not in posted
    assert "/tasks/t-3/release-managed" in posted
    row = _row(db, "t-3")
    assert row["status"] == "pending" and row["claim_token"] is None and row["started_at"] is None
    assert w._result_spool.reserved_bytes() == MAX_ENVELOPE_BYTES  # only the other one


# --------------------------------------------------------------------------- #
# INT04 — drain never releases a running managed backend (M1)
# --------------------------------------------------------------------------- #
class _RecordingHTTP:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def post(self, path, body=None, timeout=10):
        self.calls.append(("POST", path, body))
        return {"status": "ok"}

    def get(self, path, params=None, timeout=10):
        self.calls.append(("GET", path, params))
        return []


def test_INT04_drain_releases_only_unstarted_managed_and_legacy(tmp_path, monkeypatch):
    http = _RecordingHTTP()
    w = _worker(tmp_path, http)

    async def _noop(*a, **k):
        return None

    for name in ("_register_until_success", "_heartbeat_loop", "_quota_observe_loop"):
        monkeypatch.setattr(w, name, _noop, raising=False)
    monkeypatch.setattr(w, "_reap_stale_backend_children", lambda: None, raising=False)
    monkeypatch.setattr(w, "_install_signal_handler", lambda: None, raising=False)
    monkeypatch.setattr(w, "_deregister", lambda: None, raising=False)
    monkeypatch.setattr(agent_mod, "_run_nudge_listener", _noop)

    async def scenario():
        for tid in ("t-run", "t-claimed", "t-legacy"):
            w._active[tid] = asyncio.create_task(asyncio.sleep(0.05))
        w._managed_claims["t-run"] = {"claim_token": "tokrun", "status": "running"}
        w._managed_claims["t-claimed"] = {"claim_token": "tokclaimed", "status": "claimed"}
        asyncio.get_running_loop().call_later(0.05, w._shutdown.set)
        await w.run()

    asyncio.run(scenario())
    posted = [c[1] for c in http.calls if c[0] == "POST"]
    assert "/tasks/t-run/release" not in posted and "/tasks/t-run/release-managed" not in posted
    assert "/tasks/t-claimed/release-managed" in posted
    assert "/tasks/t-claimed/release" not in posted
    assert "/tasks/t-legacy/release" in posted
    assert "t-run" in w._managed_claims  # ownership retained


# --------------------------------------------------------------------------- #
# INT05 — flag OFF: legacy protocol-0 poll/claim only, byte-identical
# --------------------------------------------------------------------------- #
def test_INT05_flag_off_legacy_poll_and_claim_only(db, tmp_path, monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http, managed=False)
    _seed_turn(db, "t-m", "sess-m")  # a managed row exists but must stay invisible
    db.upsert_session(Session(  # un-enrolled legacy session
        session_id="sess-l", backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id=NODE,
    ))
    db.enqueue_task("t-legacy", "sess-l", NODE, "claude", "run_oneoff", {"prompt": "legacy"})

    async def scenario():
        rows = await w._fetch_pending()
        assert [r["id"] for r in rows] == ["t-legacy"]
        await w._handle_task(rows[0])

    asyncio.run(scenario())
    paths = [c[1] for c in http.calls]
    assert paths == ["/tasks/pending", "/tasks/t-legacy/claim", "/tasks/t-legacy/result"]
    assert http.calls[1][2] == {"node_id": NODE}
    assert backend.rows[0]["payload"]["prompt"] == "legacy"
    assert _row(db, "t-m")["status"] == "pending"

    # Registration advertises no managed protocol when OFF, protocol 1 when ON.
    reg = _RecordingHTTP()
    w._http = reg
    w._register()
    assert "queue_protocols" not in reg.calls[-1][2]["capabilities"]
    w.cfg.managed_turns = True
    w._register()
    assert reg.calls[-1][2]["capabilities"]["queue_protocols"] == [0, 1]


# --------------------------------------------------------------------------- #
# INT06 — m2 stale receipt never echoes an unverified token; m1 evidence
# --------------------------------------------------------------------------- #
def _claim_start(client: TestClient, task_id: str, node: str = NODE) -> str:
    h = {"Authorization": f"Bearer {TOKEN}"}
    r = client.post(f"/tasks/{task_id}/claim-managed", json={"node_id": node, "incarnation_id": "i"}, headers=h)
    assert r.status_code == 200, r.text
    tok = r.json()["claim_token"]
    r = client.post(f"/tasks/{task_id}/start-managed", json={"node_id": node, "claim_token": tok, "incarnation_id": "i"}, headers=h)
    assert r.status_code == 200, r.text
    return tok


def test_INT06_stale_receipt_requires_recorded_token(db):
    client = TestClient(ts.app)
    h = {"Authorization": f"Bearer {TOKEN}"}
    _seed_turn(db, "t-6", "sess-6")
    tok = _claim_start(client, "t-6")
    body = {"node_id": NODE, "claim_token": tok, "success": True, "output": "ok"}
    assert client.post("/tasks/t-6/result-managed", json=body, headers=h).json()["status"] == "accepted"
    # Genuine late replay for the SAME token → stale receipt (task+token).
    r = client.post("/tasks/t-6/result-managed", json=body, headers=h)
    assert r.status_code == 200 and r.json() == {"status": "accepted (stale)", "task_id": "t-6", "claim_token": tok}
    # Forged/superseded token on a terminal row → refused, token NOT echoed.
    forged = {**body, "claim_token": "forged-token"}
    r = client.post("/tasks/t-6/result-managed", json=forged, headers=h)
    assert r.status_code == 409
    assert "forged-token" not in r.text


def test_INT07_quiescence_requires_real_bound_evidence(db):
    client = TestClient(ts.app)
    h = {"Authorization": f"Bearer {TOKEN}"}
    _seed_turn(db, "t-7", "sess-7")
    tok = _claim_start(client, "t-7")
    assert db.enter_recovery("t-7", tok, reason="carrier lost")
    url = "/tasks/t-7/quiescence"
    base = {"node_id": NODE, "claim_token": tok}
    # Any non-null result is NOT evidence (m1).
    assert client.post(url, json={**base, "result": {}}, headers=h).status_code == 422
    assert client.post(url, json={**base, "result": {"anything": 1}}, headers=h).status_code == 422
    # Bare boolean / missing native identity / success-without-result refused.
    assert client.post(url, json={**base, "quiescent": True}, headers=h).status_code == 409
    assert client.post(url, json={**base, "quiescent": True, "terminal": True,
                                  "terminal_status": "cancelled"}, headers=h).status_code == 409
    assert client.post(url, json={**base, "quiescent": True, "terminal": True, "native_session_id": "n",
                                  "terminal_status": "completed"}, headers=h).status_code == 409
    # Wrong carrier / wrong token refused.
    assert client.post(url, json={**base, "node_id": "Other", "result": {"success": True}},
                       headers=h).status_code == 409
    assert client.post(url, json={**base, "claim_token": "nope", "result": {"success": True}},
                       headers=h).status_code == 409
    assert _row(db, "t-7")["status"] == "recovery_required"
    # A real durable terminal result reconciles atomically (native id committed).
    r = client.post(url, json={**base, "result": {"success": True, "output": "spooled",
                                                  "backend_session_id": "native-q"}}, headers=h)
    assert r.status_code == 200 and r.json()["resolved_status"] == "completed"
    assert _row(db, "t-7")["status"] == "completed"
    assert _sess(db, "sess-7")["backend_session_id"] == "native-q"


def test_INT08_quiescence_observation_without_result_resolves_failed_or_cancelled(db):
    client = TestClient(ts.app)
    h = {"Authorization": f"Bearer {TOKEN}"}
    _seed_turn(db, "t-8", "sess-8")
    tok = _claim_start(client, "t-8")
    assert db.enter_recovery("t-8", tok, reason="deadline")
    r = client.post("/tasks/t-8/quiescence", json={
        "node_id": NODE, "claim_token": tok, "quiescent": True, "terminal": True,
        "terminal_status": "cancelled", "native_session_id": "native-8",
    }, headers=h)
    assert r.status_code == 200 and r.json()["resolved_status"] == "cancelled"
    assert db.get_active_turn("sess-8") is None


def test_INT09_restarted_incarnation_cannot_start_old_claim(db):
    client = TestClient(ts.app)
    h = {"Authorization": f"Bearer {TOKEN}"}
    _seed_turn(db, "t-9", "sess-9")
    r = client.post("/tasks/t-9/claim-managed", json={"node_id": NODE, "incarnation_id": "old"}, headers=h)
    tok = r.json()["claim_token"]
    assert r.json()["task"]["backend"] == "claude" and r.json()["task"]["action"] == "resume_session"
    r = client.post("/tasks/t-9/start-managed", json={"node_id": NODE, "claim_token": tok, "incarnation_id": "new"}, headers=h)
    assert r.status_code == 409
    ok = client.post("/tasks/t-9/start-managed", json={"node_id": NODE, "claim_token": tok, "incarnation_id": "old"}, headers=h)
    again = client.post("/tasks/t-9/start-managed", json={"node_id": NODE, "claim_token": tok, "incarnation_id": "old"}, headers=h)
    assert ok.status_code == again.status_code == 200 and ok.json() == again.json()
    # Release after start is forbidden.
    r = client.post("/tasks/t-9/release-managed", json={"node_id": NODE, "claim_token": tok}, headers=h)
    assert r.status_code == 409
    assert _row(db, "t-9")["status"] == "running"
