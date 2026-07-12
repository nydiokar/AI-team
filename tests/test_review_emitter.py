"""M3.2 slice 1 — review.* verdict emitter tests (no network, no paid CLI).

HERMETIC: the MCP tool surface is imported directly (like test_mcp_manager); the
Control API route runs over a real temp MeshDB + a real ``record_review`` seam via
the FastAPI TestClient (like test_control_api_flows); the close-gate is exercised at
the db level on a real temp MeshDB (like test_case_closure).

Proves:
  (a) 'record_review' is a registered Manager tool;
  (b) flag ON ⇒ POST /api/cases/{id}/review appends the correct review.* event per verdict;
  (c) db.close_case refuses on an unresolved rework, closes after a later accept OR waive;
  (d) flag OFF is byte-identical ⇒ route 404 + no review gate in close_case.

Run: `pytest tests/test_review_emitter.py -q`
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.control import control_api
import src.control.db as db_mod
from src.control.db import MeshDB, CaseCloseBlocked
from src.orchestrator import TaskOrchestrator
from src.services.session_store import SessionStore
from src.services.session_service import SessionService

# Import the MCP tool surface the same hermetic way test_mcp_manager does.
os.environ["AI_TEAM_ENV_FILE"] = "/nonexistent/mcp_manager_test.env"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
mcp_manager = importlib.import_module("mcp_manager")


TOKEN = "test-control-token"


# ---------------------------------------------------------------------------
# (a) record_review is a registered Manager tool
# ---------------------------------------------------------------------------

def test_record_review_registered_in_tools():
    names = {t["name"] for t in mcp_manager._TOOLS}
    assert "record_review" in names
    assert "record_review" in mcp_manager._TOOL_IMPLS
    tool = next(t for t in mcp_manager._TOOLS if t["name"] == "record_review")
    assert set(tool["inputSchema"]["required"]) == {"case_id", "verdict"}


def test_record_review_rejects_bad_verdict():
    with pytest.raises(ValueError):
        mcp_manager._record_review({"case_id": "c-1", "verdict": "nope"})


# ---------------------------------------------------------------------------
# Control API fixtures (real seam over a real temp db)
# ---------------------------------------------------------------------------

class _StubOrchestrator:
    def __init__(self) -> None:
        self.session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)

    # Bind the real orchestrator seam (uses get_db(), patched below).
    record_review = TaskOrchestrator.record_review


@pytest.fixture
def db(tmp_path):
    return MeshDB(str(tmp_path / "mesh.db"))


@pytest.fixture
def client(monkeypatch, db):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    monkeypatch.setattr(control_api, "_db", lambda: db)
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    return TestClient(control_api.build_control_api(_StubOrchestrator()))


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# (b) flag ON ⇒ route appends the correct review.* event per verdict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict,event_type", [
    ("accepted", "review.accepted"),
    ("rework_requested", "review.rework_requested"),
    ("waived", "review.waived"),
])
def test_review_route_appends_event(monkeypatch, client, db, verdict, event_type):
    monkeypatch.setenv("REVIEW_EMITTER_ENABLED", "1")
    fid = db.open_case("obj", f"sess-{verdict}")

    r = client.post(
        f"/api/cases/{fid}/review",
        json={"verdict": verdict, "reason": "why"},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["event_type"] == event_type

    events = [e for e in db.list_flow_events(fid) if e["event_type"] == event_type]
    assert len(events) == 1
    import json as _json
    payload = _json.loads(events[0]["payload_json"])
    assert payload == {"verdict": verdict, "reason": "why"}
    assert events[0]["actor"] == "manager"


def test_review_route_invalid_verdict_422(monkeypatch, client, db):
    monkeypatch.setenv("REVIEW_EMITTER_ENABLED", "1")
    fid = db.open_case("obj", "sess-x")
    r = client.post(f"/api/cases/{fid}/review", json={"verdict": "bogus"}, headers=_auth())
    assert r.status_code == 422


def test_review_route_requires_token(monkeypatch, client, db):
    monkeypatch.setenv("REVIEW_EMITTER_ENABLED", "1")
    fid = db.open_case("obj", "sess-x")
    assert client.post(f"/api/cases/{fid}/review", json={"verdict": "accepted"}).status_code in (401, 403)


# ---------------------------------------------------------------------------
# (c) close-gate: refuse on unresolved rework; close after later accept OR waive
# ---------------------------------------------------------------------------

def test_close_blocked_by_unresolved_rework(monkeypatch, tmp_path):
    monkeypatch.setenv("REVIEW_EMITTER_ENABLED", "1")
    db = MeshDB(str(tmp_path / "mesh.db"))
    fid = db.open_case("obj", "sess-1")
    db.append_flow_event(fid, "review.rework_requested", "manager",
                         payload={"verdict": "rework_requested", "reason": "fix it"})
    with pytest.raises(CaseCloseBlocked):
        db.close_case(fid)


def test_close_allowed_after_later_accept(monkeypatch, tmp_path):
    monkeypatch.setenv("REVIEW_EMITTER_ENABLED", "1")
    db = MeshDB(str(tmp_path / "mesh.db"))
    fid = db.open_case("obj", "sess-1")
    db.append_flow_event(fid, "review.rework_requested", "manager", payload={"verdict": "rework_requested"})
    db.append_flow_event(fid, "review.accepted", "manager", payload={"verdict": "accepted"})
    assert db.close_case(fid) is True


def test_close_allowed_after_later_waive(monkeypatch, tmp_path):
    monkeypatch.setenv("REVIEW_EMITTER_ENABLED", "1")
    db = MeshDB(str(tmp_path / "mesh.db"))
    fid = db.open_case("obj", "sess-1")
    db.append_flow_event(fid, "review.rework_requested", "manager", payload={"verdict": "rework_requested"})
    db.append_flow_event(fid, "review.waived", "manager", payload={"verdict": "waived", "reason": "ok"})
    assert db.close_case(fid) is True


# ---------------------------------------------------------------------------
# (d) flag OFF is byte-identical: route 404 + no review gate in close_case
# ---------------------------------------------------------------------------

def test_review_route_404_when_flag_off(monkeypatch, client, db):
    monkeypatch.delenv("REVIEW_EMITTER_ENABLED", raising=False)
    fid = db.open_case("obj", "sess-1")
    r = client.post(f"/api/cases/{fid}/review", json={"verdict": "accepted"}, headers=_auth())
    assert r.status_code == 404
    # Nothing was appended.
    assert [e for e in db.list_flow_events(fid) if e["event_type"].startswith("review.")] == []


def test_close_ignores_review_gate_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.delenv("REVIEW_EMITTER_ENABLED", raising=False)
    db = MeshDB(str(tmp_path / "mesh.db"))
    fid = db.open_case("obj", "sess-1")
    # An unresolved rework event exists, but with the flag OFF the gate is skipped.
    db.append_flow_event(fid, "review.rework_requested", "manager", payload={"verdict": "rework_requested"})
    assert db.close_case(fid) is True
