"""Tests for T3 — Watched Jobs.

All tests honour the test cost guard: no paid Claude/Codex CLI, trivial real
processes (echo, sleep) are used for watched jobs.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from config import config
from src.control.db import MeshDB, get_db


def _job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


def _mk_session(db: MeshDB, session_id: str) -> None:
    """Insert a minimal real session row so a job referencing it is genuinely
    'owned' (not orphaned) under the session-existence check in list_jobs."""
    with db._write() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, backend, repo_path, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, "claude", "/repo", "idle",
             "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"),
        )


# ---------------------------------------------------------------------------
# Schema & DB layer
# ---------------------------------------------------------------------------


def test_jobs_table_created(tmp_path):
    """Migration v4 creates the jobs table."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    row = db._conn().execute("SELECT COUNT(*) FROM jobs").fetchone()
    assert row[0] == 0


def test_register_and_get_job(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    jid = _job_id()
    db.register_job(
        job_id=jid,
        node_id="test-node",
        label="test-script",
        session_id="sess_001",
        command="echo hello",
        notify=True,
    )
    job = db.get_job(jid)
    assert job is not None
    assert job["status"] == "running"
    assert job["label"] == "test-script"
    assert job["node_id"] == "test-node"
    assert job["session_id"] == "sess_001"


def test_register_job_duplicate_is_idempotent(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    jid = _job_id()
    db.register_job(job_id=jid, node_id="n1", label="l1")
    db.register_job(job_id=jid, node_id="n2", label="l2")
    job = db.get_job(jid)
    assert job["node_id"] == "n1"  # first write wins


def test_start_and_complete_job(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    jid = _job_id()
    db.register_job(job_id=jid, node_id="n1", label="l1")
    db.start_job(
        jid,
        pid=12345,
        pgid=12345,
        log_path="/tmp/test.log",
        started_epoch=111.25,
        observed_command="sleep 30",
    )
    job = db.get_job(jid)
    assert job["pid"] == 12345
    assert job["pgid"] == 12345
    assert job["started_epoch"] == 111.25
    assert job["last_checked_at"] is not None
    assert job["last_seen_command"] == "sleep 30"
    assert job["last_seen_started_epoch"] == 111.25

    db.complete_job(jid, exit_code=0, tail="all good")
    job = db.get_job(jid)
    assert job["status"] == "done"
    assert job["exit_code"] == 0
    assert job["tail"] == "all good"
    assert job["finished_at"] is not None


def test_fail_job(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    jid = _job_id()
    db.register_job(job_id=jid, node_id="n1", label="l1")
    db.fail_job(jid, "exit code 1: something broke")
    job = db.get_job(jid)
    assert job["status"] == "failed"
    assert "something broke" in job["tail"]


def test_record_job_probe_updates_liveness_fields(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    jid = _job_id()
    db.register_job(job_id=jid, node_id="n1", label="l1")
    db.record_job_probe(
        jid,
        observed_command="python train.py",
        observed_started_epoch=222.5,
        probe_error="",
    )

    job = db.get_job(jid)
    assert job["last_checked_at"] is not None
    assert job["last_probe_error"] == ""
    assert job["last_seen_command"] == "python train.py"
    assert job["last_seen_started_epoch"] == 222.5


def test_list_jobs_filters(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    j1, j2, j3 = _job_id(), _job_id(), _job_id()
    db.register_job(job_id=j1, node_id="node-a", label="a")
    db.register_job(job_id=j2, node_id="node-a", label="b")
    db.register_job(job_id=j3, node_id="node-b", label="c")
    db.complete_job(j3, exit_code=0)

    assert len(db.list_jobs(node_id="node-a")) == 2
    assert len(db.list_jobs(node_id="node-b")) == 1
    assert len(db.list_jobs(status="running")) == 2
    assert len(db.list_jobs(status="done")) == 1


def test_list_jobs_filters_unowned_before_limit(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    _mk_session(db, "sess_1")  # genuinely-owned: the session exists
    for i in range(3):
        db.register_job(
            job_id=f"job_owned_{i}",
            node_id="node-a",
            label=f"owned {i}",
            session_id="sess_1",
        )
    db.register_job(
        job_id="job_unowned",
        node_id="node-a",
        label="unowned",
        session_id=None,
    )

    jobs = db.list_jobs(ownership="unowned", limit=1)

    assert [job["id"] for job in jobs] == ["job_unowned"]


def test_list_jobs_surfaces_orphaned_as_unowned(tmp_path):
    """A job whose session_id matches NO known session (e.g. registered against a
    native/backend UUID) must surface in the unowned System view — flagged
    orphaned — rather than vanish. A genuinely-owned job stays out."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    _mk_session(db, "sess_real")
    db.register_job(job_id="job_owned", node_id="n", label="owned",
                    session_id="sess_real")
    db.register_job(job_id="job_orphan", node_id="n", label="orphan",
                    session_id="native-uuid-does-not-exist")
    db.register_job(job_id="job_null", node_id="n", label="null", session_id=None)

    unowned_ids = {j["id"] for j in db.list_jobs(ownership="unowned", limit=20)}
    assert unowned_ids == {"job_orphan", "job_null"}
    assert "job_owned" not in unowned_ids

    by_id = {j["id"]: j for j in db.list_jobs(limit=20)}
    assert by_id["job_orphan"]["orphaned"] == 1
    assert by_id["job_owned"]["orphaned"] == 0
    assert by_id["job_null"]["orphaned"] == 0


def test_get_terminal_jobs_since(tmp_path):
    from src.control.db import _now
    db = MeshDB(str(tmp_path / "mesh.db"))
    jid = _job_id()
    db.register_job(job_id=jid, node_id="n1", label="l1")
    before = _now()
    time.sleep(0.01)
    db.complete_job(jid, exit_code=0)
    terminal = db.get_terminal_jobs_since(before)
    assert len(terminal) == 1
    assert terminal[0]["id"] == jid


def test_list_jobs_limits(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    for i in range(5):
        db.register_job(job_id=_job_id(), node_id="n1", label=f"l{i}")
    assert len(db.list_jobs(limit=3)) == 3


# ---------------------------------------------------------------------------
# Task server endpoints via FastAPI TestClient
# ---------------------------------------------------------------------------


def test_job_endpoint_functions(tmp_path):
    """Verify job CRUD endpoint functions without opening a real server."""

    # Point the DB singleton at a temp path
    from config import config as cfg
    cfg.mesh.db_path = str(tmp_path / "mesh_api.db")
    cfg.mesh.worker_token = "test-token-api"
    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old is not None:
        old.close()

    try:
        from fastapi import HTTPException
        from src.control.task_server import (
            JobDonePayload,
            JobProbePayload,
            JobStartPayload,
            RegisterJobPayload,
            _require_auth,
            get_job,
            list_jobs,
            record_job_probe,
            register_job,
            report_job_done,
            start_job,
        )
        from fastapi.security import HTTPAuthorizationCredentials

        # Register a job
        data = register_job(RegisterJobPayload(
            node_id="test-node",
            label="api-test",
            session_id="sess_api",
            command="echo ok",
            notify=True,
        ))
        assert data["status"] == "registered"
        jid = data["job_id"]
        assert jid.startswith("job_")

        # GET /jobs/{id}
        assert get_job(jid)["status"] == "running"

        # Start the job
        resp = start_job(jid, JobStartPayload(
            node_id="test-node",
            pid=8888,
            pgid=8888,
            started_epoch=333.75,
            observed_command="echo ok",
        ))
        assert resp["status"] == "started"

        resp = record_job_probe(jid, JobProbePayload(
            node_id="test-node",
            observed_command="echo ok",
            observed_started_epoch=333.75,
            probe_error="",
        ))
        assert resp["status"] == "recorded"

        # Report done
        resp = report_job_done(jid, JobDonePayload(
            node_id="test-node",
            exit_code=0,
            tail="api test passed",
        ))
        assert resp["status"] == "accepted"

        # Verify terminal state
        job = get_job(jid)
        assert job["status"] == "done"
        assert job["exit_code"] == 0

        # GET /jobs lists it
        assert any(j["id"] == jid for j in list_jobs(node_id="test-node"))
        with pytest.raises(HTTPException):
            list_jobs(session_id="sess_api", ownership="unowned")
        with pytest.raises(HTTPException):
            list_jobs(ownership="invalid")

        lost_data = register_job(RegisterJobPayload(
            node_id="test-node",
            label="lost-test",
            command="sleep 10",
        ))
        lost_id = lost_data["job_id"]
        resp = report_job_done(lost_id, JobDonePayload(
            node_id="test-node",
            exit_code=-1,
            status="lost",
            tail="pid start time changed",
        ))
        assert resp["status"] == "accepted"
        assert get_job(lost_id)["status"] == "lost"

        # Auth failure
        with pytest.raises(HTTPException) as exc:
            _require_auth(HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-token"))
        assert exc.value.status_code == 401

    finally:
        db_mod._db_instance = None
        if old is not None:
            old.close()
        db_mod._db_instance = old


# ---------------------------------------------------------------------------
# Job watcher helpers
# ---------------------------------------------------------------------------


def test_report_job_done_does_not_call_telegram_api(tmp_path, monkeypatch):
    """The task server records terminal jobs; the gateway routes notifications."""
    from config import config as cfg
    cfg.mesh.db_path = str(tmp_path / "mesh_api.db")
    cfg.mesh.worker_token = "test-token-api"
    cfg.telegram.bot_token = "test-bot-token"
    cfg.telegram.allowed_users = [123]
    cfg.telegram.notification_chat_id = None
    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old is not None:
        old.close()

    def _urlopen_should_not_run(*args, **kwargs):
        raise AssertionError("task server must not call Telegram directly")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen_should_not_run)

    try:
        from src.control.task_server import JobDonePayload, RegisterJobPayload, register_job, report_job_done

        data = register_job(RegisterJobPayload(
            node_id="test-node",
            label="api-test",
            command="echo ok",
            notify=True,
        ))
        resp = report_job_done(data["job_id"], JobDonePayload(
            node_id="test-node",
            exit_code=0,
            tail="api test passed",
        ))
        assert resp["status"] == "accepted"
    finally:
        db_mod._db_instance = None
        if old is not None:
            old.close()
        db_mod._db_instance = old


@pytest.mark.asyncio
async def test_gateway_terminal_job_notifies_session_and_agent(tmp_path, monkeypatch):
    import src.services.session_store as session_store_module
    from src.control import transcript
    from src.orchestrator import TaskOrchestrator
    from src.services.session_store import SessionStore

    state_dir = tmp_path / "state"
    sessions_dir = state_dir / "sessions"
    monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", sessions_dir, raising=False)
    monkeypatch.setattr(
        session_store_module,
        "_BINDINGS_FILE",
        state_dir / "telegram" / "active_bindings.json",
        raising=False,
    )

    store = SessionStore()
    session = store.create("codex", str(tmp_path), telegram_chat_id=100, owner_user_id=1)

    class _Notifier:
        def __init__(self):
            self.calls = []

        async def notify_task_outcome(self, task_id, result, *, session=None, chat_id=None, prefix=""):
            self.calls.append({
                "task_id": task_id,
                "output": result.output,
                "chat_id": chat_id,
                "session_id": session.session_id if session else None,
            })

    notifier = _Notifier()
    submitted = []

    async def _submit_instruction(description, **kwargs):
        submitted.append({"description": description, **kwargs})
        return "task_followup"

    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.session_store = store
    orch.notifier = notifier
    orch.submit_instruction = _submit_instruction

    await orch._process_terminal_job({
        "id": "job_test123",
        "session_id": session.session_id,
        "node_id": "worker-a",
        "label": "npm test",
        "status": "done",
        "exit_code": 0,
        "tail": "all tests passed",
        "notify": 1,
        "notify_agent": 1,
    })

    assert notifier.calls == [{
        "task_id": "job_test123",
        "output": "Watched job `npm test` done.\nExit code: `0`\nAgent continuation requested.\n\nLast log lines:\n```\nall tests passed\n```",
        "chat_id": 100,
        "session_id": session.session_id,
    }]
    assert submitted[0]["session_id"] == session.session_id
    assert submitted[0]["source"] == "watched_job"
    assert submitted[0]["extra_metadata"] == {"job_id": "job_test123", "source": "watched_job"}
    assert "all tests passed" in submitted[0]["description"]

    turns = transcript.get_transcript(tmp_path / "results", sessions_dir, session.session_id)
    assert turns is not None
    assert turns[-1]["task_id"] == "job_test123"
    assert turns[-1]["instruction"] == "Watched job finished: npm test"
    assert "all tests passed" in turns[-1]["result"]

    from src.control.db import get_db
    row = get_db().get_task("job_test123")
    saved = store.get(session.session_id)
    assert row["completed_at"] == saved.task_history[-1]["timestamp"]


def test_pid_alive_with_nonexistent_pid():
    from src.worker.agent import _pid_alive
    assert _pid_alive(999999999) is False


def test_orchestrator_lists_local_and_remote_jobs(monkeypatch):
    import src.control.db as db_mod
    from src.orchestrator import TaskOrchestrator

    class _FakeDB:
        def list_jobs(self, status=None, session_id=None, ownership=None, limit=20):
            if status == "running":
                return [{"id": "local-running", "status": "running"}]
            return [{"id": "local-done", "status": "done"}]

    class _FakeClient:
        def list_jobs(self, node_id=None, status=None, session_id=None, ownership=None, limit=20):
            if status == "running":
                return [{"id": "remote-running", "status": "running"}]
            return [
                {"id": "remote-done", "status": "done"},
                {"id": "local-done", "status": "done"},
            ]

    monkeypatch.setattr(db_mod, "get_db", lambda: _FakeDB())

    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch._remote_jobs_client = lambda: _FakeClient()

    jobs = orch.list_watched_jobs(limit=20)

    assert jobs["running"] == [
        {"id": "local-running", "status": "running"},
        {"id": "remote-running", "status": "running"},
    ]
    assert jobs["recent"] == [
        {"id": "local-done", "status": "done"},
        {"id": "remote-done", "status": "done"},
    ]


def test_orchestrator_filters_local_and_remote_jobs_by_session(monkeypatch):
    import src.control.db as db_mod
    from src.orchestrator import TaskOrchestrator

    calls = []

    class _FakeDB:
        def list_jobs(self, status=None, session_id=None, ownership=None, limit=20):
            calls.append(("local", status, session_id, ownership, limit))
            if session_id != "sess_jobs":
                return []
            if status == "running":
                return [{"id": "local-owned-running", "status": "running", "session_id": session_id}]
            return [{"id": "local-owned-done", "status": "done", "session_id": session_id}]

    class _FakeClient:
        def list_jobs(self, node_id=None, status=None, session_id=None, ownership=None, limit=20):
            calls.append(("remote", status, session_id, ownership, limit))
            if session_id != "sess_jobs":
                return []
            if status == "running":
                return [{"id": "remote-owned-running", "status": "running", "session_id": session_id}]
            return [{"id": "remote-owned-done", "status": "done", "session_id": session_id}]

    monkeypatch.setattr(db_mod, "get_db", lambda: _FakeDB())

    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch._remote_jobs_client = lambda: _FakeClient()

    jobs = orch.list_watched_jobs(limit=10, session_id="sess_jobs")

    assert [job["id"] for job in jobs["running"]] == [
        "local-owned-running",
        "remote-owned-running",
    ]
    assert [job["id"] for job in jobs["recent"]] == [
        "local-owned-done",
        "remote-owned-done",
    ]
    assert all(call[2] == "sess_jobs" for call in calls)


def test_orchestrator_filters_local_and_remote_jobs_by_unowned(monkeypatch):
    import src.control.db as db_mod
    from src.orchestrator import TaskOrchestrator

    calls = []

    class _FakeDB:
        def list_jobs(self, status=None, session_id=None, ownership=None, limit=20):
            calls.append(("local", status, session_id, ownership, limit))
            if ownership != "unowned":
                return []
            if status == "running":
                return [{"id": "local-unowned-running", "status": "running", "session_id": None}]
            return [{"id": "local-unowned-done", "status": "done", "session_id": None}]

    class _FakeClient:
        def list_jobs(self, node_id=None, status=None, session_id=None, ownership=None, limit=20):
            calls.append(("remote", status, session_id, ownership, limit))
            if ownership != "unowned":
                return []
            if status == "running":
                return [{"id": "remote-unowned-running", "status": "running", "session_id": None}]
            return [{"id": "remote-unowned-done", "status": "done", "session_id": None}]

    monkeypatch.setattr(db_mod, "get_db", lambda: _FakeDB())

    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch._remote_jobs_client = lambda: _FakeClient()

    jobs = orch.list_watched_jobs(limit=10, ownership="unowned")

    assert [job["id"] for job in jobs["running"]] == [
        "local-unowned-running",
        "remote-unowned-running",
    ]
    assert [job["id"] for job in jobs["recent"]] == [
        "local-unowned-done",
        "remote-unowned-done",
    ]
    assert all(call[3] == "unowned" for call in calls)


def test_orchestrator_returns_cached_remote_jobs_when_fetch_in_progress():
    from src.orchestrator import TaskOrchestrator

    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch._watched_jobs_cache_lock = threading.Lock()
    orch._watched_jobs_remote_cache = {
        (None, None, 20): (
            time.monotonic() - 100.0,
            {"running": [{"id": "stale-running"}], "recent": [{"id": "stale-done"}]},
        )
    }
    orch._watched_jobs_remote_cache_ttl_sec = 2.0
    assert orch._watched_jobs_cache_lock.acquire(blocking=False)
    try:
        assert orch._cached_remote_watched_jobs(
            limit=20,
            session_id=None,
            ownership=None,
        ) == {"running": [{"id": "stale-running"}], "recent": [{"id": "stale-done"}]}
    finally:
        orch._watched_jobs_cache_lock.release()


def test_orchestrator_filters_remote_terminal_jobs_once():
    from src.orchestrator import TaskOrchestrator

    class _FakeClient:
        def list_jobs(self, node_id=None, status=None, session_id=None, limit=20):
            return [
                {
                    "id": "job_new",
                    "status": "done",
                    "updated_at": "2026-06-30T09:00:00",
                    "started_epoch": 200.0,
                },
                {
                    "id": "job_old",
                    "status": "done",
                    "updated_at": "2026-06-30T09:00:00",
                    "started_epoch": 100.0,
                },
                {
                    "id": "job_running",
                    "status": "running",
                    "updated_at": "2026-06-30T12:00:00",
                    "started_epoch": 300.0,
                },
            ]

    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch._processed_terminal_jobs = {"job_seen"}
    orch._remote_job_poll_started_epoch = 150.0
    orch._remote_jobs_client = lambda: _FakeClient()

    jobs = orch._remote_terminal_jobs_since("2026-06-30T12:00:00")

    assert [job["id"] for job in jobs] == ["job_new"]


def test_process_identity_for_current_process():
    from src.worker.agent import _process_identity
    ident = _process_identity(os.getpid())
    assert ident["alive"] is True
    assert "error" in ident


def test_job_identity_mismatch_detects_reused_pid():
    from src.worker.agent import _job_identity_mismatch
    reason = _job_identity_mismatch(
        {"last_seen_started_epoch": 100.0, "last_seen_command": "sleep 30"},
        {"alive": True, "started_epoch": 200.0, "command": "python other.py"},
    )
    assert "start time changed" in reason


def test_job_identity_mismatch_accepts_matching_identity():
    from src.worker.agent import _job_identity_mismatch
    reason = _job_identity_mismatch(
        {"last_seen_started_epoch": 100.0, "last_seen_command": "sleep 30"},
        {"alive": True, "started_epoch": 100.5, "command": "sleep 30"},
    )
    assert reason == ""


def test_read_log_tail_with_missing_file():
    from src.worker.agent import _read_log_tail
    assert _read_log_tail("/nonexistent/path.log") == ""


def test_read_log_tail_returns_last_lines(tmp_path):
    from src.worker.agent import _read_log_tail
    log = tmp_path / "test.log"
    log.write_text("line1\nline2\nline3\nline4\nline5\n")
    tail = _read_log_tail(str(log), max_lines=2)
    assert "line4" in tail
    assert "line5" in tail
    assert "line1" not in tail
