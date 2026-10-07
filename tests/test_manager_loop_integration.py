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
from tests.stage8a_legacy import unenrolled

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
    # [A82 Stage 8a] Sessions are born managed (their turns go to a managed
    # carrier). This harness proves the Case loop on the IN-PROCESS executor, so
    # each created session is modelled as operator-unenrolled (the legacy branch
    # that remains until Stage 8b). The managed Manager loop: PC01 + producers.
    real_create = o.session_service.create_session

    def _legacy_born(**kw):
        res = real_create(**kw)
        if getattr(res, "ok", False):
            unenrolled(get_db(), res.session.session_id)
        return res

    o.session_service.create_session = _legacy_born
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


# ---------------------------------------------------------------------------
# test_manager_loop_end_to_end, test_failed_worker_leaves_case_open_for_rework
# and test_session_based_worker_joins_case_as_worker RETIRED at the A82
# Stage-8b convergence cutoff. They proved the Manager Case loop on the LEGACY
# in-process executor (the _Worker harness drives _task_worker over the
# in-memory queue) by modelling each session as operator-UNENROLLED — exactly
# the legacy session-execution branch deleted here. There is no in-process
# managed-carrier harness to re-home them onto; the managed Manager-invoke turn
# is covered by tests/test_turn_queue_precutover.py::test_PC01_* and the live
# multi-backend validation (.ai/dispatch/A82_MULTIBACKEND_VALIDATION.md).
# NOTE (hand-back): retiring these drops in-process manager-loop INTEGRATION
# coverage (boot->dispatch->review->close, rework, worker-joins-case). A managed
# in-process harness is recommended as A82 follow-up.
# ---------------------------------------------------------------------------

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
