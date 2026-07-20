"""Control API — session-fork write surface (no network, no paid backend).

Covers the thin adapter wiring added by feat/session-fork-case:
- POST /api/instructions threads `continue_inline` to submit_instruction as
  extra_metadata={"continue_inline": ...}; absent ⇒ None (byte-identical turn).
- `continue_inline` past the 8KB bound ⇒ 422 (DoS guard, §7).
- POST /api/sessions/{id}/fork returns {ok, new_session_id, case_id}; a
  session_not_found reason maps to 404.
"""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


TOKEN = "test-fork-token"


class _StubOrchestrator:
    def __init__(self):
        self.session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)
        self.extra_metadatas = []          # extra_metadata per submit
        self.fork_calls = []               # (source_id, kwargs)
        self.fork_result = {"ok": True, "new_session_id": "sess_new", "case_id": "case_1"}
        self._next_task_id = "task_web_1"

    async def submit_instruction(self, description, session_id=None, cwd=None,
                                 target_files=None, source="runtime",
                                 parent_flow_run_id=None, join_case_id=None,
                                 extra_metadata=None, **_):
        self.extra_metadatas.append(extra_metadata)
        return self._next_task_id

    def fork_session(self, source_session_id, **kwargs):
        self.fork_calls.append((source_session_id, kwargs))
        return self.fork_result


@pytest.fixture
def orch():
    return _StubOrchestrator()


@pytest.fixture
def client(monkeypatch, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(orch))


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _make_session(client, tmp_path):
    r = client.post("/api/sessions", headers=_auth(),
                    json={"backend": "claude", "repo_path": str(tmp_path)})
    assert r.status_code == 200
    return r.json()["session"]["session_id"]


def test_instruction_threads_continue_inline(client, orch, tmp_path):
    sid = _make_session(client, tmp_path)
    r = client.post("/api/instructions", headers=_auth(), json={
        "description": "continue from here",
        "session_id": sid,
        "continue_inline": "You: prior marked message",
    })
    assert r.status_code == 200
    assert orch.extra_metadatas[-1] == {"continue_inline": "You: prior marked message"}


def test_instruction_without_continue_inline_is_none(client, orch, tmp_path):
    sid = _make_session(client, tmp_path)
    r = client.post("/api/instructions", headers=_auth(), json={
        "description": "normal turn",
        "session_id": sid,
    })
    assert r.status_code == 200
    assert orch.extra_metadatas[-1] is None


def test_blank_continue_inline_is_none(client, orch, tmp_path):
    sid = _make_session(client, tmp_path)
    r = client.post("/api/instructions", headers=_auth(), json={
        "description": "normal turn",
        "session_id": sid,
        "continue_inline": "   ",
    })
    assert r.status_code == 200
    assert orch.extra_metadatas[-1] is None


def test_oversized_continue_inline_is_422(client, tmp_path):
    sid = _make_session(client, tmp_path)
    r = client.post("/api/instructions", headers=_auth(), json={
        "description": "x",
        "session_id": sid,
        "continue_inline": "y" * 8001,
    })
    assert r.status_code == 422


def test_fork_endpoint_returns_ids(client, orch):
    r = client.post("/api/sessions/src/fork", headers=_auth(),
                    json={"backend": "claude", "repo_path": "/repo", "title": "cont"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "new_session_id": "sess_new", "case_id": "case_1"}
    src, kwargs = orch.fork_calls[-1]
    assert src == "src"
    assert kwargs["backend"] == "claude"
    assert kwargs["repo_path"] == "/repo"
    assert kwargs["title"] == "cont"


def test_fork_unknown_source_is_404(client, orch):
    orch.fork_result = {"ok": False, "reason": "session_not_found"}
    r = client.post("/api/sessions/ghost/fork", headers=_auth(),
                    json={"backend": "claude", "repo_path": "/repo"})
    assert r.status_code == 404
    assert r.json()["detail"]["reason"] == "session_not_found"


def test_fork_requires_auth(client):
    r = client.post("/api/sessions/src/fork",
                    json={"backend": "claude", "repo_path": "/repo"})
    assert r.status_code in (401, 403)
