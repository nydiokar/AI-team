"""M3 — read-only web dashboard tests (no network, no paid backend).

Covers: auth enforcement, the read-model JSON endpoints fed by db.list_* +
SessionView, the events poll endpoint (cold tail + incremental since-offset),
and the observability.read_recent_events reader it sits on.
"""
import json

import pytest
from fastapi.testclient import TestClient

from src.control import dashboard
from src.core import observability


TOKEN = "test-dash-token"


@pytest.fixture
def client(monkeypatch):
    # Force a known token regardless of env/config.
    monkeypatch.setattr(dashboard, "_dashboard_token", lambda: TOKEN)
    return TestClient(dashboard.app)


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- auth -------------------------------------------------------------------

def test_health_is_open(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_index_is_open_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "AI-Team Cockpit" in r.text


def test_api_requires_token(client):
    assert client.get("/api/sessions").status_code in (401, 403)  # no header


def test_api_rejects_bad_token(client):
    r = client.get("/api/sessions", headers=_auth("wrong"))
    assert r.status_code == 401


def test_missing_server_token_is_500(monkeypatch):
    monkeypatch.setattr(dashboard, "_dashboard_token", lambda: "")
    c = TestClient(dashboard.app)
    r = c.get("/api/sessions", headers=_auth("anything"))
    assert r.status_code == 500


# --- read-model endpoints ---------------------------------------------------

def test_sessions_endpoint_reflects_store(client, tmp_path):
    # Create a session through the real service/store (isolated DB via conftest).
    from src.services.session_store import SessionStore
    from src.services.session_service import SessionService
    from src.core.interfaces import SessionOrigin

    svc = SessionService(SessionStore())
    res = svc.create_session(
        backend="claude", repo_path=str(tmp_path), chat_id=1,
        origin=SessionOrigin("web", "user"),
    )
    assert res.ok

    r = client.get("/api/sessions", headers=_auth())
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    ids = {s["session_id"] for s in sessions}
    assert res.session.session_id in ids
    mine = next(s for s in sessions if s["session_id"] == res.session.session_id)
    # SessionView shape (M2): derived booleans + origin present.
    assert mine["backend"] == "claude"
    assert mine["is_active"] is True
    assert mine["origin_channel"] == "web"


def test_tasks_and_nodes_endpoints_return_lists(client):
    rt = client.get("/api/tasks", headers=_auth())
    rn = client.get("/api/nodes", headers=_auth())
    assert rt.status_code == 200 and isinstance(rt.json()["tasks"], list)
    assert rn.status_code == 200 and isinstance(rn.json()["nodes"], list)


def test_tasks_limit_validation(client):
    assert client.get("/api/tasks?limit=0", headers=_auth()).status_code == 422
    assert client.get("/api/tasks?limit=99999", headers=_auth()).status_code == 422


# --- events endpoint + reader ----------------------------------------------

@pytest.fixture
def events_file(monkeypatch, tmp_path):
    """Point observability at a temp events.ndjson."""
    monkeypatch.setattr(observability, "_LOGS_DIR", tmp_path)
    return tmp_path / "events.ndjson"


def test_read_recent_events_empty(events_file):
    data = observability.read_recent_events()
    assert data["events"] == [] and data["offset"] == 0


def test_read_recent_events_tail_and_incremental(events_file):
    observability.emit_event("alpha", session_id="s1")
    first = observability.read_recent_events()
    assert [e["event"] for e in first["events"]] == ["alpha"]
    off = first["offset"]
    assert off > 0

    # Nothing new since the last offset.
    same = observability.read_recent_events(since_offset=off)
    assert same["events"] == []

    # Append more; only the delta comes back.
    observability.emit_event("beta", task_id="t1")
    delta = observability.read_recent_events(since_offset=off)
    assert [e["event"] for e in delta["events"]] == ["beta"]


def test_read_recent_events_recovers_after_rotation(events_file):
    observability.emit_event("a")
    observability.emit_event("b")
    off = observability.read_recent_events()["offset"]
    # Simulate rotation: file becomes smaller than the client's stale offset.
    events_file.write_text('{"event":"fresh"}\n', encoding="utf-8")
    data = observability.read_recent_events(since_offset=off)
    # Stale offset must NOT silence the stream — the tail comes back.
    assert [e["event"] for e in data["events"]] == ["fresh"]


def test_read_recent_events_skips_malformed(events_file):
    events_file.write_text(
        '{"event":"good"}\nnot json\n{"event":"good2"}\n', encoding="utf-8"
    )
    data = observability.read_recent_events()
    assert [e["event"] for e in data["events"]] == ["good", "good2"]


def test_events_endpoint(client, events_file):
    observability.emit_event("dispatch", task_id="tX")
    r = client.get("/api/events", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert any(e["event"] == "dispatch" for e in body["events"])
    assert body["offset"] > 0

    # Poll with the offset → no repeats.
    r2 = client.get(f"/api/events?since={body['offset']}", headers=_auth())
    assert r2.json()["events"] == []
