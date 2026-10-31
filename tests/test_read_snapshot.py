"""Read snapshot consistency, SQL cancellation and thread-local cleanup."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator

import pytest

from src.control.db import MeshDB
from src.control.jobs_read_model import list_controller_jobs


@pytest.fixture
def db(tmp_path) -> Iterator[MeshDB]:
    value = MeshDB(str(tmp_path / "snapshot.db"))
    yield value
    value.close()


def test_expensive_sql_is_interrupted_and_connection_recovers(db) -> None:
    original_timeout = db._conn().execute("PRAGMA busy_timeout").fetchone()[0]
    with pytest.raises(sqlite3.OperationalError, match="interrupted"), db.read_snapshot(budget_sec=.001):
        db._conn().execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n").fetchone()
    assert not db._conn().in_transaction
    assert db._conn().execute("PRAGMA busy_timeout").fetchone()[0] == original_timeout
    # A later managed write and read must not inherit the expired handler.
    db.register_job(job_id="after", node_id="n", label="after")
    assert list_controller_jobs(db)["running"][0]["id"] == "after"


def test_error_and_nested_scopes_release_without_clobbering_outer_snapshot(db) -> None:
    with db.read_snapshot():
        assert db._conn().in_transaction
        with db.read_snapshot():
            assert db._conn().execute("SELECT 1").fetchone()[0] == 1
        assert db._conn().in_transaction
    with pytest.raises(ValueError, match="failure"), db.read_snapshot():
        raise ValueError("failure")
    assert not db._conn().in_transaction


def test_jobs_arrays_share_snapshot_with_writer_before_second_select(db, monkeypatch) -> None:
    db.register_job(job_id="race", node_id="n", label="old")
    original = db.list_jobs
    calls = 0
    def read(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            writer = sqlite3.connect(str(db._path))
            try:
                writer.execute("UPDATE jobs SET status='done',label='new' WHERE id='race'")
                writer.commit()
            finally:
                writer.close()
        return original(**kwargs)
    monkeypatch.setattr(db, "list_jobs", read)
    result = list_controller_jobs(db)
    assert result["running"][0]["label"] == result["recent"][0]["label"] == "old"
    assert result["recent"][0]["status"] == "running"
    assert list_controller_jobs(db)["recent"][0]["label"] == "new"


def test_snapshot_handler_is_thread_local(db) -> None:
    ready, release = threading.Event(), threading.Event()
    errors: list[str] = []
    def other() -> None:
        try:
            with db.read_snapshot():
                ready.set()
                assert release.wait(5)
                assert db._conn().execute("SELECT 1").fetchone()[0] == 1
        except (sqlite3.Error, AssertionError) as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=other)
    thread.start()
    try:
        assert ready.wait(5)
        with pytest.raises(sqlite3.OperationalError), db.read_snapshot(budget_sec=0):
            db._conn().execute("SELECT 1").fetchone()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors



def test_session_http_page_and_evidence_share_snapshot(db, tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from src.control import control_api
    from src.control import db as db_module
    from src.core.interfaces import Session, SessionStatus
    from src.services.session_service import SessionService
    from src.services.session_store import SessionStore
    db.upsert_session(Session(session_id="owner", backend="claude", repo_path="/tmp",
                              status=SessionStatus.AWAITING_INPUT, created_at="1", updated_at="1"))
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    monkeypatch.setattr(control_api, "_db", lambda: db)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "snapshot-token")
    original = db.running_job_sessions
    committed = False
    def evidence(ids):
        nonlocal committed
        if not committed:
            with sqlite3.connect(str(db._path)) as writer:
                writer.execute("UPDATE sessions SET status='busy' WHERE session_id='owner'")
                writer.execute("INSERT INTO jobs(id,session_id,node_id,label,status,started_at,created_at,updated_at) VALUES ('new','owner','n','new','running','2','2','2')")
            committed = True
        return original(ids)
    monkeypatch.setattr(db, "running_job_sessions", evidence)
    orch = SimpleNamespace(session_service=SessionService(SessionStore()), quota_coordinator=None)
    with TestClient(control_api.build_control_api(orch)) as client:
        auth = {"Authorization": "Bearer snapshot-token"}
        first = client.get("/api/sessions", headers=auth).json()["sessions"][0]
        assert first["status"] == "awaiting_input" and first["reason"]["kind"] == "idle"
        second = client.get("/api/sessions", headers=auth).json()["sessions"][0]
        assert second["status"] == "busy" and second["reason"] is None
    assert committed and not db._conn().in_transaction


@pytest.mark.parametrize("path,error", [
    ("/api/jobs", "jobs_unavailable"), ("/api/sessions", "sessions_unavailable"),
    ("/api/work/affiliations/sessions?format=compact", "affiliations_unavailable"),
    ("/api/work/affiliations/sessions", "affiliations_unavailable"),
    ("/jobs/completions", "job_completion_unavailable"),
])
def test_budget_errors_are_structured_and_release_admission(db, monkeypatch, path, error) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from src.control import control_api, task_server
    from src.control import db as db_module
    from src.services.session_service import SessionService
    from src.services.session_store import SessionStore
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    monkeypatch.setattr(control_api, "_db", lambda: db)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "snapshot-token")
    monkeypatch.setattr(task_server, "get_db", lambda: db)
    monkeypatch.setattr(task_server, "_worker_token", lambda: "snapshot-token")
    original = db.read_snapshot
    monkeypatch.setattr(db, "read_snapshot", lambda: original(budget_sec=0))
    orch = SimpleNamespace(session_service=SessionService(SessionStore()), quota_coordinator=None)
    app = task_server.app if path.startswith("/jobs/") else control_api.build_control_api(orch)
    with TestClient(app) as client:
        for _ in range(6):
            response = client.get(path, headers={"Authorization": "Bearer snapshot-token"})
            assert response.status_code == 503 and response.json() == {"detail": error}
    assert not db._conn().in_transaction
