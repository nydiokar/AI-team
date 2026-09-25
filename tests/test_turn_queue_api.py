"""A82 Stage 1 — API compatibility + commit-before-202 RED acceptance tests.

Scope (per packet §5): the COMPATIBILITY subset only.

  * COMPAT: the legacy `POST /api/instructions` success envelope
    (`{ok, task_id, session}`) and 200 status are UNCHANGED for legacy callers,
    and the telemetry `/api/turns` + `/api/turns/{id}` routes are unchanged.
    These are GREEN regression guards that pin the contract the new work must
    not break (design §9). They use the same stub-orchestrator TestClient
    pattern as tests/test_control_api_write.py.

  * TARGET (RED): the brand-new `POST /api/sessions/{id}/turn-requests` route
    returns 202 ONLY AFTER durable admission (commit-before-202, design §9,
    §4). That route does not exist yet → 404/red. And the compatibility route
    must NOT acknowledge on a failed durable commit (503, not 200) — a contract
    the current handler does not honor (it can return the optimistic envelope).

No network/paid backend: stub orchestrator + FastAPI TestClient.
"""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


TOKEN = "test-turnqueue-token"


class _StubOrchestrator:
    def __init__(self):
        self.session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)
        self.submitted = []
        self._backends = {"claude": object()}
        self._next_task_id = "task_web_1"
        self.commit_fails = False  # when True, admission raises a DB failure

    async def submit_instruction(self, description, session_id=None, cwd=None,
                                 target_files=None, source="runtime",
                                 parent_flow_run_id=None, **_):
        if self.commit_fails:
            raise RuntimeError("durable commit failed (simulated DB unavailable)")
        self.submitted.append((description, session_id, cwd, source))
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


# --------------------------------------------------------------------------- #
# COMPAT — legacy POST /api/instructions envelope + status unchanged (GREEN)
# --------------------------------------------------------------------------- #
def test_APICOMPAT_instructions_success_envelope_unchanged(client, orch):
    """Legacy callers must keep the `{ok, task_id, session}` envelope + 200.
    Regression guard (design §9): the new work must not change this shape.
    """
    r = client.post("/api/instructions", headers=_auth(), json={"description": "do a thing"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("task_id") == "task_web_1"
    assert "session" in body


def test_APICOMPAT_turns_telemetry_routes_unchanged(client):
    """Telemetry `/api/turns` and `/api/turns/{id}` remain the telemetry routes
    (design §9, §3.11: distinct from the new turn-request resources).
    """
    r = client.get("/api/turns", headers=_auth())
    assert r.status_code == 200
    assert "turns" in r.json()
    # Detail route resolves (404 for an unknown id, NOT a routing error).
    r2 = client.get("/api/turns/does-not-exist", headers=_auth())
    assert r2.status_code == 404


# --------------------------------------------------------------------------- #
# TARGET (RED) — commit-before-202 on the new route
# --------------------------------------------------------------------------- #
def test_API_new_turn_request_route_returns_202_after_commit(client):
    """The new `POST /api/sessions/{id}/turn-requests` returns 202 ONLY after
    durable admission (design §9). RED: route not implemented yet → 404.
    """
    r = client.post(
        "/api/sessions/sess-1/turn-requests",
        headers=_auth(),
        json={"body": "hello", "operation_id": "op-1"},
    )
    assert r.status_code == 202, (
        f"new turn-request route did not return 202 (got {r.status_code}); "
        "commit-before-202 route not implemented (design §9)"
    )


def test_API_commit_failure_does_not_acknowledge(client, orch):
    """A failed durable commit must NOT be acknowledged as success — it must
    return 503 with no acceptance (design §6/§8: no accepted envelope on failed
    commit). RED: current handler surfaces a 500 / optimistic path rather than a
    clean 503 fail-closed.
    """
    # Create the session so admission reaches submit_instruction (and then fails
    # in the durable commit), rather than 404-ing on an unknown session.
    from src.core.interfaces import Session, SessionStatus
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc).isoformat()
    orch.session_service.store.save(
        Session(
            session_id="sess-1",
            backend="claude",
            repo_path="/tmp/repo",
            status=SessionStatus.IDLE,
            created_at=now,
            updated_at=now,
            machine_id="worker-a",
        )
    )
    orch.commit_fails = True
    r = client.post("/api/instructions", headers=_auth(), json={"description": "x", "session_id": "sess-1"})
    assert r.status_code == 503, (
        f"failed durable commit returned {r.status_code}, not a fail-closed 503; "
        "acceptance must not be emitted on a failed commit (design §6/§8)"
    )
    assert r.json().get("ok") is not True, "a failed commit still returned ok=True"
