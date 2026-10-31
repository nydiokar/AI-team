"""Federated reads must distinguish a complete empty union from failed coverage."""
from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from config import config
from src.control import control_api
from src.control.db import get_db
from src.control.jobs_read_model import JobsUnavailableError
from src.control.task_server_client import TaskServerClient
from src.orchestrator import TaskOrchestrator


@pytest.fixture
def federation(monkeypatch):
    monkeypatch.setattr(config.mesh, "jobs_read_mode", "federated")
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    client = TaskServerClient("http://unused.invalid", "test-token")
    orch._remote_jobs_client = lambda: client
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "federation-token")
    return orch, client


def test_unreachable_remote_is_unavailable_not_empty(federation, monkeypatch) -> None:
    orch, _ = federation
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=OSError("private network detail")))
    with TestClient(control_api.build_control_api(orch)) as api:
        for _ in range(6):
            response = api.get("/api/jobs", headers={"Authorization": "Bearer federation-token"})
            assert response.status_code == 503
            assert response.json() == {"detail": "jobs_unavailable"}


def test_recent_failure_does_not_cache_partial_running_success(federation, monkeypatch) -> None:
    orch, client = federation
    fetch = Mock(side_effect=[{"jobs": [{"id": "running", "status": "running"}], "format": "verified-v1"}, None, {"jobs": [], "format": "verified-v1"}, {"jobs": [], "format": "verified-v1"}])
    monkeypatch.setattr(client, "_get", fetch)
    with pytest.raises(JobsUnavailableError):
        orch.list_watched_jobs()
    assert orch.list_watched_jobs() == {"running": [], "recent": []}
    assert fetch.call_count == 4


def test_local_sql_failure_does_not_claim_complete_remote_union(federation, monkeypatch) -> None:
    import sqlite3
    orch, client = federation
    monkeypatch.setattr(client, "_get", lambda *args: {"jobs": [], "format": "verified-v1"})
    monkeypatch.setattr(get_db(), "list_jobs", Mock(side_effect=sqlite3.OperationalError("offline")))
    with pytest.raises(JobsUnavailableError):
        orch.list_watched_jobs()


def test_expired_cache_with_busy_refresher_is_unavailable(federation) -> None:
    orch, _ = federation
    orch._watched_jobs_cache_lock = threading.Lock()
    orch._watched_jobs_remote_cache = {(None, None, 20): (time.monotonic()-100, {"running": [], "recent": []})}
    orch._watched_jobs_remote_cache_ttl_sec = 2
    with orch._watched_jobs_cache_lock, pytest.raises(JobsUnavailableError):
        orch.list_watched_jobs()


@pytest.mark.parametrize("body", [None, {}, [None], [{"id": "", "status": "done"}],
    {"format": "verified-v1", "jobs": [None]},
    {"format": "verified-v1", "jobs": [{"id": "", "status": "done"}]},
    {"format": "verified-v1", "jobs": [{"id": "j", "status": "unknown"}]},
])
def test_reliable_jobs_rejects_bad_shape_legacy_default_stays_compatible(monkeypatch, body) -> None:
    client = TaskServerClient("http://unused.invalid", "test-token")
    monkeypatch.setattr(client, "_get", lambda *args: body)
    with pytest.raises(ValueError):
        client.list_jobs(reliable=True)
    assert client.list_jobs() == (body or [])


def test_healthy_empty_and_fresh_cache_are_success(federation, monkeypatch) -> None:
    orch, client = federation
    fetch = Mock(return_value={"jobs": [], "format": "verified-v1"})
    monkeypatch.setattr(client, "_get", fetch)
    assert orch.list_watched_jobs() == {"running": [], "recent": []}
    assert orch.list_watched_jobs() == {"running": [], "recent": []}
    assert fetch.call_count == 2



@pytest.mark.parametrize("format", ["legacy", "verified-v1"])
def test_task_server_jobs_failure_validation_and_serialization_gate(monkeypatch, format) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from src.control import task_server
    db = get_db()
    monkeypatch.setattr(task_server, "get_db", lambda: db)
    monkeypatch.setattr(task_server, "_worker_token", lambda: "federation-token")
    auth = {"Authorization": "Bearer federation-token"}
    slots = threading.BoundedSemaphore(4)
    monkeypatch.setattr(task_server, "_JOBS_LIST_READ_SLOTS", slots)
    original = task_server.JSONResponse.render
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    count = 0
    def render(self, body):
        nonlocal count
        if isinstance(body, list) or isinstance(body, dict) and "jobs" in body:
            with lock:
                count += 1
                if count == 4:
                    entered.set()
            assert release.wait(10)
        return original(self, body)
    monkeypatch.setattr(task_server.JSONResponse, "render", render)
    with TestClient(task_server.app) as client:
        assert client.get("/jobs").status_code in (401, 403)
        for params in ({"limit": 0}, {"limit": 1001}, {"node_id": "x"*129}, {"session_id": "x"*129}, {"status": "garbage"}):
            assert client.get("/jobs", params=params, headers=auth).status_code == 422
        with ThreadPoolExecutor(max_workers=4) as pool:
            calls = [pool.submit(client.get, f"/jobs?format={format}", headers=auth) for _ in range(4)]
            try:
                assert entered.wait(5)
                for _ in range(96):
                    assert client.get(f"/jobs?format={format}", headers=auth).status_code == 429
            finally:
                release.set()
            assert [f.result().status_code for f in calls] == [200]*4
        monkeypatch.setattr(task_server, "get_db", lambda: None)
        for _ in range(6):
            result = client.get(f"/jobs?format={format}", headers=auth)
            assert result.status_code == 503 and result.json() == {"detail": "jobs_unavailable"}
        monkeypatch.setattr(task_server, "get_db", lambda: db)
        def fail(self, body):
            if isinstance(body, list) or isinstance(body, dict) and "jobs" in body:
                raise TypeError("private serialization error")
            return original(self, body)
        monkeypatch.setattr(task_server.JSONResponse, "render", fail)
        for _ in range(6):
            result = client.get(f"/jobs?format={format}", headers=auth)
            assert result.status_code == 503 and result.json() == {"detail": "jobs_unavailable"}
    for _ in range(4):
        assert slots.acquire(blocking=False)
    for _ in range(4):
        slots.release()



def test_old_server_array_is_not_accepted_as_verified_availability(monkeypatch) -> None:
    client = TaskServerClient("http://unused.invalid", "test-token")
    get = Mock(return_value=[])
    monkeypatch.setattr(client, "_get", get)
    with pytest.raises(ValueError, match="protocol"):
        client.list_jobs(reliable=True)
    assert get.call_args.args[1]["format"] == "verified-v1"



def test_federated_facade_client_server_round_trip_and_remote_recovery(federation, tmp_path, monkeypatch) -> None:
    from src.control import task_server
    from src.control.db import MeshDB
    orch, rpc = federation
    remote = MeshDB(str(tmp_path / "remote.db"))
    get_db().register_job(job_id="local-job", node_id="n", label="local")
    remote.register_job(job_id="remote-job", node_id="n", label="remote")
    monkeypatch.setattr(task_server, "get_db", lambda: remote)
    monkeypatch.setattr(task_server, "_worker_token", lambda: "test-token")
    with TestClient(task_server.app) as server:
        requests = []
        def get(path, params):
            requests.append(params.copy())
            response = server.get(path, params=params, headers=rpc._headers())
            return response.json() if response.status_code == 200 else None
        monkeypatch.setattr(rpc, "_get", get)
        try:
            result = orch.list_watched_jobs()
            assert {row["id"] for row in result["running"]} == {"local-job", "remote-job"}
            assert all(params["format"] == "verified-v1" for params in requests)
            monkeypatch.setattr(task_server, "get_db", lambda: None)
            orch._watched_jobs_remote_cache.clear()
            with pytest.raises(JobsUnavailableError):
                orch.list_watched_jobs()
            monkeypatch.setattr(task_server, "get_db", lambda: remote)
            assert {row["id"] for row in orch.list_watched_jobs()["recent"]} == {"local-job", "remote-job"}
        finally:
            remote.close()
