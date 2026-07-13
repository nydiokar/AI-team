"""U3 — Control API write surface tests (no network, no paid backend).

The write endpoints are thin adapters over the same services Telegram calls. We
use a stub orchestrator carrying the REAL SessionService (over an isolated store
via conftest) plus async spies for submit_instruction / compact_session and a sync
spy for cancel_task — so we assert the adapter wiring without invoking any backend.
"""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


TOKEN = "test-write-token"


class _FakeExecResult:
    def __init__(self, success=True, output="compacted", errors=None):
        self.success = success
        self.output = output
        self.errors = errors or []


class _FakeBackend:
    def __init__(self):
        self.closed = []

    def close(self, session):
        self.closed.append(session.session_id)


class _StubOrchestrator:
    def __init__(self):
        self.session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)
        self.submitted = []          # (description, session_id, cwd, source)
        self.parent_flow_run_ids = []  # [A32] loose lineage id per submit
        self.cancelled = []          # task_ids
        self.compacted = []          # session_ids
        self._backends = {"claude": _FakeBackend()}
        self._next_task_id = "task_web_1"
        # When set, submit_instruction raises the harness admission block instead of
        # accepting the task — mirrors orchestrator._enqueue_task with the Level-3
        # guard armed. None ⇒ normal accept.
        self.block_task_id = None

    async def submit_instruction(self, description, session_id=None, cwd=None,
                                 target_files=None, source="runtime",
                                 parent_flow_run_id=None, **_):
        if self.block_task_id is not None:
            from src.orchestrator import HarnessAdmissionBlocked
            raise HarnessAdmissionBlocked(self.block_task_id)
        self.submitted.append((description, session_id, cwd, source))
        # [A32] Record the loose lineage id separately so tests can assert it is
        # threaded from InstructionBody without disturbing the legacy tuple shape.
        self.parent_flow_run_ids.append(parent_flow_run_id)
        return self._next_task_id

    def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return True

    async def compact_session(self, session_id):
        self.compacted.append(session_id)
        return _FakeExecResult()


@pytest.fixture
def orch():
    return _StubOrchestrator()


@pytest.fixture
def client(monkeypatch, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(orch))


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- auth on every write ----------------------------------------------------

def test_writes_require_auth(client):
    assert client.post("/api/instructions", json={"description": "hi"}).status_code in (401, 403)
    assert client.post("/api/sessions", json={"backend": "claude", "repo_path": "/x"}).status_code in (401, 403)


# --- create session ---------------------------------------------------------

def test_create_session_tags_web_origin(client, orch, tmp_path):
    r = client.post("/api/sessions", headers=_auth(),
                    json={"backend": "claude", "repo_path": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["session"]["origin_channel"] == "web"
    assert body["session"]["origin_kind"] == "user"
    # And it is visible through the read endpoint.
    sid = body["session"]["session_id"]
    sessions = client.get("/api/sessions", headers=_auth()).json()["sessions"]
    assert any(s["session_id"] == sid for s in sessions)


def test_create_session_unknown_backend_is_400(client, tmp_path):
    r = client.post("/api/sessions", headers=_auth(),
                    json={"backend": "nope", "repo_path": str(tmp_path)})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "unknown_backend"


def test_create_session_invalid_repo_path_is_400_with_detail(monkeypatch):
    """Feature #38: a bad LOCAL repo_path is rejected at create time with a 400,
    the stable reason, and the human detail surfaced through the envelope — so the
    web client can show *why* instead of opening a doomed session."""
    from src.services.session_service import CommandResult

    def reject(_p):
        return CommandResult(False, reason="invalid_repo_path",
                             detail="Path does not exist.")

    orch = _StubOrchestrator()
    orch.session_service = SessionService(SessionStore(), repo_path_validator=reject)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    client = TestClient(control_api.build_control_api(orch))

    r = client.post("/api/sessions", headers=_auth(),
                    json={"backend": "claude", "repo_path": "/no/such/dir"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["reason"] == "invalid_repo_path"
    assert detail["detail"] == "Path does not exist."
    # Nothing was created.
    assert client.get("/api/sessions", headers=_auth()).json()["sessions"] == []


# --- instructions -----------------------------------------------------------

def test_instruction_one_off(client, orch):
    r = client.post("/api/instructions", headers=_auth(), json={"description": "do a thing"})
    assert r.status_code == 200
    assert r.json()["task_id"] == "task_web_1"
    assert orch.submitted[-1] == ("do a thing", None, None, "web_oneoff")
    # [A32] No lineage supplied ⇒ None threaded (byte-identical to pre-A32).
    assert orch.parent_flow_run_ids[-1] is None


def test_instruction_threads_parent_flow_run_id(client, orch):
    """[A32] A Manager→worker dispatch passes parent_flow_run_id; it must reach
    submit_instruction (which stamps it onto the child flow_runs row when
    HARNESS_FLOW_DRIVE is ON). Endpoint just threads it — no gating here."""
    r = client.post("/api/instructions", headers=_auth(),
                    json={"description": "child work", "parent_flow_run_id": "flow_parent_1"})
    assert r.status_code == 200
    assert orch.parent_flow_run_ids[-1] == "flow_parent_1"


def test_instruction_to_session_flips_busy(client, orch, tmp_path):
    from src.core.interfaces import SessionStatus
    res = orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    sid = res.session.session_id

    r = client.post("/api/instructions", headers=_auth(),
                    json={"description": "fix bug", "session_id": sid})
    assert r.status_code == 200
    assert r.json()["task_id"] == "task_web_1"
    # submit_instruction called with source=web_session and the session's repo as cwd.
    desc, called_sid, cwd, source = orch.submitted[-1]
    assert (desc, called_sid, source) == ("fix bug", sid, "web_session")
    assert cwd == str(tmp_path)
    # Session went BUSY and recorded the task id.
    s = orch.session_service.store.get(sid)
    assert s.status == SessionStatus.BUSY
    assert s.last_task_id == "task_web_1"


def test_instruction_unknown_session_404(client):
    r = client.post("/api/instructions", headers=_auth(),
                    json={"description": "x", "session_id": "nope"})
    assert r.status_code == 404


# --- harness Level-3 admission block (A13) ----------------------------------

def test_blocked_oneoff_instruction_is_409_with_reason(client, orch):
    """A one-off submit refused by the Level-3 admission gate returns a clean 409
    with the stable machine reason + human detail — not an opaque 500."""
    orch.block_task_id = "task_blocked_1"
    r = client.post("/api/instructions", headers=_auth(),
                    json={"description": "migrate the auth schema"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "harness_level3_needs_approval"
    assert detail["task_id"] == "task_blocked_1"
    assert detail["detail"]  # non-empty human copy
    # Nothing was accepted.
    assert orch.submitted == []


def test_blocked_session_instruction_reverts_busy_to_idle(client, orch, tmp_path):
    """When a session submit is blocked AFTER the optimistic mark_busy, the session
    must be returned to IDLE — never stranded BUSY with no in-flight task."""
    from src.core.interfaces import SessionStatus
    res = orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    sid = res.session.session_id
    orch.block_task_id = "task_blocked_2"

    r = client.post("/api/instructions", headers=_auth(),
                    json={"description": "rewrite the mesh router", "session_id": sid})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "harness_level3_needs_approval"
    # The session is usable again, not stuck BUSY.
    s = orch.session_service.store.get(sid)
    assert s.status == SessionStatus.IDLE


# --- idempotency ------------------------------------------------------------

def test_idempotency_key_dedupes_instruction(client, orch):
    h = {**_auth(), "Idempotency-Key": "k1"}
    r1 = client.post("/api/instructions", headers=h, json={"description": "once"})
    r2 = client.post("/api/instructions", headers=h, json={"description": "once"})
    assert r1.json() == r2.json()
    assert len(orch.submitted) == 1  # second call did not re-act


def test_idempotency_key_is_concurrency_safe(monkeypatch, orch):
    """CONC-1: two genuinely concurrent requests sharing an Idempotency-Key must
    collapse to a single side effect. We drive the ASGI app directly with
    asyncio.gather so both requests are in-flight on one event loop at once (the
    TestClient serializes requests through a single portal and cannot reproduce
    this). Without the per-key guard the second request misses the cache mid-flight
    and submits a duplicate; with it, the second blocks on the asyncio.Lock and then
    serves the first's cached response.

    Regression guard: an event-loop deadlock would also surface here (the test
    would time out) if the async path ever held a blocking threading.Lock across
    its ``await``."""
    import asyncio
    import httpx

    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    app = control_api.build_control_api(orch)

    # Force interleave: the first submit yields control mid-flight so the second
    # request runs before the first stores its idempotent response.
    barrier = asyncio.Event()
    orig = orch.submit_instruction
    first = {"seen": False}

    async def slow_submit(*a, **k):
        if not first["seen"]:
            first["seen"] = True
            # Let the gather's second coroutine reach the guard before we finish.
            await asyncio.sleep(0.05)
        return await orig(*a, **k)

    orch.submit_instruction = slow_submit

    async def _run():
        h = {**_auth(), "Idempotency-Key": "race"}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            r1, r2 = await asyncio.gather(
                ac.post("/api/instructions", headers=h, json={"description": "once"}),
                ac.post("/api/instructions", headers=h, json={"description": "once"}),
            )
            return r1.json(), r2.json()

    j1, j2 = asyncio.run(asyncio.wait_for(_run(), timeout=5))

    # Exactly one real submission despite two concurrent identical requests.
    assert len(orch.submitted) == 1
    assert j1 == j2


# --- stop / compact ---------------------------------------------------------

def test_stop_cancels_last_task(client, orch, tmp_path):
    res = orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    s = res.session
    s.last_task_id = "task_running"
    orch.session_service.store.save(s)

    r = client.post(f"/api/sessions/{s.session_id}/stop", headers=_auth())
    assert r.status_code == 200 and r.json()["cancelled"] is True
    assert orch.cancelled == ["task_running"]
    # Stop records the run outcome, but CANCELLED is not a closed lifecycle.
    from src.core.interfaces import SessionStatus
    stopped = orch.session_service.store.get(s.session_id)
    assert stopped.status == SessionStatus.CANCELLED
    assert stopped.status != SessionStatus.CLOSED


def test_compact_returns_result_shape(client, orch, tmp_path):
    res = orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    sid = res.session.session_id
    r = client.post(f"/api/sessions/{sid}/compact", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["output"] == "compacted"
    assert orch.compacted == [sid]


def test_stop_unknown_session_404(client):
    assert client.post("/api/sessions/nope/stop", headers=_auth()).status_code == 404


# --- parity: close / restore / model (U3.5/P5) ------------------------------

def test_close_then_restore(client, orch, tmp_path):
    res = orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    sid = res.session.session_id

    rc = client.post(f"/api/sessions/{sid}/close", headers=_auth())
    assert rc.status_code == 200 and rc.json()["session"]["status"] == "closed"

    rr = client.post(f"/api/sessions/{sid}/restore", headers=_auth())
    assert rr.status_code == 200 and rr.json()["session"]["status"] == "idle"


def test_restore_non_closed_is_409(client, orch, tmp_path):
    res = orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    r = client.post(f"/api/sessions/{res.session.session_id}/restore", headers=_auth())
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "not_closed"


def test_set_model_valid_and_unknown(client, orch, tmp_path):
    res = orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    sid = res.session.session_id

    ok = client.post(f"/api/sessions/{sid}/model", headers=_auth(), json={"model": "opus"})
    assert ok.status_code == 200 and ok.json()["session"]["model"] == "opus"

    bad = client.post(f"/api/sessions/{sid}/model", headers=_auth(), json={"model": "nope-9000"})
    assert bad.status_code == 400 and bad.json()["detail"]["reason"] == "unknown_model"


def test_close_unknown_session_404(client):
    assert client.post("/api/sessions/nope/close", headers=_auth()).status_code == 404


# --- parity tier 2: inspect / jobs (U3.5/P6,P7) -----------------------------

def test_inspect_routes_through_inspector(client, orch, monkeypatch, tmp_path):
    res = orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    sid = res.session.session_id

    class _FakeInspector:
        async def run(self, session, op, params):
            assert op == "list_dirs"
            return {"path": session.repo_path, "dirs": ["a", "b"]}

    import src.control.node_inspector as ni
    monkeypatch.setattr(ni, "get_inspector", lambda: _FakeInspector())

    r = client.post(f"/api/sessions/{sid}/inspect", headers=_auth(),
                    json={"op": "list_dirs", "limit": 12})
    assert r.status_code == 200 and r.json()["dirs"] == ["a", "b"]


def test_inspect_unknown_session_404(client):
    assert client.post("/api/sessions/nope/inspect", headers=_auth(),
                       json={"op": "list_dirs"}).status_code == 404


def test_jobs_returns_running_and_recent(client, monkeypatch):
    import src.control.control_api as capi

    class _FakeDB:
        def list_jobs(self, status=None, session_id=None, ownership=None, limit=20):
            return [{"id": "j1", "status": status or "done"}]

    monkeypatch.setattr(capi, "_db", lambda: _FakeDB())
    r = client.get("/api/jobs", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert "running" in body and "recent" in body


def test_jobs_prefers_orchestrator_merged_view(monkeypatch, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)

    def _list_watched_jobs(limit=20, session_id=None, ownership=None):
        return {
            "running": [{"id": "remote-running", "status": "running"}],
            "recent": [{"id": "remote-done", "status": "done"}],
        }

    orch.list_watched_jobs = _list_watched_jobs
    client = TestClient(control_api.build_control_api(orch))

    r = client.get("/api/jobs", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {
        "running": [{"id": "remote-running", "status": "running"}],
        "recent": [{"id": "remote-done", "status": "done"}],
    }


def test_jobs_supports_unowned_filter(client, monkeypatch):
    import src.control.control_api as capi

    calls = []

    class _FakeDB:
        def list_jobs(self, status=None, session_id=None, ownership=None, limit=20):
            calls.append((status, session_id, ownership, limit))
            if ownership == "unowned":
                return [{"id": "unowned", "status": status or "done", "session_id": None}]
            return [{"id": "owned", "status": status or "done", "session_id": "sess_1"}]

    monkeypatch.setattr(capi, "_db", lambda: _FakeDB())
    r = client.get("/api/jobs?ownership=unowned", headers=_auth())

    assert r.status_code == 200
    assert r.json()["running"][0]["id"] == "unowned"
    assert calls[0][2] == "unowned"


def test_jobs_rejects_conflicting_session_and_unowned(client):
    r = client.get("/api/jobs?session_id=sess_1&ownership=unowned", headers=_auth())
    assert r.status_code == 400


def test_jobs_requires_auth(client):
    assert client.get("/api/jobs").status_code in (401, 403)


# --- [M3.3] POST /api/cases — open a new Case on an existing Manager session ---

def _wire_open_case(orch, *, enabled=True, returns="case-new"):
    """Give the stub orchestrator the two hooks the /api/cases route calls."""
    orch._manager_role_enabled = lambda: enabled
    orch.open_case_calls = []

    def _open_case(objective, session_id, role="manager", completion_criteria=None):
        orch.open_case_calls.append((objective, session_id, role, completion_criteria))
        return returns

    orch.open_case = _open_case


def test_open_case_opens_on_existing_session(client, orch):
    _wire_open_case(orch, returns="case-new")
    sess = orch.session_service.store.create(backend="claude", repo_path="/tmp/repo")
    r = client.post(
        "/api/cases",
        json={"objective": "next objective", "session_id": sess.session_id,
              "completion_criteria": "tests green"},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "case_id": "case-new"}
    assert orch.open_case_calls == [("next objective", sess.session_id, "manager", "tests green")]


def test_open_case_disabled_returns_409(client, orch):
    _wire_open_case(orch, enabled=False)
    r = client.post("/api/cases", json={"objective": "x", "session_id": "s"}, headers=_auth())
    assert r.status_code == 409


def test_open_case_unknown_session_returns_404(client, orch):
    _wire_open_case(orch)
    r = client.post("/api/cases", json={"objective": "x", "session_id": "ghost"}, headers=_auth())
    assert r.status_code == 404


def test_open_case_requires_auth(client):
    assert client.post("/api/cases", json={"objective": "x", "session_id": "s"}).status_code in (401, 403)
