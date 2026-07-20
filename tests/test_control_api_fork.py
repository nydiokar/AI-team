"""Control API — session-fork write surface (no network, no paid backend).

Fork is now the ORDINARY create-session pipeline plus two orthogonal additions:
  - `continued_from` on POST /api/sessions — a session→session lineage pointer
    (NOT a Case/role; mesh-capable exactly like any create);
  - `continue_inline` on POST /api/instructions — the marked-message context,
    injected once on the forked session's first turn.

Covers:
- POST /api/sessions threads `continued_from` into create_session (absent ⇒ None).
- POST /api/instructions threads `continue_inline` as extra_metadata on both
  branches; absent/blank ⇒ None (byte-identical turn); >8KB ⇒ 422 (DoS guard, §7).
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
        self._next_task_id = "task_web_1"

    async def submit_instruction(self, description, session_id=None, cwd=None,
                                 target_files=None, source="runtime",
                                 parent_flow_run_id=None, join_case_id=None,
                                 extra_metadata=None, **_):
        self.extra_metadatas.append(extra_metadata)
        return self._next_task_id


@pytest.fixture
def orch():
    return _StubOrchestrator()


@pytest.fixture
def client(monkeypatch, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(orch))


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _make_session(client, tmp_path, **extra):
    body = {"backend": "claude", "repo_path": str(tmp_path)}
    body.update(extra)
    r = client.post("/api/sessions", headers=_auth(), json=body)
    assert r.status_code == 200, r.text
    return r.json()["session"]


# --- continued_from lineage on the ordinary create path ---------------------

def test_create_stamps_continued_from(client, orch, tmp_path):
    src = _make_session(client, tmp_path)["session_id"]
    fork = _make_session(client, tmp_path, continued_from=src)
    # The lineage is persisted on the new session and surfaced in the view.
    assert fork["continued_from"] == src
    # And it is a real, independent session (fresh id), not the source.
    assert fork["session_id"] != src


def test_create_without_continued_from_is_none(client, tmp_path):
    s = _make_session(client, tmp_path)
    assert s.get("continued_from") in (None, "")


# --- continue_inline carry-over on the instruction path ---------------------

def test_instruction_threads_continue_inline(client, orch, tmp_path):
    sid = _make_session(client, tmp_path)["session_id"]
    r = client.post("/api/instructions", headers=_auth(), json={
        "description": "continue from here",
        "session_id": sid,
        "continue_inline": "You: prior marked message",
    })
    assert r.status_code == 200
    assert orch.extra_metadatas[-1] == {"continue_inline": "You: prior marked message"}


def test_instruction_without_continue_inline_is_none(client, orch, tmp_path):
    sid = _make_session(client, tmp_path)["session_id"]
    r = client.post("/api/instructions", headers=_auth(),
                    json={"description": "normal turn", "session_id": sid})
    assert r.status_code == 200
    assert orch.extra_metadatas[-1] is None


def test_blank_continue_inline_is_none(client, orch, tmp_path):
    sid = _make_session(client, tmp_path)["session_id"]
    r = client.post("/api/instructions", headers=_auth(),
                    json={"description": "normal turn", "session_id": sid, "continue_inline": "   "})
    assert r.status_code == 200
    assert orch.extra_metadatas[-1] is None


def test_oversized_continue_inline_is_422(client, tmp_path):
    sid = _make_session(client, tmp_path)["session_id"]
    r = client.post("/api/instructions", headers=_auth(), json={
        "description": "x", "session_id": sid, "continue_inline": "y" * 8001,
    })
    assert r.status_code == 422
