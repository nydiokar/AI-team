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
        self.brief_calls = []
        self.reconcile_calls = []
        self.sweep_calls = []
        self.state_calls = []
        self._brief_ok = True

    def arm_wait_group(self, case_id, wait_group_id, condition, member_task_ids, *, actor="manager"):
        self.calls.append((case_id, wait_group_id, condition, list(member_task_ids), actor))
        return {"ok": True, "event_id": 42}

    def get_case_brief(self, case_id):
        self.brief_calls.append(case_id)
        if not self._brief_ok:
            return {"ok": False, "reason": "case_not_found"}
        return {"ok": True, "brief": {"case_id": case_id, "workers": []}}

    def boot_reconcile_case(self, case_id, *, actor="manager"):
        self.reconcile_calls.append((case_id, actor))
        return {"ok": True, "reconciled": {"resolved": []}, "rearmed": []}

    async def sweep_orphaned_cases(self, *, limit=200, dry_run=False, reason="manager_session_unavailable"):
        self.sweep_calls.append({"limit": limit, "dry_run": dry_run, "reason": reason})
        return {"ok": True, "dry_run": dry_run, "scanned": limit, "candidates": [], "cleaned": []}

    async def set_case_state(self, case_id, *, state, actor="operator", reason="operator_state_change"):
        self.state_calls.append({"case_id": case_id, "state": state, "actor": actor, "reason": reason})
        return {"ok": True, "changed": True, "status": state}


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


# --- A54 get_case_brief route (read-only, NOT flag-gated) -------------------

def test_brief_requires_auth(client):
    assert client.get("/api/cases/c1/brief").status_code in (401, 403)


def test_brief_returns_state_no_flag_needed(client, monkeypatch, orch):
    _off(monkeypatch)  # read is NOT flag-gated — still served
    r = client.get("/api/cases/c1/brief", headers=_auth())
    assert r.status_code == 200
    assert r.json()["brief"]["case_id"] == "c1"
    assert orch.brief_calls == ["c1"]


def test_brief_404_for_unknown_case(client, monkeypatch, orch):
    orch._brief_ok = False
    r = client.get("/api/cases/nope/brief", headers=_auth())
    assert r.status_code == 404


# --- A54 boot-reconcile route (flag-gated like /waits/reconcile) ------------

def test_boot_reconcile_requires_auth(client):
    assert client.post("/api/cases/c1/boot-reconcile").status_code in (401, 403)


def test_boot_reconcile_404_when_relay_off(client, monkeypatch, orch):
    monkeypatch.delenv("DURABLE_RELAY_ENABLED", raising=False)
    r = client.post("/api/cases/c1/boot-reconcile", headers=_auth())
    assert r.status_code == 404
    assert orch.reconcile_calls == []  # never reached the seam


def test_boot_reconcile_delegates_when_relay_on(client, monkeypatch, orch):
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")
    r = client.post("/api/cases/c1/boot-reconcile", headers=_auth())
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert orch.reconcile_calls == [("c1", "manager")]


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


# --------------------------------------------------------------------------- #
# [A53] POST /api/cases/{id}/interrupt (kill path)                             #
# --------------------------------------------------------------------------- #

class _KillOrchestrator:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def interrupt_case(self, case_id, *, actor="operator", reason="operator_kill"):
        self.calls.append((case_id, actor, reason))
        return self._result


def _kill_client(monkeypatch, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(orch))


def test_interrupt_requires_auth(monkeypatch):
    orch = _KillOrchestrator({"ok": True, "cancelled_tasks": [], "already": False})
    client = _kill_client(monkeypatch, orch)
    assert client.post("/api/cases/c1/interrupt", json={}).status_code in (401, 403)


def test_interrupt_ok(monkeypatch):
    orch = _KillOrchestrator({"ok": True, "cancelled_tasks": ["t1"], "already": False, "status": "blocked"})
    client = _kill_client(monkeypatch, orch)
    r = client.post("/api/cases/c1/interrupt", json={"reason": "runaway"}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["cancelled_tasks"] == ["t1"]
    assert orch.calls == [("c1", "operator", "runaway")]


def test_interrupt_unknown_case_404(monkeypatch):
    orch = _KillOrchestrator({"ok": False, "reason": "case_not_found"})
    client = _kill_client(monkeypatch, orch)
    r = client.post("/api/cases/nope/interrupt", json={}, headers=_auth())
    assert r.status_code == 404


def test_interrupt_not_flag_gated(monkeypatch):
    """The kill path is a safety valve — reachable even with CASE_CONTINUATION_ENABLED off."""
    monkeypatch.delenv("CASE_CONTINUATION_ENABLED", raising=False)
    orch = _KillOrchestrator({"ok": True, "cancelled_tasks": [], "already": False})
    client = _kill_client(monkeypatch, orch)
    r = client.post("/api/cases/c1/interrupt", json={}, headers=_auth())
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# POST /api/cases/orphans/sweep                                                #
# --------------------------------------------------------------------------- #

def test_orphan_sweep_requires_auth(client):
    assert client.post("/api/cases/orphans/sweep", json={}).status_code in (401, 403)


def test_orphan_sweep_delegates_to_orchestrator(client, orch):
    r = client.post(
        "/api/cases/orphans/sweep",
        json={"dry_run": True, "limit": 7, "reason": "manual_cleanup"},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert orch.sweep_calls == [{
        "limit": 7,
        "dry_run": True,
        "reason": "manual_cleanup",
    }]


# --------------------------------------------------------------------------- #
# POST /api/cases/{id}/state                                                   #
# --------------------------------------------------------------------------- #

def test_case_state_requires_auth(client):
    assert client.post("/api/cases/c1/state", json={"state": "open"}).status_code in (401, 403)


def test_case_state_delegates_to_orchestrator(client, orch):
    r = client.post(
        "/api/cases/c1/state",
        json={"state": "open", "reason": "recovered"},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "open"
    assert orch.state_calls == [{
        "case_id": "c1",
        "state": "open",
        "actor": "operator",
        "reason": "recovered",
    }]


def test_case_state_unknown_case_404(monkeypatch):
    class _StateFailOrchestrator(_StubOrchestrator):
        async def set_case_state(self, case_id, *, state, actor="operator", reason="operator_state_change"):
            return {"ok": False, "reason": "case_not_found"}

    client = _kill_client(monkeypatch, _StateFailOrchestrator())
    r = client.post("/api/cases/nope/state", json={"state": "open"}, headers=_auth())
    assert r.status_code == 404
