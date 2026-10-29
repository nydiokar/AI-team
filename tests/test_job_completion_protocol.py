"""Completion federation protocol: in-process HTTP, bounded input/output, real SQLite."""
from __future__ import annotations

import io
import json
import sqlite3
import threading
import urllib.error
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from config import config
from src.control import db as db_module
from src.control import task_server
from src.control.db import MeshDB
from src.control.task_server_client import TaskServerClient
from tests.test_job_completion_reliability import _register
from tests.test_turn_queue_4d import _env
from tests.test_turn_queue_producer1 import _flags, _no_cli_spawn  # noqa: F401


@pytest.fixture
def api(tmp_path, monkeypatch) -> Iterator[tuple[MeshDB, TestClient]]:
    db = MeshDB(str(tmp_path / "remote.db"))
    monkeypatch.setattr(task_server, "get_db", lambda: db)
    monkeypatch.setattr(task_server, "_worker_token", lambda: "completion-test")
    yield db, TestClient(task_server.app)
    db.close()


def test_completion_api_bounds_auth_failure_and_serialization_gate(api, monkeypatch) -> None:
    db, client = api
    auth = {"Authorization": "Bearer completion-test"}
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/jobs/completions"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/JobCompletionPage",
    }
    assert client.get("/jobs/completions").status_code in (401, 403)
    assert client.get("/jobs/completions", headers={"Authorization": "Bearer wrong"}).status_code == 401
    for query in ("limit=0", "limit=26", "after_id=x%20y", "after_id=" + "x" * 129):
        assert client.get("/jobs/completions?" + query, headers=auth).status_code == 422
    assert client.get("/jobs/completions", headers=auth).json() == {"jobs": [], "next_cursor": None}
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(task_server, "_JOB_COMPLETION_READ_SLOTS", slots)
    slots.acquire()
    assert client.get("/jobs/completions", headers=auth).status_code == 429
    slots.release()
    monkeypatch.setattr(task_server, "get_db", lambda: None)
    response = client.get("/jobs/completions", headers=auth)
    assert response.status_code == 503
    assert response.json() == {"detail": "job_completion_unavailable"}
    assert slots.acquire(blocking=False)
    slots.release()
    monkeypatch.setattr(task_server, "get_db", lambda: db)
    with monkeypatch.context() as backing_failure:
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("private db path")
        backing_failure.setattr(db, "job_completion_page", unavailable)
        response = client.get("/jobs/completions", headers=auth)
        assert response.status_code == 503
        assert response.json() == {"detail": "job_completion_unavailable"}
    _register(db, "small")
    db.complete_job("small", 0, "x" * 1000000)
    response = client.get("/jobs/completions", headers=auth)
    assert response.status_code == 200
    assert len(response.json()["jobs"][0]["tail"]) == 1500
    assert "command" not in response.json()["jobs"][0]
    # Permit must still be held while constructing the response body.
    original = task_server.JSONResponse.render

    def render(self, body) -> bytes:
        assert not slots.acquire(blocking=False)
        return original(self, body)

    monkeypatch.setattr(task_server.JSONResponse, "render", render)
    assert client.get("/jobs/completions", headers=auth).status_code == 200


@pytest.mark.asyncio
async def test_federation_more_than_50_old_starts_and_late_transition_behind_cursor(api, tmp_path, monkeypatch) -> None:
    remote_db, api_client = api
    local, orch = _env(tmp_path / "local", monkeypatch)
    monkeypatch.setattr(config.mesh, "jobs_read_mode", "federated")
    for i in range(130):
        _register(remote_db, f"j{i:03d}")
        remote_db.complete_job(f"j{i:03d}", 0)
    _register(remote_db, "a_late")
    with remote_db._write() as conn:
        conn.execute("UPDATE jobs SET started_epoch=1, updated_at='2000-01-01'")
    calls: list[str] = []

    def urlopen(request, timeout) -> io.BytesIO:
        calls.append(request.full_url)
        response = api_client.get(request.full_url.replace("http://remote", ""), headers={"Authorization": "Bearer completion-test"})
        assert response.status_code == 200
        return io.BytesIO(response.content)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    orch._remote_jobs_client = lambda: TaskServerClient("http://remote", "completion-test", timeout=2)
    orch.notifier.notify_task_outcome = AsyncMock()
    await orch._poll_job_completions_once()
    assert len(calls) == 4  # bounded per pass even with a larger backlog
    remote_db.complete_job("a_late", 0)  # before the saved cursor; later sweep must find it
    for _ in range(9):
        await orch._poll_job_completions_once()
    assert {c.args[0] for c in orch.notifier.notify_task_outcome.call_args_list} == {
        "a_late", *(f"j{i:03d}" for i in range(130)),
    }
    assert orch.notifier.notify_task_outcome.call_count == 131
    assert local._conn().execute("SELECT COUNT(*) FROM job_completion_effects WHERE notification_state='done' AND action_state='done'").fetchone()[0] == 131


@pytest.mark.parametrize("body", [b"{", b"x" * 1048577, json.dumps({"jobs": [], "next_cursor": "stuck"}).encode(),
                                      json.dumps({"jobs": [{"id": "x", "status": "garbage"}], "next_cursor": None}).encode()],
                         ids=["truncated", "oversized", "stuck", "invalid-status"])
def test_client_rejects_malformed_oversized_and_nonadvancing_pages(monkeypatch, body) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(body))
    with pytest.raises(ValueError):
        TaskServerClient("http://remote", "token").list_job_completions()


def test_old_remote_protocol_fails_explicitly(monkeypatch) -> None:
    def old(*args, **kwargs) -> None:
        raise urllib.error.HTTPError("http://remote/jobs/completions", 404, "old server", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", old)
    with pytest.raises(urllib.error.HTTPError):
        TaskServerClient("http://remote", "token").list_job_completions()


@pytest.mark.asyncio
async def test_remote_outage_does_not_suppress_already_durable_effects(tmp_path, monkeypatch) -> None:
    from tests.test_turn_queue_4d import _job

    db, orch = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(config.mesh, "jobs_read_mode", "federated")
    db.stage_job_completion(_job(notify_agent=0), "remote")

    def unavailable(cursor) -> None:
        raise urllib.error.HTTPError("http://remote/jobs/completions", 404, "old server", {}, None)

    orch._remote_job_completion_page = unavailable
    await orch._poll_job_completions_once()
    assert orch.notifier.calls == ["job_x"]
    assert db.get_job_completion("job_x")["notification_state"] == "done"


def test_migration_47_skips_historical_terminals_enrolls_running_and_future(tmp_path, monkeypatch) -> None:
    path = tmp_path / "migration.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE seed_guard (id INTEGER)")  # do not seed the v47 test template
    conn.close()
    migrations = db_module._get_migrations
    with monkeypatch.context() as before:
        before.setattr(db_module, "_get_migrations", lambda: [(v, s) for v, s in migrations() if v < 47])
        before.setattr(db_module, "_CURRENT_VERSION", 46)
        old = MeshDB(str(path))
        _register(old, "historical")
        old.complete_job("historical", 0)
        _register(old, "running")
        old.close()
    upgraded = MeshDB(str(path))
    try:
        assert upgraded.get_job("historical")["completion_enrolled"] == 0
        assert upgraded.get_job("running")["completion_enrolled"] == 1
        upgraded.complete_job("running", 0)
        _register(upgraded, "future")
        upgraded.complete_job("future", 0)
        assert {j["id"] for j in upgraded.job_completion_page()["jobs"]} == {"running", "future"}
        upgraded.stage_local_job_completions()
        assert upgraded.get_job_completion("historical") is None
        assert upgraded.pending_job_completions() == ["future", "running"]
        plan = [r[3] for r in upgraded._conn().execute("EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE completion_enrolled=1 AND status IN ('done','failed','lost') AND (notify=1 OR notify_agent=1) AND id > '' ORDER BY id LIMIT 25")]
        assert any("idx_jobs_completions" in p for p in plan)
        assert not any("TEMP B-TREE" in p for p in plan)
    finally:
        upgraded.close()
