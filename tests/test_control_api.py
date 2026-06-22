"""U1 — embedded Control API tests (no network, no paid backend).

The Control API is built from a live orchestrator (build_control_api), so its
read handlers call orchestrator.session_service directly instead of side-reading
files. These tests use a minimal stand-in orchestrator wrapping the real
SessionService over an isolated store (conftest), plus the real DB for
tasks/nodes — mirroring test_dashboard.py's coverage on the new in-process surface.
"""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


TOKEN = "test-control-token"


class _StubOrchestrator:
    """Minimal orchestrator: only what the read handlers touch (session_service)."""

    def __init__(self) -> None:
        self.session_service = SessionService(SessionStore())


@pytest.fixture
def orch():
    return _StubOrchestrator()


@pytest.fixture
def client(monkeypatch, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(orch))


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- auth -------------------------------------------------------------------

def test_health_is_open(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_api_requires_token(client):
    assert client.get("/api/sessions").status_code in (401, 403)


def test_api_rejects_bad_token(client):
    assert client.get("/api/sessions", headers=_auth("wrong")).status_code == 401


def test_missing_server_token_is_500(monkeypatch, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "")
    c = TestClient(control_api.build_control_api(orch))
    assert c.get("/api/sessions", headers=_auth("anything")).status_code == 500


# --- read-model endpoints (fed by the live SessionService) ------------------

def test_sessions_endpoint_reflects_service(client, orch, tmp_path):
    from src.core.interfaces import SessionOrigin

    res = orch.session_service.create_session(
        backend="claude", repo_path=str(tmp_path), chat_id=1,
        origin=SessionOrigin("web", "user"),
    )
    assert res.ok

    r = client.get("/api/sessions", headers=_auth())
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    mine = next(s for s in sessions if s["session_id"] == res.session.session_id)
    assert mine["backend"] == "claude"
    assert mine["is_active"] is True
    assert mine["origin_channel"] == "web"


def test_sessions_limit_validation_and_bound(client, orch, tmp_path):
    for _ in range(5):
        orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    assert client.get("/api/sessions?limit=0", headers=_auth()).status_code == 422
    assert client.get("/api/sessions?limit=99999", headers=_auth()).status_code == 422
    r = client.get("/api/sessions?limit=2", headers=_auth())
    assert r.status_code == 200 and len(r.json()["sessions"]) == 2


def test_tasks_and_nodes_endpoints_return_lists(client):
    rt = client.get("/api/tasks", headers=_auth())
    rn = client.get("/api/nodes", headers=_auth())
    assert rt.status_code == 200 and isinstance(rt.json()["tasks"], list)
    assert rn.status_code == 200 and isinstance(rn.json()["nodes"], list)


def test_tasks_limit_validation(client):
    assert client.get("/api/tasks?limit=0", headers=_auth()).status_code == 422
    assert client.get("/api/tasks?limit=99999", headers=_auth()).status_code == 422


# --- nodes: DB fallback annotates liveness when the registry is empty -------

def test_nodes_fallback_annotates_live_when_registry_empty(client):
    from src.control.db import get_db
    d = get_db()
    d.upsert_node(node_id="N1", tailscale_ip="127.0.0.1", api_port=9001,
                  backends=["claude"], max_concurrent=1)  # last_heartbeat=now
    r = client.get("/api/nodes", headers=_auth())
    assert r.status_code == 200
    n1 = next(n for n in r.json()["nodes"] if n["node_id"] == "N1")
    assert n1["live"] is True
    assert "heartbeat_age_sec" in n1


# --- events poll (same reader as the dashboard) -----------------------------

def test_events_endpoint(client, monkeypatch, tmp_path):
    from src.core import observability
    monkeypatch.setattr(observability, "_LOGS_DIR", tmp_path)
    observability.emit_event("dispatch", task_id="tX")
    r = client.get("/api/events", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert any(e["event"] == "dispatch" for e in body["events"])
    r2 = client.get(f"/api/events?since={body['offset']}", headers=_auth())
    assert r2.json()["events"] == []
