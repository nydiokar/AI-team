"""
A39 — M3 pre-live de-risk: the Manager loop end-to-end, in-process, NO paid CLI.

This is the *integration* proof the A38 unit suite deliberately is not. `test_manager_role.py`
stubs `open_case`, `submit_instruction`, `_api_request` and the whole backend — so it proves each
piece in isolation but NEVER that the real pieces wire together. That gap is exactly where A38's
adversarial pass found three loop-breaking bugs (wait-for-joined-worker, missing close path,
flag half-wiring). This harness closes it: a REAL `TaskOrchestrator`, a REAL `MeshDB`, the REAL
`mcp_manager` tool logic and the REAL `control_api` read/close handlers — with only the Claude
turn faked (a canned success). Both flags ON.

It drives the whole §6 Phase 3.1 loop and asserts the invariants that would ACTUALLY break live:

  operator objective
    → invoke_manager  (ONE Case, case_role="manager", completion_criteria persisted)
    → the Manager's OWN first turn runs and ATTACHES to its own Case  (branch B)
        · REVEAL: does the manager's own turn emit task.finished on the Case? (it does — so the
          Case timeline carries TWO task.finished events; wait_for_worker's task_id filter is
          therefore load-bearing under REAL conditions, not just the mocked one)
        · REVEAL: does processing that turn silently DEMOTE case_role manager→worker?
          (branch B calls _set_session_case_affiliation with no role; the fast-path must save it)
    → dispatch a worker into the SAME Case via join_case_id  (admission branch J)
        · REVEAL: does the JOIN birth a child Case? (it must NOT — flow_run count stays 1)
    → worker turn runs → task.finished on the Case; Case stays OPEN  (Task finished != Case completed)
    → the REAL wait_for_worker resolves the WORKER's task.finished off a real 2-event timeline
        · REVEAL: does it falsely resolve on a task_id that never finished? (it must not)
    → close_case REFUSES on unmet completion_criteria, then CLOSES when reconciled
        · REVEAL: does closing clear the manager session's durable Case affiliation?

Run: `pytest tests/test_manager_loop_integration.py -v`  (plain pytest — no live/paid backend).
"""
import asyncio
import re

import pytest
from fastapi.testclient import TestClient

import scripts.mcp_manager as mcp_manager
from config import config
from src.control import control_api
from src.control.db import get_db
from src.core.interfaces import ExecutionResult
from src.orchestrator import TaskOrchestrator

TOKEN = "test-tok"


def _ok_result(*_a, **_k) -> ExecutionResult:
    """A canned successful backend turn — stands in for the paid Claude CLI.

    Accepts any (session,)/(session,message)/(cwd,message) + telemetry kwargs shape:
    the orchestrator calls create_session / resume_session / run_oneoff through a
    thread wrapper, so a permissive signature covers all three.
    """
    return ExecutionResult(
        success=True,
        output="fake work done",
        backend_session_id="fake-bsid",
        return_code=0,
        files_modified=[],
        errors=[],
    )


def _fail_result(*_a, **_k) -> ExecutionResult:
    """A canned FAILED backend turn — the live Manager will hit failing workers often."""
    return ExecutionResult(
        success=False,
        output="worker hit an error",
        backend_session_id="fake-bsid",
        return_code=1,
        files_modified=[],
        errors=["boom"],
        error_class="tool_error",
    )


@pytest.fixture
def orch(tmp_path, monkeypatch):
    # Keep artifacts off the repo — mirror the sanctioned e2e setup (test_queue_persistence).
    for name in ("tasks", "results", "summaries", "logs"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config.system, f"{name}_dir", str(tmp_path / name), raising=False)

    # Both gates ON — the Manager Case machinery (attach / JOIN / timeline / close) lives
    # entirely behind these; OFF ⇒ byte-identical (covered by the A38 unit suite).
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")

    o = TaskOrchestrator()
    # Fake ONLY the backend turn. Everything else — admission, links, events, terminal
    # seam, close gating — is the real code path.
    for meth in ("create_session", "resume_session", "run_oneoff"):
        monkeypatch.setattr(o._backends["claude"], meth, _ok_result)
    # Repo-path validation (must live inside the configured workspace root) is not what
    # A39 exercises — bypass it so the harness is hermetic (mirrors test_control_api).
    o.session_service._repo_path_validator = lambda _p: None
    return o


@pytest.fixture
def client(orch, monkeypatch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    # The read/close handlers read the same isolated singleton the orchestrator writes to.
    monkeypatch.setattr(control_api, "_db", lambda: get_db())
    return TestClient(control_api.build_control_api(orch))


@pytest.fixture
def route_tools_through_client(client, monkeypatch):
    """Run the REAL mcp_manager tool functions against the REAL FastAPI handlers.

    The tools only GET (/api/flows/{id}, /api/work/{id}/timeline) and POST /api/cases/{id}/close
    — none of which touch the asyncio task queue — so routing them through TestClient is safe
    (unlike the enqueue paths, whose asyncio.Queue is bound to the test's own loop). This exercises
    tool logic → HTTP → handler → db/orchestrator end-to-end, faking nothing.
    """
    def _shim(method, path, payload=None, timeout=20.0):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        if method == "GET":
            r = client.get(path, headers=headers)
        elif method == "POST":
            r = client.post(path, headers=headers, json=(payload or {}))
        else:  # pragma: no cover
            raise RuntimeError(f"unexpected method {method}")
        # Mirror _api_request: any non-2xx (except the 200 refusal envelope) is a RuntimeError.
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} on {method} {path}: {r.text}")
        return r.json()

    monkeypatch.setattr(mcp_manager, "_api_request", _shim)
    return _shim


class _Worker:
    """Spin the REAL _task_worker coroutine on the test loop (its terminal seam — where
    task.finished is emitted — is precisely what A39 must prove), without start()'s
    embedded servers / file watcher / telegram."""

    def __init__(self, orch):
        self.orch = orch
        self._task = None

    async def __aenter__(self):
        self.orch.running = True
        self._task = asyncio.create_task(self.orch._task_worker("w0"))
        return self

    async def __aexit__(self, *exc):
        self.orch.running = False
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def wait_task(self, task_id, timeout=20.0):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while task_id not in self.orch.task_results:
            if loop.time() > deadline:
                raise AssertionError(
                    f"task {task_id!r} never finished; results={list(self.orch.task_results)}"
                )
            await asyncio.sleep(0.05)
        return self.orch.task_results[task_id]


def _timeline_events(client, case_id):
    r = client.get(f"/api/work/{case_id}/timeline", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    return r.json()["events"]


def _finished_entities(events):
    return sorted(e["entity_id"] for e in events if e["event_type"] == "task.finished")


@pytest.mark.asyncio
async def test_manager_loop_end_to_end(orch, client, route_tools_through_client):
    db = get_db()

    # ── STEP A · BOOT ─────────────────────────────────────────────────────────
    # Drive invoke_manager directly on the test loop (it enqueues → the asyncio.Queue
    # must live on THIS loop, so we can't route it through TestClient's portal loop).
    res = await orch.invoke_manager(
        "Sketch the review.* verdict emitter (M3.2) skeleton",
        repo_path=str(config.system.tasks_dir),  # any real dir; work is faked
        completion_criteria="tests green",
    )
    assert res["ok"] is True, res
    case_id = res["case_id"]
    mgr_session_id = res["session_id"]
    mgr_task_id = res["task_id"]

    # Exactly ONE Case, open, with the objective + checkable criterion persisted.
    runs = db.list_flow_runs()
    assert len(runs) == 1, f"expected ONE Case at boot, got {len(runs)}"
    case = db.get_flow_run(case_id)
    assert case["status"] is None                       # open
    assert case["current_stage"] == "objective_lock"
    assert case["completion_criteria"] == "tests green"

    # The manager session is durably affiliated as the manager.
    sess = orch.session_store.get(mgr_session_id)
    assert sess.current_case_id == case_id
    assert sess.case_role == "manager"

    # The manager's own first turn ATTACHED to its own Case (branch B) at enqueue time.
    events = _timeline_events(client, case_id)
    assert any(e["event_type"] == "flow.created" for e in events)
    attached = [e for e in events if e["event_type"] == "task.attached"]
    assert [e["entity_id"] for e in attached] == [mgr_task_id]

    async with _Worker(orch) as worker:
        # ── STEP B · MANAGER'S OWN TURN RUNS ──────────────────────────────────
        await worker.wait_task(mgr_task_id)

        # REVEAL: processing its own turn must NOT demote the manager to a worker.
        sess = orch.session_store.get(mgr_session_id)
        assert sess.case_role == "manager", "manager was silently demoted by its own turn"

        # The manager turn emits its OWN task.finished; the Case stays open.
        assert _finished_entities(_timeline_events(client, case_id)) == [mgr_task_id]
        assert db.get_flow_run(case_id)["status"] is None
        assert len(db.list_flow_runs()) == 1

        # ── STEP C · DISPATCH A WORKER INTO THE SAME CASE (branch J) ──────────
        # This is the exact seam the Manager's dispatch_worker(case_id=…) tool rides:
        # POST /api/instructions maps body.case_id → join_case_id. We call the seam
        # directly (it enqueues) rather than through the tool's HTTP portal loop.
        worker_task_id = await orch.submit_instruction(
            description="Do a bounded worker chore for the Case.",
            cwd=str(config.system.tasks_dir),
            source="manager_invoke_worker",
            join_case_id=case_id,
        )

        # REVEAL: the JOIN must NOT birth a child Case.
        assert len(db.list_flow_runs()) == 1, "worker JOIN wrongly birthed a child Case"
        task_links = db.list_flow_links(flow_run_id=case_id, entity_type="task", role="task")
        assert worker_task_id in [l["entity_id"] for l in task_links]
        worker_attach = [
            e for e in _timeline_events(client, case_id)
            if e["event_type"] == "task.attached" and e["entity_id"] == worker_task_id
        ]
        assert len(worker_attach) == 1

        # ── STEP D · WORKER RUNS · Case stays OPEN ────────────────────────────
        await worker.wait_task(worker_task_id)
        assert db.get_flow_run(case_id)["status"] is None, "worker completion wrongly closed the Case"

        # The Case timeline now carries TWO distinct task.finished events.
        assert _finished_entities(_timeline_events(client, case_id)) == sorted(
            [mgr_task_id, worker_task_id]
        )

        # ── STEP E · REAL wait_for_worker against the REAL 2-event timeline ───
        out = await asyncio.to_thread(
            mcp_manager._wait_for_worker,
            {"task_id": worker_task_id, "flow_run_id": case_id, "poll_interval": 0.05, "timeout": 5},
        )
        assert "DONE" in out and "task.finished" in out, out

        # REVEAL (the A38 filter fix, under real data): a task_id that never finished
        # must NOT resolve DONE just because SOME task.finished exists on the Case.
        out_ghost = await asyncio.to_thread(
            mcp_manager._wait_for_worker,
            {"task_id": "ghost-never-ran", "flow_run_id": case_id, "poll_interval": 0.05, "timeout": 1},
        )
        assert "DONE" not in out_ghost, out_ghost

    # ── STEP F · CLOSE — refuses, then closes ────────────────────────────────
    refused = await asyncio.to_thread(mcp_manager._close_case, {"case_id": case_id})
    assert "REFUSED" in refused and "completion_criteria" in refused, refused
    assert db.get_flow_run(case_id)["status"] is None  # still open after refusal

    # NOTE (A39 finding): the authoritative gate `_criterion_resolved` (db.py) accepts ONLY
    # {"status":"met"} / {"status":"waived","reason":...} — NOT the {"met":true} shape the
    # close_case tool schema advertised as its example. Using the wrong shape yields an
    # UNCLOSABLE Case (perpetual "completion_criteria not reconciled"). The tool example was
    # corrected to match; this asserts the canonical, working contract.
    closed = await asyncio.to_thread(
        mcp_manager._close_case,
        {"case_id": case_id, "criteria_reconciliation": [{"criterion": "tests green", "status": "met"}]},
    )
    assert "CLOSED" in closed, closed
    assert db.get_flow_run(case_id)["status"] == "closed"
    assert any(e["event_type"] == "flow.closed" for e in _timeline_events(client, case_id))

    # Closing cleared the manager session's durable Case affiliation (A37).
    sess = orch.session_store.get(mgr_session_id)
    assert sess.current_case_id is None
    assert sess.case_role is None

    # Idempotent: a second close is a no-op decision signal, not a duplicate close.
    again = await asyncio.to_thread(mcp_manager._close_case, {"case_id": case_id})
    assert "already terminal" in again, again


@pytest.mark.asyncio
async def test_failed_worker_leaves_case_open_for_rework(orch, client, route_tools_through_client, monkeypatch):
    """The live Manager will get failing workers. A failed worker turn must leave the Case
    OPEN (so the Manager can rework/redecide) and wait_for_worker must report ATTENTION —
    proving it read the WORKER's failed task.finished, not the manager's earlier success."""
    db = get_db()
    res = await orch.invoke_manager(
        "objective with a worker that fails",
        repo_path=str(config.system.tasks_dir),
        completion_criteria="tests green",
    )
    case_id, mgr_task_id = res["case_id"], res["task_id"]

    async with _Worker(orch) as worker:
        await worker.wait_task(mgr_task_id)  # manager's own turn: SUCCESS

        # Now make the one-off worker's turn FAIL (run_oneoff is the one-off worker path).
        monkeypatch.setattr(orch._backends["claude"], "run_oneoff", _fail_result)
        worker_task_id = await orch.submit_instruction(
            description="A worker chore that will fail.",
            cwd=str(config.system.tasks_dir),
            source="manager_invoke_worker",
            join_case_id=case_id,
        )
        result = await worker.wait_task(worker_task_id)
        assert result.success is False

        # Case stays OPEN — a failed task is NOT a Case close (Task finished != Case completed).
        assert db.get_flow_run(case_id)["status"] is None
        assert len(db.list_flow_runs()) == 1

        # The worker's task.finished carries outcome=failed; wait_for_worker must surface it.
        out = await asyncio.to_thread(
            mcp_manager._wait_for_worker,
            {"task_id": worker_task_id, "flow_run_id": case_id, "poll_interval": 0.05, "timeout": 5},
        )
        assert "ATTENTION" in out and "DONE" not in out, out
        assert "failed" in out.lower(), out

    # A failed worker task does NOT gate close (only open child flows / approvals / unmet
    # criteria do) — closing is the Manager's judgment. Documented here so the design point
    # is explicit: task failure is a decision input, not a hard close-gate.
    closed = await asyncio.to_thread(
        mcp_manager._close_case,
        {"case_id": case_id, "outcome": "cancelled",
         "criteria_reconciliation": [{"criterion": "tests green", "status": "waived", "reason": "worker failed; abandoning"}]},
    )
    assert "CLOSED" in closed, closed
    assert db.get_flow_run(case_id)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_session_based_worker_joins_case_as_worker(orch, client, route_tools_through_client):
    """dispatch_worker may reuse an existing worker SESSION (cheaper — reuses orientation).
    That session must JOIN the Manager's Case as role=worker, with no child Case born —
    a distinct admission branch from the one-off worker in the main test."""
    db = get_db()
    res = await orch.invoke_manager(
        "objective with a session-based worker",
        repo_path=str(config.system.tasks_dir),
        completion_criteria="done",
    )
    case_id, mgr_task_id = res["case_id"], res["task_id"]

    # A separate worker session (as the Manager's dispatch_worker(session_id=…) would reuse).
    from src.core.interfaces import SessionOrigin
    wr = orch.session_service.create_session(
        backend="claude", repo_path=str(config.system.tasks_dir),
        node_id="__local__", origin=SessionOrigin(channel="web", kind="user"), bind_chat=False,
    )
    worker_session_id = wr.session.session_id

    async with _Worker(orch) as worker:
        await worker.wait_task(mgr_task_id)
        worker_task_id = await orch.submit_instruction(
            description="Bounded chore in a reused worker session.",
            session_id=worker_session_id,
            cwd=str(config.system.tasks_dir),
            source="manager_invoke_worker",
            join_case_id=case_id,
        )
        await worker.wait_task(worker_task_id)

    # Still ONE Case (no child), and the worker session is affiliated as a worker.
    assert len(db.list_flow_runs()) == 1
    assert db.get_flow_run(case_id)["status"] is None
    wsess = orch.session_store.get(worker_session_id)
    assert wsess.current_case_id == case_id
    assert wsess.case_role == "worker"
    # The manager session keeps its role — two sessions, two roles, one Case.
    assert orch.session_store.get(res["session_id"]).case_role == "manager"


@pytest.mark.asyncio
async def test_manager_invoke_disabled_is_refused(orch, client, monkeypatch):
    """Negative control: with the role gate OFF, the whole surface is inert (409)."""
    monkeypatch.delenv("MANAGER_ROLE_ENABLED", raising=False)
    r = client.post(
        "/api/manager",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"objective": "x", "repo_path": str(config.system.tasks_dir)},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "manager_role_disabled"
