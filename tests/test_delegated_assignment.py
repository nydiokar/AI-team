"""Offline delivery through Manager MCP, managed carrier and Case inbox."""
import asyncio
import json
import multiprocessing
import os
import re
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from config import config
from scripts import mcp_jobs, mcp_manager
from src.control import control_api, db as db_module, node_registry, task_server
from tests.test_agent_inbox_delivery import _add_session, _drive, _env, _rows, _tick, _wakes
from tests.test_turn_queue_4b import _pass
from tests.test_turn_queue_producer1 import _submit


class Harness:
    auth = {"Authorization": "Bearer offline-token"}

    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.db, self.orchestrator = _env(tmp_path, monkeypatch)
        self.orchestrator.notifier = SimpleNamespace(notify_task_outcome=AsyncMock())
        monkeypatch.setattr(config.mesh, "enabled", True)
        monkeypatch.setattr(config.mesh, "delegated_assignments_enabled", True)
        monkeypatch.setattr(control_api, "_dashboard_token", lambda: "offline-token")
        monkeypatch.setattr(task_server, "_worker_token", lambda: "offline-token")
        monkeypatch.setattr(task_server, "get_db", lambda: self.db)
        monkeypatch.setenv("WORKER_NODE_ID", "worker-a")
        _add_session(self.db, "worker")
        self.case_id = self.db.open_case("Interpret four experiments", "sess-1", role="manager")
        self.gateway = TestClient(control_api.build_control_api(self.orchestrator))
        self.carrier = TestClient(task_server.app)
        monkeypatch.setattr(node_registry, "_registry", node_registry.NodeRegistry())
        self.post("/nodes/register", {
            "node_id": "worker-a", "tailscale_ip": "127.0.0.1", "api_port": 0, "incarnation_id": "inc-1",
            "capabilities": {"backends": ["claude"], "queue_protocols": [0, 1], "managed_backends": ["claude"]},
        })
        monkeypatch.setattr(mcp_manager, "_api_request", self.request)
        monkeypatch.setattr(mcp_jobs, "_post_job", self.job_request)
        self.requests = []
        self.manager = _submit(self.orchestrator, description="Delegate", source="manager_invoke", operation_id="manager")
        self.manager_claim, _ = self.start(self.manager)

    def request(self, method, path, payload=None, headers=None):
        self.requests.append((method, path))
        response = self.gateway.request(method, path, json=payload, headers={**self.auth, **(headers or {})})
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, path, payload, status=200):
        response = self.carrier.post(path, headers=self.auth, json=payload)
        assert response.status_code == status, response.text
        return response.json()

    def job_request(self, payload, path="/jobs"):
        return self.post(path, payload)

    def dispatch(self, *, case_id=None, manager="sess-1", objective="Run experiments", legacy=False, operation_id=None):
        self.monkeypatch.setenv("AI_TEAM_SESSION_ID", manager)
        args = {"case_id": case_id or self.case_id, "session_id": "worker", "objective": objective}
        if not legacy:
            args["expected_deliverable"] = "Findings and artifact locations"
            if operation_id:
                args["operation_id"] = operation_id
        reply = mcp_manager._dispatch_worker(args)
        self.monkeypatch.setenv("AI_TEAM_SESSION_ID", "worker")
        return reply.splitlines()[0].split()[-1]

    def start(self, task_id):
        _pass(self.db, self.orchestrator)
        grant = self.post(f"/tasks/{task_id}/claim-managed", {
            "node_id": "worker-a", "carrier_kind": "worker_daemon", "incarnation_id": "inc-1",
        })
        payload = grant["task"]["payload"]
        self.post(f"/tasks/{task_id}/start-managed", {
            "node_id": "worker-a", "claim_token": grant["claim_token"], "incarnation_id": "inc-1",
        })
        proof = re.search(r"Assignment tool turn_proof: ([a-f0-9]{64})", payload["prompt"])
        tool_args = {"turn_id": task_id, "turn_proof": proof[1]} if proof else {}
        if tool_args:
            assert task_id in payload["prompt"]
            assert tool_args["turn_proof"] not in self.db.get_task(task_id)["payload"]
            assert payload["session"]["last_user_message"] == payload["prompt"]
        return grant["claim_token"], tool_args

    def finish(self, task_id, token, *, status="completed", output="finished turn"):
        self.post(f"/tasks/{task_id}/result-managed", {
            "node_id": "worker-a", "claim_token": token, "success": status == "completed", "output": output,
        })

    def launch(self, tool_args, name="experiment"):
        return mcp_jobs._watch_job({
            **tool_args, "command": f"run-{name}", "label": name, "operation_id": name,
        }).splitlines()[0].split()[-1]

    def disposition(self, tool_args, disposition, **fields):
        return json.loads(mcp_jobs._assignment_disposition(self.report_args(tool_args, disposition, **fields)))

    def report_args(self, tool_args, disposition, **fields):
        return {"session_id": "worker", "node_id": "worker-a", **tool_args, "disposition": disposition,
                "summary": "Worker checkpoint or findings", **fields}

    def poll_receipts(self):
        return asyncio.run(self.orchestrator._poll_job_completions_once())

    def restart_controller_db(self):
        path = self.db._path
        self.db.close()
        self.db = db_module.MeshDB(str(path))
        self.monkeypatch.setattr(db_module, "get_db", lambda: self.db)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def test_staged_controller_refuses_new_assignments_and_drains_existing_work(harness, monkeypatch):
    h = harness
    assignment_id = h.dispatch()
    monkeypatch.setattr(config.mesh, "delegated_assignments_enabled", False)
    response = h.gateway.post("/api/instructions", headers={**h.auth, "X-AI-Team-Principal": "automation"}, json={
        "description": "New assignment", "expected_deliverable": "Findings", "case_id": h.case_id,
        "session_id": "worker", "requester_session_id": "sess-1",
    })
    assert response.status_code == 503
    assert response.json()["detail"] == "delegated_assignment_admission_disabled"
    assert [row["assignment_id"] for row in _rows(h.db, "SELECT assignment_id FROM delegated_assignments")] == [assignment_id]
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(assignment_id)
    h.disposition(tool_args, "completed", evidence=["artifact"])
    h.finish(assignment_id, token)
    _drive(h.db, h.orchestrator)
    (wake,) = _wakes(h.db)
    assert "Assignment report" in wake["prompt"]
    assert h.dispatch(legacy=True)


def test_four_detached_jobs_deliver_one_explicit_assignment_report(harness):
    h = harness
    assignment_id = h.dispatch()
    case_read = h.request("GET", f"/api/cases/{h.case_id}/brief")
    assert case_read["brief"]["assignments"][0]["assignment_id"] == assignment_id
    h.finish(h.manager, h.manager_claim)

    worker_turn = assignment_id
    previous_job = None
    for index, exit_code in enumerate((0, 7, 0, 0), start=1):
        token, tool_args = h.start(worker_turn)
        if previous_job:
            result = json.loads(mcp_jobs._job_result({**tool_args, "job_id": previous_job}))
            assert result["exit_code"] == (7 if index == 3 else 0)
            if index == 2:
                assert not result["tail"]
        job_id = h.launch(tool_args, f"experiment-{index}")
        assert h.launch(tool_args, f"experiment-{index}") == job_id
        assert h.db.get_job(job_id)["assignment_id"] == assignment_id
        complete_job = {"node_id": "worker-a", "exit_code": exit_code,
                        "tail": "" if index == 1 else f"artifact-{index}"}
        # Fast exits and silent exits must stay attached and wait for the checkpoint.
        h.post(f"/jobs/{job_id}/done", complete_job)
        assert h.poll_receipts() == 0
        h.disposition(tool_args, "yield", job_id=job_id, summary=f"Continue after experiment {index}")
        h.finish(worker_turn, token, output=f"Launched {job_id}")
        _drive(h.db, h.orchestrator)
        assert all("Assignment report" not in wake["prompt"] for wake in _wakes(h.db))
        assert any(job_id in wake["prompt"] and "Assignment progress" in wake["prompt"]
                   for wake in _wakes(h.db))
        if index == 2:
            h.restart_controller_db()
        assert h.poll_receipts() == 1
        h.post(f"/jobs/{job_id}/done", complete_job)
        assert h.poll_receipts() == 0
        (continuation,) = _rows(h.db, "SELECT * FROM mesh_tasks WHERE idempotency_key = ?", f"watched:{job_id}")
        assert continuation["session_id"] == "worker"
        assert continuation["assignment_id"] == assignment_id
        assert continuation["sender_session_id"] == "sess-1"
        assert continuation["flow_run_id"] == h.case_id
        assert job_id in continuation["prompt"]
        if exit_code:
            assert "exit code 7" in continuation["prompt"]
        worker_turn = continuation["id"]
        previous_job = job_id

    token, tool_args = h.start(worker_turn)
    assert json.loads(mcp_jobs._job_result({**tool_args, "job_id": previous_job}))["exit_code"] == 0
    h.disposition(tool_args, "continue", summary="Compare artifacts before submitting")
    h.finish(worker_turn, token)
    assert _tick(h.orchestrator) == 1
    (analysis_turn,) = [w for w in _wakes(h.db) if w["assignment_id"] == assignment_id]
    assert analysis_turn["session_id"] == "worker"
    assert analysis_turn["sender_session_id"] == "sess-1"
    token, tool_args = h.start(analysis_turn["id"])
    fields = {"summary": "Experiment 2 failed, recovery confirmed the finding.",
              "evidence": ["artifacts/experiment-4.json"]}
    h.disposition(tool_args, "completed", **fields)
    h.disposition(tool_args, "completed", **fields)
    h.finish(analysis_turn["id"], token)
    h.restart_controller_db()
    h.disposition(tool_args, "completed", **fields)
    _drive(h.db, h.orchestrator)
    (final,) = [wake for wake in _wakes(h.db) if "Assignment report" in wake["prompt"]]
    assert final["session_id"] == "sess-1"
    assert fields["summary"] in final["prompt"]
    assert fields["evidence"][0] in final["prompt"]
    assert "record_review" in final["prompt"] and assignment_id in final["prompt"]
    assert not _rows(h.db, "SELECT * FROM agent_inbox WHERE kind IN ('watched_job', 'completion')")
    h.orchestrator.notifier.notify_task_outcome.assert_not_awaited()
    assert not _rows(h.db, "SELECT id FROM mesh_tasks WHERE turn_source IN ('human','operator')")
    assert h.requests == [("POST", "/api/instructions"), ("GET", f"/api/cases/{h.case_id}/brief")]

    h.monkeypatch.setenv("AI_TEAM_SESSION_ID", "sess-1")
    assert "REFUSED" in mcp_manager._close_case({"case_id": h.case_id, "continuation_plan": "Finish"})
    assert "Recorded" in mcp_manager._record_review({
        "case_id": h.case_id, "task_id": assignment_id, "verdict": "accepted", "reason": "Verified artifact",
    })
    assert "CLOSED" in mcp_manager._close_case({"case_id": h.case_id, "continuation_plan": "Finish"})


@pytest.mark.parametrize("outcome", ["failed", "decision_needed"])
def test_explicit_failure_and_decision_needed_have_actionable_report(harness, outcome):
    h = harness
    assignment_id = h.dispatch()
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(assignment_id)
    h.post("/assignments/disposition", h.report_args(tool_args, outcome), status=422)
    h.disposition(tool_args, outcome, next_action="Authorize access to the sealed reference data")
    h.finish(assignment_id, token)
    _drive(h.db, h.orchestrator)
    (report,) = _wakes(h.db)
    assert f"({outcome})" in report["prompt"]
    assert "Authorize access to the sealed reference data" in report["prompt"]
    premature_accept = h.gateway.post(f"/api/cases/{h.case_id}/review", headers=h.auth, json={
        "task_id": assignment_id, "verdict": "accepted",
    })
    assert premature_accept.status_code == 409
    assert "REFUSED" in mcp_manager._close_case({"case_id": h.case_id, "continuation_plan": "Resolve the blocker"})


def test_unresolved_turn_is_visible_and_resumes_same_worker_without_duplicate_job(harness):
    h = harness
    assignment_id = h.dispatch(operation_id="restart-safe")
    h.restart_controller_db()
    assert h.dispatch(operation_id="restart-safe") == assignment_id
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(assignment_id)
    job_id = h.launch(tool_args)
    h.finish(assignment_id, token)  # no typed disposition
    case = h.request("GET", f"/api/cases/{h.case_id}/brief")["brief"]
    assert case["assignments"][0]["state"] == "unresolved"
    assert case["assignments"][0]["issue"] == "missing_disposition"
    assert case["open_waits"] == [assignment_id]
    assert case["ready_waits"] == []
    h.post(f"/jobs/{job_id}/done", {"node_id": "worker-a", "exit_code": 0})
    assert h.poll_receipts() == 0
    _tick(h.orchestrator)
    (repair,) = [w for w in _wakes(h.db) if w["session_id"] == "worker"]
    token, repair_args = h.start(repair["id"])
    assert repair["assignment_id"] == assignment_id
    assert job_id in h.db.get_task(repair["id"])["payload"]
    assert h.launch(repair_args) == job_id
    h.disposition(repair_args, "yield", job_id=job_id)
    h.finish(repair["id"], token)
    assert h.poll_receipts() == 1


def test_reused_worker_session_isolated_by_origin_and_live_claim(harness):
    h = harness
    first = h.dispatch()
    _add_session(h.db, "manager-2")
    other_case = h.db.open_case("Separate assignment", "manager-2", role="manager")
    second = h.dispatch(case_id=other_case, manager="manager-2", objective="Separate job")
    h.finish(h.manager, h.manager_claim)

    first_token, first_args = h.start(first)
    first_job = h.launch(first_args)
    h.disposition(first_args, "yield", job_id=first_job)
    h.finish(first, first_token)

    second_token, second_args = h.start(second)
    h.post("/assignments/disposition", h.report_args(first_args, "completed", evidence=["old"]), status=409)
    h.post("/assignments/disposition", h.report_args({**second_args, "turn_proof": first_args["turn_proof"]},
                                                   "completed", evidence=["spoof"]), status=403)
    h.post("/assignments/disposition", h.report_args(second_args, "yield", job_id=first_job), status=409)
    h.post(f"/jobs/{first_job}/result", {"session_id": "worker", "node_id": "worker-a", **second_args}, status=403)
    second_job = h.launch(second_args)
    assert second_job != first_job
    h.disposition(second_args, "yield", job_id=second_job)
    h.finish(second, second_token)

    for job in (second_job, first_job):
        h.post(f"/jobs/{job}/done", {"node_id": "worker-a", "exit_code": 0})
        assert h.poll_receipts() == 1
    for job, assignment, owner, case in ((second_job, second, "manager-2", other_case),
                                         (first_job, first, "sess-1", h.case_id)):
        (turn,) = _rows(h.db, "SELECT * FROM mesh_tasks WHERE idempotency_key=?", f"watched:{job}")
        assert turn["assignment_id"] == assignment
        assert turn["flow_run_id"] == case
        assert turn["sender_session_id"] == owner
        token, tool_args = h.start(turn["id"])
        h.disposition(tool_args, "completed", evidence=[f"artifact-for-{job}"])
        h.finish(turn["id"], token)
    _drive(h.db, h.orchestrator)
    finals = [w for w in _wakes(h.db) if "Assignment report" in w["prompt"]]
    assert {w["session_id"] for w in finals} == {"sess-1", "manager-2"}
    assert all((first in w["prompt"]) == (w["session_id"] == "sess-1") for w in finals)


def test_report_and_inbox_intent_rollback_together_and_replay_after_commit(harness):
    h = harness
    assignment_id = h.dispatch()
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(assignment_id)
    report = h.report_args(tool_args, "completed", evidence=["artifact"])
    with h.db._write() as conn:
        conn.execute(
            "CREATE TRIGGER inject_report_failure BEFORE INSERT ON agent_inbox "
            "WHEN NEW.kind='assignment_report' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    h.post("/assignments/disposition", report, status=503)
    h.restart_controller_db()
    assert h.db.assignment_for_turn(assignment_id)["state"] == "active"
    assert not _rows(h.db, "SELECT * FROM assignment_dispositions")
    with h.db._write() as conn:
        conn.execute("DROP TRIGGER inject_report_failure")
    h.post("/assignments/disposition", report)
    h.restart_controller_db()
    h.post("/assignments/disposition", report)
    h.post("/assignments/disposition", {**report, "evidence": ["different"]}, status=409)
    h.finish(assignment_id, token)
    _drive(h.db, h.orchestrator)
    assert len([w for w in _wakes(h.db) if "Assignment report" in w["prompt"]]) == 1


def test_cancellation_fences_queued_wake_and_late_worker_report(harness):
    h = harness
    assignment_id = h.dispatch()
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(assignment_id)
    job_id = h.launch(tool_args)
    # Commit launch notice admission but leave its carrier unstarted.
    assert _tick(h.orchestrator) == 1
    (wake,) = _wakes(h.db)
    _pass(h.db, h.orchestrator)
    assert h.db.get_task(wake["id"])["status"] == "pending"
    assert "CLOSED" in mcp_manager._close_case({
        "case_id": h.case_id, "continuation_plan": "Cancel assignment", "outcome": "cancelled",
    })
    h.post("/assignments/disposition", h.report_args(tool_args, "yield", job_id=job_id), status=403)
    h.finish(assignment_id, token)
    h.post(f"/jobs/{job_id}/done", {"node_id": "worker-a", "exit_code": 0})
    assert h.poll_receipts() == 1
    assert h.db.get_task(wake["id"])["status"] == "cancelled"
    assert not _rows(h.db, "SELECT id FROM mesh_tasks WHERE turn_kind='watched_job'")
    assert h.db.assignment_for_turn(assignment_id)["state"] == "cancelled"


def test_worker_checkpoint_stays_bound_when_closed_worker_has_successor(harness):
    from tests.test_agent_inbox_delivery import _close

    h = harness
    assignment_id = h.dispatch()
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(assignment_id)
    h.disposition(tool_args, "continue", summary="Compare results")
    h.finish(assignment_id, token)
    _add_session(h.db, "successor")
    with h.db._write() as conn:
        conn.execute("UPDATE sessions SET continued_from='worker' WHERE session_id='successor'")
    _close(h.orchestrator, "worker")

    _drive(h.db, h.orchestrator)
    assert all(wake["session_id"] != "successor" for wake in _wakes(h.db))
    assert not h.db.pending_for("successor").messages
    (assignment,) = h.request("GET", f"/api/cases/{h.case_id}/brief")["brief"]["assignments"]
    assert (assignment["state"], assignment["issue"]) == ("unresolved", "worker_unavailable")
    (notice,) = _wakes(h.db)
    assert notice["session_id"] == "sess-1" and "worker_unavailable" in notice["prompt"]


def test_ordinary_single_turn_dispatch_keeps_existing_completion(harness):
    h = harness
    task_id = h.dispatch(legacy=True)
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(task_id)
    assert not tool_args
    h.finish(task_id, token)
    _drive(h.db, h.orchestrator)
    (wake,) = _wakes(h.db)
    assert "Finished since your last turn" in wake["prompt"]
    assert task_id in wake["prompt"]
    assert not h.db.assignment_for_turn(task_id)


def test_launch_and_job_admission_recover_at_commit_boundaries(harness):
    h = harness
    assignment_id = h.dispatch()
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(assignment_id)
    launch = {"command": "run", "label": "run", "session_id": "worker", "node_id": "worker-a",
              "operation_id": "recoverable", "notify_agent": True, **tool_args}
    with h.db._write() as conn:
        conn.execute(
            "CREATE TRIGGER inject_launch_failure BEFORE INSERT ON agent_inbox "
            "WHEN NEW.kind='assignment_progress' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    h.post("/jobs", launch, status=503)
    assert not _rows(h.db, "SELECT id FROM jobs")
    with h.db._write() as conn:
        conn.execute("DROP TRIGGER inject_launch_failure")
    job_id = h.post("/jobs", launch)["job_id"]
    h.restart_controller_db()
    assert h.post("/jobs", launch)["job_id"] == job_id
    h.disposition(tool_args, "yield", job_id=job_id)
    h.finish(assignment_id, token)
    h.post(f"/jobs/{job_id}/done", {"node_id": "worker-a", "exit_code": 0})
    with h.db._write() as conn:
        conn.execute(
            "CREATE TRIGGER inject_admission_failure BEFORE UPDATE OF assignment_result_turn_id ON jobs "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    assert h.poll_receipts() == 1
    assert h.db.get_job(job_id)["assignment_result_turn_id"] is None
    assert not _rows(h.db, "SELECT id FROM mesh_tasks WHERE idempotency_key=?", f"watched:{job_id}")
    assert h.db.assignment_for_turn(assignment_id)["state"] == "unresolved"
    assert h.db.assignment_for_turn(assignment_id)["issue"] == "job_continuation_unavailable"
    with h.db._write() as conn:
        conn.execute("DROP TRIGGER inject_admission_failure")
    h.monkeypatch.setenv("AI_TEAM_SESSION_ID", "sess-1")
    assert "pending" in mcp_manager._resume_assignment({"case_id": h.case_id, "assignment_id": assignment_id})
    h.monkeypatch.setenv("AI_TEAM_SESSION_ID", "worker")
    _tick(h.orchestrator)
    (repair,) = [w for w in _wakes(h.db) if w["session_id"] == "worker"]
    repair_token, repair_args = h.start(repair["id"])
    h.disposition(repair_args, "yield", job_id=job_id)
    h.finish(repair["id"], repair_token)
    with h.db._write() as conn:
        conn.execute(
            "CREATE TRIGGER inject_effect_ack_failure BEFORE UPDATE OF action_state ON job_completion_effects "
            "WHEN NEW.action_state='done' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    assert h.poll_receipts() == 1
    continuation = h.db.get_job(job_id)["assignment_result_turn_id"]
    assert continuation and h.db.get_task(continuation)
    assert h.db.get_job_completion(job_id)["action_state"] == "pending"
    h.restart_controller_db()
    with h.db._write() as conn:
        conn.execute("DROP TRIGGER inject_effect_ack_failure")
        conn.execute("UPDATE job_completion_effects SET lease_until=0 WHERE job_id=?", (job_id,))
    assert h.poll_receipts() == 1
    assert h.db.get_job_completion(job_id)["action_state"] == "done"
    assert _rows(h.db, "SELECT id FROM mesh_tasks WHERE idempotency_key=?", f"watched:{job_id}") == [{"id": continuation}]


def test_job_and_disposition_receivers_reject_bad_or_oversized_inputs(harness):
    h = harness
    for path in ("/jobs", "/assignments/disposition"):
        assert h.carrier.post(path, content=b"{").status_code == 401
        assert h.carrier.post(path, headers=h.auth, content=b"{").status_code == 422
        assert h.carrier.post(path, headers=h.auth, content=b"x" * (40 * 1024 + 1)).status_code == 413


def test_failed_worker_turn_recovers_on_original_assignment(harness):
    h = harness
    assignment_id = h.dispatch()
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(assignment_id)
    h.finish(assignment_id, token, status="failed", output="backend interrupted")
    assert h.db.assignment_for_turn(assignment_id)["issue"] == "worker_turn_failed"
    h.restart_controller_db()
    _tick(h.orchestrator)
    (retry,) = [w for w in _wakes(h.db) if w["session_id"] == "worker"]
    assert retry["assignment_id"] == assignment_id
    assert retry["sender_session_id"] == "sess-1"
    recovery_token, retry_args = h.start(retry["id"])
    h.post("/assignments/disposition", h.report_args(tool_args, "completed", evidence=["obsolete"]), status=403)
    h.disposition(retry_args, "completed", evidence=["recovered"])
    h.finish(retry["id"], recovery_token)
    _drive(h.db, h.orchestrator)
    assert len([w for w in _wakes(h.db) if "Assignment report" in w["prompt"]]) == 1


def test_report_retries_if_wake_admission_rolls_back(harness):
    h = harness
    assignment_id = h.dispatch()
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(assignment_id)
    h.disposition(tool_args, "completed", evidence=["verified artifact"])
    h.finish(assignment_id, token)
    with h.db._write() as conn:
        conn.execute(
            "CREATE TRIGGER inject_delivery_failure BEFORE UPDATE ON agent_inbox "
            "WHEN NEW.kind='assignment_report' AND NEW.state='delivered' "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    assert _tick(h.orchestrator) == 0
    assert _wakes(h.db) == []
    assert h.db.pending_for("sess-1").messages[0].state == "pending"
    h.restart_controller_db()
    with h.db._write() as conn:
        conn.execute("DROP TRIGGER inject_delivery_failure")
    _drive(h.db, h.orchestrator)
    (wake,) = _wakes(h.db)
    assert "verified artifact" in wake["prompt"]


def test_assignment_write_admission_holds_through_http_cancellation(harness, monkeypatch):
    h = harness
    held = threading.Event()
    entered = 0
    lock = threading.Lock()

    def blocking_write(_report):
        nonlocal entered
        with lock:
            entered += 1
        assert held.wait(8)
        return {"status": "accepted"}

    monkeypatch.setattr(task_server, "report_assignment", blocking_write)
    payload = {"turn_id": "t", "turn_proof": "proof", "node_id": "worker-a",
               "session_id": "worker", "disposition": "continue", "summary": "checkpoint"}
    path = "/assignments/disposition"

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=task_server.app),
                                     base_url="http://test") as client:
            active = [asyncio.create_task(client.post(path, headers=h.auth, json=payload)) for _ in range(4)]
            try:
                async with asyncio.timeout(4):
                    while entered < 4:
                        await asyncio.sleep(0.005)
                active[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await active[0]
                replies = await asyncio.gather(*(client.post(path, headers=h.auth, json=payload) for _ in range(96)))
                assert all(reply.status_code == 429 for reply in replies)
                assert entered == 4
            finally:
                held.set()
                await asyncio.gather(*active, return_exceptions=True)

    asyncio.run(run())


def _crashing_report_writer(path, request, *, before_commit):
    from src.control import agent_inbox, delegated_assignment

    db = db_module.MeshDB(path)
    enqueue = agent_inbox.enqueue_assignment

    def write_and_exit(*args, **kwargs):
        enqueue(*args, **kwargs)
        os._exit(71)

    if before_commit:
        agent_inbox.enqueue_assignment = write_and_exit
    db.record_assignment_disposition(delegated_assignment.Disposition.model_validate(request))
    os._exit(72)


def test_report_writer_process_death_before_and_after_commit(harness):
    h = harness
    assignment_id = h.dispatch()
    h.finish(h.manager, h.manager_claim)
    token, tool_args = h.start(assignment_id)
    report = h.report_args(tool_args, "completed", evidence=["crash-safe artifact"])
    context = multiprocessing.get_context("spawn")
    for before_commit, exit_code in ((True, 71), (False, 72)):
        writer = context.Process(
            target=_crashing_report_writer, args=(str(h.db._path), report),
            kwargs={"before_commit": before_commit},
        )
        writer.start()
        try:
            writer.join(8)
            assert writer.exitcode == exit_code
        finally:
            if writer.is_alive():
                writer.terminate()
                writer.join()
            writer.close()
        h.restart_controller_db()
        stored = _rows(h.db, "SELECT state,report FROM delegated_assignments WHERE assignment_id=?", assignment_id)
        notices = _rows(h.db, "SELECT message_id FROM agent_inbox WHERE kind='assignment_report'")
        if before_commit:
            assert stored == [{"state": "active", "report": None}]
            assert not notices
        else:
            assert stored[0]["state"] == "completed"
            assert json.loads(stored[0]["report"])["evidence"] == ["crash-safe artifact"]
            assert notices == [{"message_id": f"assignment_report:{assignment_id}"}]
    h.post("/assignments/disposition", report)
    h.finish(assignment_id, token)
    _drive(h.db, h.orchestrator)
    (wake,) = _wakes(h.db)
    assert "crash-safe artifact" in wake["prompt"]
