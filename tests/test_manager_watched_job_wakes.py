"""A detached process exit uses the real Case inbox and managed wake queue."""
import asyncio
import json
import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from config import config
from scripts import mcp_manager
from src.control import control_api, db as db_module, task_server
from tests.test_agent_inbox_delivery import _add_session, _drive, _env, _tick, _wakes
from tests.test_turn_queue_4b import _pass, _run
from tests.test_turn_queue_producer1 import _submit


@pytest.fixture
def launched_job(tmp_path, monkeypatch):
    db, orchestrator = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(config.mesh, "enabled", True)
    _add_session(db, "job-worker")
    case_id = db.open_case("inspect the output artifact", "sess-1", role="manager")
    manager_turn = _submit(orchestrator, description="start", operation_id="manager")
    _pass(db, orchestrator)
    manager_token = _run(db, manager_turn)
    worker_turn = _submit(
        orchestrator, description="launch and return", session_id="job-worker",
        source="automation_session", join_case_id=case_id, operation_id="worker",
    )
    db.complete_turn(manager_turn, manager_token, {"success": True, "output": "dispatched"})
    _pass(db, orchestrator)
    worker_token = _run(db, worker_turn)
    monkeypatch.setattr(task_server, "get_db", lambda: db)
    job_id = task_server.register_job(task_server.RegisterJobPayload(
        node_id="worker-a", label="silent job", session_id="job-worker", notify_agent=False,
    ))["job_id"]
    assert db.get_job(job_id)["launch_case_id"] == case_id
    db.complete_turn(worker_turn, worker_token, {"success": True, "output": job_id})
    return db, orchestrator, case_id, worker_turn, job_id


def test_manager_wakes_for_silent_job_after_worker_returns(launched_job, monkeypatch):
    db, orchestrator, case_id, worker_turn, job_id = launched_job
    assert len(_drive(db, orchestrator)) == 1
    assert job_id not in _wakes(db)[0]["prompt"]

    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "test-token")
    monkeypatch.setenv("AI_TEAM_SESSION_ID", "sess-1")
    with TestClient(control_api.build_control_api(orchestrator)) as client:
        def request(method, path, payload=None):
            response = client.request(method, path, json=payload,
                                      headers={"Authorization": "Bearer test-token"})
            assert response.status_code == 200, response.text
            return response.json()

        monkeypatch.setattr(mcp_manager, "_api_request", request)
        args = {"case_id": case_id, "job_id": job_id}
        assert "watching" in mcp_manager._arm_wait_group(args).lower()
        assert job_id in mcp_manager._check_inbox({})
        assert _tick(orchestrator) == 0
        done = task_server.JobDonePayload(node_id="worker-a", exit_code=0, tail="")
        task_server.report_job_done(job_id, done)
        assert "done" in mcp_manager._check_inbox({})

        assert len(_drive(db, orchestrator)) == 1
        worker_wake, job_wake = _wakes(db)
        assert worker_turn in worker_wake["prompt"]
        assert job_wake["session_id"] == "sess-1"
        assert job_id in job_wake["prompt"]
        assert "Watched job" in job_wake["prompt"]
        assert worker_turn not in job_wake["prompt"]
        assert "record_review" not in job_wake["prompt"]
        assert json.loads(job_wake["result"])["success"] is True
        assert job_id not in mcp_manager._check_inbox({})
        task_server.report_job_done(job_id, done)
        assert "acked" in mcp_manager._arm_wait_group(args)
        assert _drive(db, orchestrator) == []


def test_late_subscription_uses_launch_case_and_current_manager(launched_job):
    db, orchestrator, case_id, _, job_id = launched_job
    _add_session(db, "new-manager")
    _add_session(db, "other-manager")
    other_case = db.open_case("unrelated", "other-manager", role="manager")
    db.create_flow_link(other_case, "session", "job-worker", "worker")
    assert db.watch_case_job(other_case, job_id, "other-manager")["reason"] == (
        "not_a_worker_job_launched_in_this_case"
    )
    assert db.watch_case_job(case_id, job_id, "other-manager")["reason"] == "not_case_manager"

    db.fail_job(job_id, "exit code 3")
    assert db.watch_case_job(case_id, job_id, "sess-1") == {"ok": True, "state": "pending"}
    db.create_flow_link(case_id, "session", "new-manager", "manager")
    assert db.watch_case_job(case_id, job_id, "sess-1")["reason"] == "not_case_manager"
    assert db.watch_case_job(case_id, job_id, "new-manager") == {"ok": True, "state": "pending"}
    assert _drive(db, orchestrator)
    (job_wake,) = [wake for wake in _wakes(db) if "Watched job" in wake["prompt"]]
    assert job_wake["session_id"] == "new-manager"
    assert f"{job_id} (failed)" in job_wake["prompt"]


def test_close_retires_running_watch_before_exit(launched_job):
    db, orchestrator, case_id, _, job_id = launched_job
    _drive(db, orchestrator)
    assert db.watch_case_job(case_id, job_id, "sess-1") == {"ok": True, "state": "watching"}
    assert db.inbox_waiting_for(["sess-1"]) == {"sess-1": True}
    assert db.close_case(case_id, force=True)
    db.fail_job(job_id, "identity lost", status="lost")
    assert _drive(db, orchestrator) == []
    assert db.inbox_waiting_for(["sess-1"]) == {"sess-1": False}
    row = db._conn().execute(
        "SELECT state, last_error FROM agent_inbox WHERE about_job_id = ?", (job_id,),
    ).fetchone()
    assert tuple(row) == ("dead", "case_closed")


def test_job_terminal_commit_failure_is_retryable(launched_job, monkeypatch):
    db, _, case_id, _, job_id = launched_job
    assert db.watch_case_job(case_id, job_id, "sess-1")["state"] == "watching"
    monkeypatch.setattr(task_server, "_worker_token", lambda: "worker-token")
    headers = {"Authorization": "Bearer worker-token"}
    path = f"/jobs/{job_id}/done"
    payload = {"node_id": "worker-a", "exit_code": 0, "tail": ""}
    client = TestClient(task_server.app)
    with db._write() as conn:
        conn.execute(
            "CREATE TRIGGER reject_terminal_test BEFORE UPDATE OF status ON jobs "
            "BEGIN SELECT RAISE(ABORT, 'injected job write failure'); END",
        )
    assert client.post(path, headers=headers, json=payload).status_code == 503
    assert db.get_job(job_id)["status"] == "running"
    assert db.pending_for("sess-1", case_id=case_id).watched_jobs()[0]["state"] == "watching"
    with db._write() as conn:
        conn.execute("DROP TRIGGER reject_terminal_test")
    assert client.post(path, headers=headers, json={**payload, "node_id": "wrong"}).status_code == 403
    assert client.post(path, headers=headers, json={**payload, "tail": "x" * 32768}).status_code == 413
    assert client.post(path, headers=headers, content=b"{").status_code == 422
    assert client.post(path, headers=headers, json=payload).status_code == 200
    assert db.get_job(job_id)["status"] == "done"
    assert db.pending_for("sess-1", case_id=case_id).watched_jobs()[0]["state"] == "pending"


def test_subscription_admission_stays_bounded_after_disconnect(launched_job, monkeypatch):
    db, orchestrator, case_id, _, job_id = launched_job
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "test-token")
    lock = threading.Lock()
    release = threading.Event()
    entered = 0

    def block(*_):
        nonlocal entered
        with lock:
            entered += 1
        assert release.wait(5)
        return {"ok": True, "state": "watching"}

    monkeypatch.setattr(db, "watch_case_job", block)
    app = control_api.build_control_api(orchestrator)
    path = f"/api/cases/{case_id}/job-watches/{job_id}?session_id=sess-1"

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post(path)).status_code == 401
            headers = {"Authorization": "Bearer test-token"}
            assert (await client.post(path, headers=headers, content=b"x")).status_code == 413
            assert (await client.post(path.replace("sess-1", "invalid.id"), headers=headers)).status_code == 422
            holders = [asyncio.create_task(client.post(path, headers=headers)) for _ in range(4)]
            try:
                async with asyncio.timeout(3):
                    while entered < 4:
                        await asyncio.sleep(0.005)
                holders[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await holders[0]
                others = await asyncio.gather(*(client.post(path, headers=headers) for _ in range(96)))
                assert all(response.status_code == 429 for response in others)
                assert entered == 4
            finally:
                release.set()
                await asyncio.gather(*holders, return_exceptions=True)

    asyncio.run(exercise())


def test_upgrade_leaves_old_jobs_without_inventing_a_case(tmp_path, historical_mesh_db):
    path = str(tmp_path / "controller-v49.db")
    old = historical_mesh_db(path, 49)
    with old._write() as conn:
        conn.execute(
            "INSERT INTO jobs(id,node_id,label,session_id,status,started_at,created_at,updated_at) "
            "VALUES ('old','worker-a','old job','job-worker','running','2026-10-11','2026-10-11','2026-10-11')"
        )
    old.close()
    upgraded = db_module.MeshDB(path)
    try:
        assert upgraded.get_job("old")["launch_case_id"] is None
        upgraded.complete_job("old", 0)
        assert upgraded.get_job("old")["status"] == "done"
        assert upgraded.inbox_ready_recipients() == []
    finally:
        upgraded.close()


def test_incomplete_trigger_does_not_advance_schema_version(tmp_path, monkeypatch):
    path = str(tmp_path / "incomplete.db")
    db_module.MeshDB(path).close()
    current = db_module._CURRENT_VERSION
    migrations = db_module._get_migrations()
    monkeypatch.setattr(db_module, "_get_migrations", lambda: migrations + [(current + 1, """
        CREATE TABLE rolled_back(id INTEGER);
        CREATE TRIGGER incomplete AFTER INSERT ON rolled_back BEGIN SELECT 1;
    """)])
    with pytest.raises(sqlite3.OperationalError):
        db_module.MeshDB(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == current
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'rolled_back'").fetchone() is None
    finally:
        conn.close()
