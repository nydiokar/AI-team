"""
A38 — M3 Phase 3.1: Manager role wiring (canonical layers + minimal vertical slice).

Covers, without any live/paid backend:
  * the provider-neutral role seam (`src/core/roles.py`) + Claude adapter;
  * driver role-boot + per-session tool scoping (flag OFF ⇒ byte-identical);
  * admission JOIN branch — a worker task joins the Manager's Case, not a child Case,
    and worker completion leaves the Case OPEN;
  * `orchestrator.invoke_manager` gating + happy path;
  * `POST /api/manager` refusal when the role path is disabled.
"""
import types

import pytest

from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_case_admission's bare-orchestrator pattern)
# ---------------------------------------------------------------------------

def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _orch() -> TaskOrchestrator:
    return TaskOrchestrator.__new__(TaskOrchestrator)


def _task(task_id, metadata=None):
    return types.SimpleNamespace(id=task_id, metadata=metadata)


class _StubStore:
    def __init__(self):
        self._d = {}

    def get(self, sid):
        return self._d.get(sid)

    def save(self, session):
        self._d[session.session_id] = session


def _session(session_id):
    return types.SimpleNamespace(session_id=session_id, current_case_id=None, case_role=None)


def _patch_db(monkeypatch, db):
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    monkeypatch.delenv("HARNESS_FLOW_DRIVE", raising=False)
    monkeypatch.delenv("MANAGER_ROLE_ENABLED", raising=False)
    monkeypatch.delenv("MANAGER_TOOLS_ENABLED", raising=False)


# ---------------------------------------------------------------------------
# Layer 1/6 — provider-neutral role seam + Claude adapter
# ---------------------------------------------------------------------------

def test_load_manager_role_is_neutral_and_stable():
    from src.core.roles import load_manager_role, MANAGER_TOOL_PROFILE
    role = load_manager_role()
    assert role.role_id == "manager"
    assert role.tool_profile == MANAGER_TOOL_PROFILE
    assert role.declared_skills  # boundaries recorded
    # Stable identity ONLY — no per-invocation slots leak into the system role.
    for token in ("{{SPEC_OR_INTENT}}", "{{BRANCH}}", "{{DATE}}"):
        assert token not in role.system_instructions


def test_claude_adapter_appends_to_preset():
    from src.core.roles import load_manager_role
    from src.backends.claude_role_adapter import claude_system_prompt, manager_tool_names
    role = load_manager_role()
    sp = claude_system_prompt(role)
    assert sp == {"type": "preset", "preset": "claude_code", "append": role.system_instructions}
    assert manager_tool_names() == [
        "mcp__manager__dispatch_worker",
        "mcp__manager__wait_for_worker",
        "mcp__manager__get_case",
    ]


def test_render_first_assignment_carries_dynamic_data():
    from src.core.roles import ManagerInvocation, render_first_assignment
    txt = render_first_assignment(
        ManagerInvocation(case_id="c1", objective="ship X", branch="feat/y")
    )
    assert "c1" in txt and "ship X" in txt and "feat/y" in txt


# ---------------------------------------------------------------------------
# Layer 6 — driver role-boot + per-session tool scoping
# ---------------------------------------------------------------------------

def test_tool_scoping_byte_identical_when_flag_off():
    from src.backends import claude_driver as d
    # Flag OFF (default): role arg is ignored ⇒ identical lists, no manager tools.
    assert d._session_allowed_tools() == d._session_allowed_tools(role="manager")
    assert not any("manager" in t for t in d._session_allowed_tools(role="manager"))


def test_tool_scoping_manager_only_when_flag_on(monkeypatch):
    from src.backends import claude_driver as d
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    monkeypatch.setattr(d, "_mcp_manager_configured", lambda: True)
    mgr = d._session_allowed_tools(role="manager")
    other = d._session_allowed_tools(role=None)
    assert all(t in mgr for t in d.manager_tool_names())
    assert not any("manager" in t for t in other)


def test_role_boot_off_is_default_even_for_manager():
    from src.backends import claude_driver as d
    # Flag OFF ⇒ (None, None) even for a manager session (byte-identical boot).
    inst = d.ClaudeSDKClientDriver.__new__(d.ClaudeSDKClientDriver)
    sess_mgr = types.SimpleNamespace(session_id="m1", case_role="manager")
    assert inst._role_boot(sess_mgr) == (None, None)


def test_role_boot_manager_session_when_flag_on(monkeypatch):
    from src.backends import claude_driver as d
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    monkeypatch.setattr(d, "_mcp_manager_configured", lambda: True)
    inst = d.ClaudeSDKClientDriver.__new__(d.ClaudeSDKClientDriver)
    sp, tools = inst._role_boot(types.SimpleNamespace(session_id="m1", case_role="manager"))
    assert sp["type"] == "preset" and sp["preset"] == "claude_code" and sp["append"]
    assert all(t in tools for t in d.manager_tool_names())
    # A non-manager session on the same (ON) flag ⇒ default path.
    assert inst._role_boot(types.SimpleNamespace(session_id="w1", case_role="worker")) == (None, None)


# ---------------------------------------------------------------------------
# Layer 3/4 — admission JOIN branch (worker joins the Manager's Case)
# ---------------------------------------------------------------------------

def test_join_attaches_worker_to_case_no_child(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()
    orch.session_store = _StubStore()
    orch.session_store.save(_session("w1"))

    case_id = db.open_case("ship the case", "mgr-sess", role="manager")
    task = _task("wt-1", {"session_id": "w1", TaskOrchestrator._JOIN_CASE_META_KEY: case_id})

    assert orch._record_flow_run_start(task) is None          # no birth
    assert len(db.list_flow_runs()) == 1                       # still ONE Case (no child)

    task_links = db.list_flow_links(flow_run_id=case_id, entity_type="task", role="task")
    assert [l["entity_id"] for l in task_links] == ["wt-1"]
    attached = [e for e in db.list_flow_events(case_id) if e["event_type"] == "task.attached"]
    assert len(attached) == 1 and attached[0]["entity_id"] == "wt-1"
    # Shared-Case stash under the DISTINCT key ⇒ terminal helper won't auto-close.
    assert task.metadata[TaskOrchestrator._CASE_ID_META_KEY] == case_id
    assert TaskOrchestrator._FLOW_RUN_META_KEY not in task.metadata
    # Worker session affiliated as worker.
    assert orch.session_store.get("w1").case_role == "worker"


def test_join_completion_leaves_case_open(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()
    orch.session_store = _StubStore()

    case_id = db.open_case("obj", "mgr-sess", role="manager")
    task = _task("wt-1", {TaskOrchestrator._JOIN_CASE_META_KEY: case_id})
    orch._record_flow_run_start(task)
    orch._flow_terminal_outcome(task, success=True)
    assert db.get_flow_run(case_id)["status"] is None  # Task finished != Case completed


def test_join_absent_or_closed_case_falls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()
    orch.session_store = _StubStore()

    # Bogus/absent case id ⇒ no attach, no child birth (standalone fall-through).
    task = _task("wt-1", {"session_id": "w1", TaskOrchestrator._JOIN_CASE_META_KEY: "nope"})
    assert orch._record_flow_run_start(task) is None
    assert db.list_flow_links(flow_run_id="nope", entity_type="task") == []
    assert len(db.list_flow_runs()) == 0
    assert TaskOrchestrator._CASE_ID_META_KEY not in task.metadata


# ---------------------------------------------------------------------------
# Layer 4 — orchestrator.invoke_manager (boot seam)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_manager_refuses_when_disabled():
    orch = _orch()
    result = await orch.invoke_manager("obj", repo_path="/x")
    assert result == {"ok": False, "reason": "manager_role_disabled"}


@pytest.mark.asyncio
async def test_invoke_manager_opens_case_and_boots(monkeypatch):
    from src.services.session_service import CommandResult
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    orch = _orch()

    session = types.SimpleNamespace(session_id="mgr-1", repo_path="/x")
    orch.session_service = types.SimpleNamespace(
        create_session=lambda **kw: CommandResult(True, session=session)
    )
    opened = {}
    orch.open_case = lambda objective, sid, role="manager", completion_criteria=None: opened.setdefault("case", "case-1") or opened["case"]
    submitted = {}

    async def _submit(description, session_id=None, cwd=None, source="runtime", **_):
        submitted.update(description=description, session_id=session_id, source=source)
        return "task-1"

    orch.submit_instruction = _submit

    result = await orch.invoke_manager("ship X", repo_path="/x")
    assert result == {"ok": True, "session_id": "mgr-1", "case_id": "case-1", "task_id": "task-1"}
    assert submitted["session_id"] == "mgr-1" and "ship X" in submitted["description"]


# ---------------------------------------------------------------------------
# Layer surface — POST /api/manager refuses when the role path is disabled
# ---------------------------------------------------------------------------

def _manager_client(monkeypatch, invoke_result):
    from fastapi.testclient import TestClient
    from src.control import control_api

    async def _invoke(**kw):
        return invoke_result

    orch = types.SimpleNamespace(invoke_manager=_invoke)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "tok")
    return TestClient(control_api.build_control_api(orch))


def test_api_manager_disabled_is_409(monkeypatch):
    client = _manager_client(monkeypatch, {"ok": False, "reason": "manager_role_disabled"})
    r = client.post("/api/manager", headers={"Authorization": "Bearer tok"},
                    json={"objective": "x", "repo_path": "/x"})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "manager_role_disabled"


def test_api_manager_ok(monkeypatch):
    client = _manager_client(
        monkeypatch,
        {"ok": True, "session_id": "s1", "case_id": "c1", "task_id": "t1"},
    )
    r = client.post("/api/manager", headers={"Authorization": "Bearer tok"},
                    json={"objective": "x", "repo_path": "/x"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "session_id": "s1", "case_id": "c1", "task_id": "t1"}
