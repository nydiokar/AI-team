"""Tests for T3 — Watched Jobs.

All tests honour the test cost guard: no paid Claude/Codex CLI, trivial real
processes (echo, sleep) are used for watched jobs.
"""

import json
import os
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


def test_pid_alive_with_nonexistent_pid():
    from src.worker.agent import _pid_alive
    assert _pid_alive(999999999) is False


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
