"""M3.4 — control-API wait-group route tests (no network, no paid backend).

``POST /api/cases/{id}/wait-group`` arms a Manager wait-group so the Wake-Dispatcher
re-enters the Case. The route is flag-gated (404 when CASE_CONTINUATION_ENABLED is
OFF ⇒ byte-identical), validates the condition + member list up front (§7 service
boundary), and delegates to ``orchestrator.arm_wait_group``. These assert the route
contract; the db-layer arming is covered by ``test_case_continuation``.
"""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api


TOKEN = "test-control-token"


class _StubOrchestrator:
    def __init__(self):
        self.calls = []

    def arm_wait_group(self, case_id, wait_group_id, condition, member_task_ids, *, actor="manager"):
        self.calls.append((case_id, wait_group_id, condition, list(member_task_ids), actor))
        return {"ok": True, "event_id": 42}


@pytest.fixture
def orch():
    return _StubOrchestrator()


@pytest.fixture
def client(monkeypatch, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(orch))


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _on(monkeypatch):
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")


def _off(monkeypatch):
    monkeypatch.delenv("CASE_CONTINUATION_ENABLED", raising=False)


_BODY = {"wait_group_id": "g1", "condition": "ALL", "member_task_ids": ["t1", "t2"]}


def test_requires_auth(client):
    assert client.post("/api/cases/c1/wait-group", json=_BODY).status_code in (401, 403)


def test_404_when_flag_off(client, monkeypatch, orch):
    _off(monkeypatch)
    r = client.post("/api/cases/c1/wait-group", json=_BODY, headers=_auth())
    assert r.status_code == 404
    assert orch.calls == []  # never reached the seam


def test_arms_and_delegates_when_flag_on(client, monkeypatch, orch):
    _on(monkeypatch)
    r = client.post("/api/cases/c1/wait-group", json=_BODY, headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "event_id": 42}
    assert orch.calls == [("c1", "g1", "ALL", ["t1", "t2"], "manager")]


def test_condition_normalized_to_upper(client, monkeypatch, orch):
    _on(monkeypatch)
    body = {**_BODY, "condition": "any"}
    client.post("/api/cases/c1/wait-group", json=body, headers=_auth())
    assert orch.calls[0][2] == "ANY"


def test_invalid_condition_422(client, monkeypatch, orch):
    _on(monkeypatch)
    body = {**_BODY, "condition": "SOME"}
    r = client.post("/api/cases/c1/wait-group", json=body, headers=_auth())
    assert r.status_code == 422
    assert orch.calls == []


def test_empty_members_422(client, monkeypatch, orch):
    _on(monkeypatch)
    body = {**_BODY, "member_task_ids": []}
    r = client.post("/api/cases/c1/wait-group", json=body, headers=_auth())
    assert r.status_code == 422
    assert orch.calls == []


def test_too_many_members_413(client, monkeypatch, orch):
    _on(monkeypatch)
    body = {**_BODY, "member_task_ids": [f"t{i}" for i in range(257)]}
    r = client.post("/api/cases/c1/wait-group", json=body, headers=_auth())
    assert r.status_code == 413
    assert orch.calls == []


# --------------------------------------------------------------------------- #
# [A52] CaseOpenBody.round_cap validation (gt=0)                               #
# --------------------------------------------------------------------------- #

def test_case_open_body_rejects_non_positive_round_cap():
    import pytest as _pytest
    from pydantic import ValidationError
    from src.control.control_api import CaseOpenBody

    for bad in (0, -1):
        with _pytest.raises(ValidationError):
            CaseOpenBody(objective="o", session_id="s", round_cap=bad)


def test_case_open_body_accepts_positive_round_cap_and_none():
    from src.control.control_api import CaseOpenBody

    assert CaseOpenBody(objective="o", session_id="s", round_cap=6).round_cap == 6
    assert CaseOpenBody(objective="o", session_id="s").round_cap is None
