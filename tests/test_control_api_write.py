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
        self.session_service = SessionService(SessionStore())
        self.submitted = []          # (description, session_id, cwd, source)
        self.cancelled = []          # task_ids
        self.compacted = []          # session_ids
        self._backends = {"claude": _FakeBackend()}
        self._next_task_id = "task_web_1"

    async def submit_instruction(self, description, session_id=None, cwd=None,
                                 target_files=None, source="runtime", **_):
        self.submitted.append((description, session_id, cwd, source))
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


# --- instructions -----------------------------------------------------------

def test_instruction_one_off(client, orch):
    r = client.post("/api/instructions", headers=_auth(), json={"description": "do a thing"})
    assert r.status_code == 200
    assert r.json()["task_id"] == "task_web_1"
    assert orch.submitted[-1] == ("do a thing", None, None, "web_oneoff")


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


# --- idempotency ------------------------------------------------------------

def test_idempotency_key_dedupes_instruction(client, orch):
    h = {**_auth(), "Idempotency-Key": "k1"}
    r1 = client.post("/api/instructions", headers=h, json={"description": "once"})
    r2 = client.post("/api/instructions", headers=h, json={"description": "once"})
    assert r1.json() == r2.json()
    assert len(orch.submitted) == 1  # second call did not re-act


# --- stop / compact ---------------------------------------------------------

def test_stop_cancels_last_task(client, orch, tmp_path):
    res = orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    s = res.session
    s.last_task_id = "task_running"
    orch.session_service.store.save(s)

    r = client.post(f"/api/sessions/{s.session_id}/stop", headers=_auth())
    assert r.status_code == 200 and r.json()["cancelled"] is True
    assert orch.cancelled == ["task_running"]


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
