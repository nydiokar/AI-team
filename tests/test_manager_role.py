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
        "mcp__manager__open_case",
        "mcp__manager__get_case",
        "mcp__manager__read_session_history",
        "mcp__manager__close_case",
        "mcp__manager__record_review",
        "mcp__manager__release_worker",
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


# ---------------------------------------------------------------------------
# Tool: dispatch_worker JOIN hint + wait_for_worker resolution for a joined worker
# ---------------------------------------------------------------------------

def test_dispatch_worker_join_hint_points_wait_at_case(monkeypatch):
    import scripts.mcp_manager as m
    monkeypatch.setattr(m, "_api_request", lambda *a, **k: {"task_id": "wt-9", "session": {}})
    out = m._dispatch_worker(
        {"objective": "do a bounded thing", "case_id": "case-7", "session_id": "warm-w"}
    )
    # A joined worker has no own flow_run — the wait hint MUST carry the Case id.
    assert "wait_for_worker(task_id='wt-9', flow_run_id='case-7')" in out
    assert "JOINS" in out


def test_wait_for_worker_resolves_joined_worker_via_case_timeline(monkeypatch):
    import scripts.mcp_manager as m

    def fake(method, path, *a, **k):
        if path.startswith("/api/flows/"):
            return {"flow": {"status": None, "current_stage": "execution"}}  # Case OPEN
        if "/timeline" in path:
            return {"events": [
                {"event_type": "task.attached", "entity_id": "wt-9"},
                {"event_type": "task.finished", "entity_id": "wt-9",
                 "payload_json": {"outcome": "success"}},
            ]}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(m, "_api_request", fake)
    out = m._wait_for_worker({"task_id": "wt-9", "flow_run_id": "case-7", "poll_interval": 1})
    assert "DONE" in out and "task.finished" in out


def test_wait_for_worker_ignores_other_tasks_finished_on_case(monkeypatch):
    """The Manager's own turn also emits task.finished on the Case — wait must
    filter by the worker's task_id and NOT return on the manager's event."""
    import scripts.mcp_manager as m
    state = {"polls": 0}

    def fake(method, path, *a, **k):
        if path.startswith("/api/flows/"):
            return {"flow": {"status": None}}
        if "/timeline" in path:
            state["polls"] += 1
            events = [{"event_type": "task.finished", "entity_id": "mgr-turn-1",
                       "payload_json": {"outcome": "success"}}]
            if state["polls"] >= 2:  # worker finishes on the 2nd poll
                events.append({"event_type": "task.finished", "entity_id": "wt-9",
                               "payload_json": {"outcome": "success"}})
            return {"events": events}
        raise AssertionError(path)

    monkeypatch.setattr(m, "_api_request", fake)
    out = m._wait_for_worker({"task_id": "wt-9", "flow_run_id": "case-7", "poll_interval": 0.01})
    assert "DONE" in out
    assert state["polls"] >= 2  # did not falsely return on the manager's own task.finished


# ---------------------------------------------------------------------------
# Tool: close_case + POST /api/cases/{id}/close (the Decision surface)
# ---------------------------------------------------------------------------

def test_close_case_tool_success_and_refusal(monkeypatch):
    import scripts.mcp_manager as m

    monkeypatch.setattr(m, "_api_request", lambda *a, **k: {"ok": True, "closed": True, "reason": None})
    assert "CLOSED" in m._close_case({"case_id": "c1"})

    monkeypatch.setattr(
        m, "_api_request",
        lambda *a, **k: {"ok": False, "closed": False, "reason": "completion_criteria not reconciled: ['tests green']"},
    )
    refused = m._close_case({"case_id": "c1"})
    assert "REFUSED" in refused and "completion_criteria" in refused


def test_api_close_case_returns_result_dict(monkeypatch):
    from fastapi.testclient import TestClient
    from src.control import control_api

    calls = {}

    def _close(case_id, *, outcome="closed", actor="operator", criteria_reconciliation=None):
        calls.update(case_id=case_id, actor=actor, outcome=outcome)
        return {"ok": False, "closed": False, "reason": "case has 1 open child flow(s)"}

    orch = types.SimpleNamespace(close_case=_close)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "tok")
    client = TestClient(control_api.build_control_api(orch))
    r = client.post("/api/cases/c1/close", headers={"Authorization": "Bearer tok"}, json={})
    # A blocked close is a normal 200 decision signal, not an HTTP error.
    assert r.status_code == 200
    assert r.json()["reason"] == "case has 1 open child flow(s)"
    assert calls["case_id"] == "c1" and calls["actor"] == "manager"


# ---------------------------------------------------------------------------
# [Manager-fork] Seed the Manager boot from a prior conversation
#   invoke_manager: continued_from -> create_session; continue_inline/continues
#   -> first-turn extra_metadata (consumed by the proven compact-context injector).
# ---------------------------------------------------------------------------

def _wire_manager_orch(monkeypatch, session_id="mgr-1"):
    """Bare orchestrator with create_session/open_case/submit_instruction stubbed,
    capturing the kwargs invoke_manager forwards to each seam."""
    from src.services.session_service import CommandResult
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    orch = _orch()
    session = types.SimpleNamespace(session_id=session_id, repo_path="/x")
    cap = {"create_kw": None, "submit_kw": None}

    def _create(**kw):
        cap["create_kw"] = kw
        return CommandResult(True, session=session)

    orch.session_service = types.SimpleNamespace(create_session=_create)
    orch.open_case = lambda objective, sid, role="manager", completion_criteria=None: "case-1"

    async def _submit(description, session_id=None, cwd=None, source="runtime",
                      extra_metadata=None, **_):
        cap["submit_kw"] = {"session_id": session_id, "extra_metadata": extra_metadata,
                            "description": description}
        return "task-1"

    orch.submit_instruction = _submit
    return orch, cap


@pytest.mark.asyncio
async def test_invoke_manager_threads_continued_from_and_inline(monkeypatch):
    orch, cap = _wire_manager_orch(monkeypatch)
    res = await orch.invoke_manager(
        "ship X", repo_path="/x",
        continued_from="prior-sess", continue_inline="You: hi\n\nAgent: done",
    )
    assert res["ok"] is True
    # Lineage pointer reaches create_session.
    assert cap["create_kw"]["continued_from"] == "prior-sess"
    # Prior-conversation digest reaches the FIRST assignment turn's metadata.
    assert cap["submit_kw"]["extra_metadata"] == {"continue_inline": "You: hi\n\nAgent: done"}


@pytest.mark.asyncio
async def test_invoke_manager_continues_used_when_no_inline(monkeypatch):
    orch, cap = _wire_manager_orch(monkeypatch)
    await orch.invoke_manager("ship X", repo_path="/x", continues="  task-42  ")
    # Server-side compact-context path; id is stripped.
    assert cap["submit_kw"]["extra_metadata"] == {"continues": "task-42"}


@pytest.mark.asyncio
async def test_invoke_manager_inline_precedes_continues(monkeypatch):
    orch, cap = _wire_manager_orch(monkeypatch)
    await orch.invoke_manager(
        "ship X", repo_path="/x", continue_inline="digest", continues="task-42",
    )
    # Inline wins (a fork never also references a parent task) — matches the injector's
    # own precedence in _maybe_inject_compact_context.
    assert cap["submit_kw"]["extra_metadata"] == {"continue_inline": "digest"}


@pytest.mark.asyncio
async def test_invoke_manager_no_fork_seed_is_byte_identical(monkeypatch):
    orch, cap = _wire_manager_orch(monkeypatch)
    await orch.invoke_manager("ship X", repo_path="/x")
    assert cap["create_kw"]["continued_from"] is None
    assert cap["submit_kw"]["extra_metadata"] is None
    # Blank / whitespace-only seeds are treated as absent (no metadata added).
    await orch.invoke_manager("ship X", repo_path="/x", continue_inline="   ", continues="")
    assert cap["submit_kw"]["extra_metadata"] is None


@pytest.mark.asyncio
async def test_manager_fork_meta_injects_prior_context_end_to_end(monkeypatch):
    """End-to-end seam: the exact extra_metadata invoke_manager emits for a fork,
    when it lands on a task, drives the REAL compact-context injector to rewrite the
    Manager's first-turn prompt with a fenced <prior_context> block — proving a forked
    Manager wakes carrying the prior line of work."""
    orch = _orch()
    orch._compact_injected_ids = set()
    fork_meta = {"continue_inline": "You: fix the gauge\n\nAgent: I edited ContextFillGauge.tsx"}
    task = types.SimpleNamespace(id="task-boot", prompt="Continue the work.", metadata=fork_meta)
    await orch._maybe_inject_compact_context(task)
    assert "<prior_context" in task.prompt
    assert "<current_instruction>\nContinue the work.\n</current_instruction>" in task.prompt
    assert "I edited ContextFillGauge.tsx" in task.prompt


def test_api_manager_forwards_fork_fields(monkeypatch):
    from fastapi.testclient import TestClient
    from src.control import control_api

    captured = {}

    async def _invoke(**kw):
        captured.update(kw)
        return {"ok": True, "session_id": "s1", "case_id": "c1", "task_id": "t1"}

    orch = types.SimpleNamespace(invoke_manager=_invoke)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "tok")
    client = TestClient(control_api.build_control_api(orch))
    r = client.post(
        "/api/manager", headers={"Authorization": "Bearer tok"},
        json={"objective": "x", "repo_path": "/x", "continued_from": "sess-9",
              "continue_inline": "You: a\n\nAgent: b", "continues": "task-7"},
    )
    assert r.status_code == 200
    assert captured["continued_from"] == "sess-9"
    assert captured["continue_inline"] == "You: a\n\nAgent: b"
    assert captured["continues"] == "task-7"


def test_api_manager_rejects_oversize_inline(monkeypatch):
    from fastapi.testclient import TestClient
    from src.control import control_api

    async def _invoke(**kw):
        return {"ok": True, "session_id": "s1", "case_id": "c1", "task_id": "t1"}

    orch = types.SimpleNamespace(invoke_manager=_invoke)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "tok")
    client = TestClient(control_api.build_control_api(orch))
    r = client.post(
        "/api/manager", headers={"Authorization": "Bearer tok"},
        json={"objective": "x", "repo_path": "/x", "continue_inline": "z" * 48001},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_remote_manager_payload_carries_injected_prior_context(monkeypatch):
    """Regression for the cross-layer inertness gap the adversarial review found:
    a NODE-PINNED forked Manager's mesh payload must snapshot the INJECTED prompt
    (prior context), not the bare one. The injector must run BEFORE _mesh_enqueue_task
    freezes payload['prompt'] — otherwise the node worker (which never runs the
    injector itself) executes the first turn with no prior line of work."""
    import socket
    orch = _orch()
    orch._compact_injected_ids = set()
    remote = "some-remote-node"
    assert remote != socket.gethostname()  # a true remote pin
    session = types.SimpleNamespace(
        session_id="mgr-1", machine_id=remote, backend_session_id=None, repo_path="/x",
        backend="claude", model=None, telegram_chat_id=None, telegram_thread_id=None,
        owner_user_id=None, last_user_message=None, driver_type=None, driver_status=None,
        cache_health=None, cache_unhealthy_count=0, previous_backend_session_ids=[],
        case_role="manager", current_case_id="case-1", role_boot=None,
    )
    orch.session_store = types.SimpleNamespace(get=lambda sid: session)

    captured = {}

    class _FakeDB:
        def enqueue_task(self, *, task_id, session_id, machine_id, backend, action, payload):
            captured["payload"] = payload

        def claim_task(self, *a, **k):
            return True

    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: _FakeDB())

    task = types.SimpleNamespace(
        id="task-boot", prompt="Continue the work.",
        metadata={"session_id": "mgr-1",
                  "continue_inline": "You: fix gauge\n\nAgent: edited ContextFillGauge.tsx"},
    )
    # Mirror _task_worker's order: inject, THEN enqueue the mesh row.
    await orch._maybe_inject_compact_context(task)
    orch._mesh_enqueue_task(task, "claude")

    assert "payload" in captured, "remote row must be enqueued"
    assert "<prior_context" in captured["payload"]["prompt"]
    assert "edited ContextFillGauge.tsx" in captured["payload"]["prompt"]
    assert "<current_instruction>\nContinue the work.\n</current_instruction>" in captured["payload"]["prompt"]


@pytest.mark.asyncio
async def test_remote_create_session_first_message_carries_prompt(monkeypatch):
    """Regression for the live 'empty message' Manager boot (session dc4d164339a3, Horse):
    a node-pinned create_session seeds its FIRST user message from
    session.last_user_message (claude_code.create_session), NOT payload['prompt'].
    The gateway sets last_user_message only later in process_task, so the mesh
    snapshot must carry THIS turn's prompt as last_user_message or the remote
    Manager boots with an empty first turn and drops the objective."""
    import socket
    orch = _orch()
    orch._compact_injected_ids = set()
    remote = "Horse"
    assert remote != socket.gethostname()
    session = types.SimpleNamespace(
        session_id="mgr-remote", machine_id=remote, backend_session_id=None, repo_path="/x",
        backend="claude", model=None, telegram_chat_id=None, telegram_thread_id=None,
        owner_user_id=None, last_user_message="", driver_type=None, driver_status=None,
        cache_health=None, cache_unhealthy_count=0, previous_backend_session_ids=[],
        case_role="manager", current_case_id="case-1", role_boot=None,
    )
    orch.session_store = types.SimpleNamespace(get=lambda sid: session)

    captured = {}

    class _FakeDB:
        def enqueue_task(self, *, task_id, session_id, machine_id, backend, action, payload):
            captured["payload"] = payload

        def claim_task(self, *a, **k):
            return True

    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: _FakeDB())

    objective = "You are being invoked as the Manager for a new objective. Objective: ship the thing."
    task = types.SimpleNamespace(id="task-boot", prompt=objective,
                                 metadata={"session_id": "mgr-remote"})
    orch._mesh_enqueue_task(task, "claude")

    sess_payload = captured["payload"]["session"]
    # The remote create_session reads THIS field as its first message — must be the objective.
    assert sess_payload["last_user_message"] == objective
    # (payload['prompt'] also carries it, but the create_session path ignores that.)
    assert captured["payload"]["prompt"] == objective


@pytest.mark.asyncio
async def test_invoke_manager_forked_assignment_points_at_source_session(monkeypatch):
    """A forked Manager's first assignment must name the source session and the
    read_session_history tool, so it can pull the FULL prior conversation beyond
    the bounded boot excerpt."""
    orch, cap = _wire_manager_orch(monkeypatch)
    await orch.invoke_manager(
        "ship X", repo_path="/x",
        continued_from="src-sess-9", continue_inline="You: hi\n\nAgent: done",
    )
    desc = cap["submit_kw"]["description"]
    assert "src-sess-9" in desc
    assert "read_session_history(session_id='src-sess-9')" in desc


@pytest.mark.asyncio
async def test_invoke_manager_no_fork_has_no_history_pointer(monkeypatch):
    orch, cap = _wire_manager_orch(monkeypatch)
    await orch.invoke_manager("ship X", repo_path="/x")
    assert "read_session_history" not in cap["submit_kw"]["description"]
