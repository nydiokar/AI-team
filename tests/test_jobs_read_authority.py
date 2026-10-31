"""Controller jobs authority, real SQL budget and bounded HTTP failures (offline)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from config import config
from config.settings import Config
from src.control import control_api
from src.control.db import get_db
from src.orchestrator import TaskOrchestrator

TOKEN = "jobs-read-test"


@pytest.fixture
def controller(monkeypatch) -> TaskOrchestrator:
    monkeypatch.setattr(config.mesh, "jobs_read_mode", "controller", raising=False)
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.session_store = SimpleNamespace(get=Mock(side_effect=AssertionError("session load")))
    orch._cached_remote_watched_jobs = Mock(side_effect=AssertionError("remote read"))
    return orch


@pytest.mark.parametrize("count", [0, 1, 20, 50])
def test_controller_two_selects_no_http_or_session_loads(controller, monkeypatch, count) -> None:
    db = get_db()
    monkeypatch.setenv("CONTROLLER_URL", "http://unreachable.invalid:9002")
    monkeypatch.setattr(db, "get_session", Mock(side_effect=AssertionError("session SQL")))
    for i in range(count):
        db.register_job(job_id=f"j{i}", node_id="n", label="job", session_id=f"missing{i}")
    statements: list[str] = []
    db._conn().set_trace_callback(statements.append)
    try:
        result = controller.list_watched_jobs(limit=50)
    finally:
        db._conn().set_trace_callback(None)
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 2
    assert len(result["running"]) == len(result["recent"]) == count
    assert result["running"] == result["recent"]
    assert all(j["orphaned"] == 1 for j in result["running"])
    assert all("completion_enrolled" not in j for j in result["recent"])
    controller.session_store.get.assert_not_called()
    controller._cached_remote_watched_jobs.assert_not_called()


def test_controller_ownership_filter_and_full_response_contract(controller, monkeypatch) -> None:
    db = get_db()
    with db._write() as conn:
        conn.execute("INSERT INTO sessions (session_id,backend,repo_path,status,created_at,updated_at) "
                     "VALUES ('owner','claude','/repo','closed','1','1')")
    owners = [None, "", "  ", "missing", "json-only", "owner"]
    for i, sid in enumerate(owners):
        db.register_job(job_id=f"j{i}", node_id="n", label="job", session_id=sid)
        with db._write() as conn:
            conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (str(i), f"j{i}"))
    db.complete_job("j3", exit_code=0, tail="done")
    # A JSON-only session would be reachable in compatibility mode. It must not
    # override the controller database's ownership, nor require a file load.
    controller.session_store.get = Mock(return_value=object())
    actual = controller.list_watched_jobs(limit=50)
    assert actual == {"running": db.list_jobs(status="running", limit=50),
                      "recent": db.list_jobs(limit=50)}
    assert {j["id"]: j["orphaned"] for j in actual["recent"]} == {
        "j0": 0, "j1": 1, "j2": 1, "j3": 1, "j4": 1, "j5": 0,
    }
    # Newer owned job must not consume the unowned limit. recent includes done.
    assert [j["id"] for j in controller.list_watched_jobs(limit=2, ownership="unowned")["recent"]] == ["j4", "j3"]
    scoped = controller.list_watched_jobs(session_id="owner")
    assert [j["id"] for j in scoped["running"]] == ["j5"]
    assert scoped["running"] == scoped["recent"]
    controller.session_store.get.assert_not_called()


@pytest.mark.parametrize("mode,orphaned", [("controller", 1), ("federated", 0)])
def test_json_only_owner_is_an_explicit_mode_difference(controller, monkeypatch, tmp_path, mode, orphaned) -> None:
    from src.services import session_store

    monkeypatch.setattr(config.mesh, "jobs_read_mode", mode)
    monkeypatch.setattr(session_store, "_SESSIONS_DIR", tmp_path)
    (tmp_path / "json-only.json").write_text(json.dumps({
        "session_id": "json-only", "backend": "claude", "repo_path": "/repo",
        "status": "idle", "created_at": "1", "updated_at": "1",
    }))
    controller.session_store = session_store.SessionStore()
    controller._cached_remote_watched_jobs = Mock(return_value={"running": [], "recent": []})
    get_db().register_job(job_id="json-job", node_id="n", label="job", session_id="json-only")
    result = controller.list_watched_jobs()
    assert result["running"][0]["orphaned"] == orphaned
    assert result["recent"][0]["orphaned"] == orphaned
    unowned = controller.list_watched_jobs(ownership="unowned")
    assert len(unowned["running"]) == orphaned
    assert len(unowned["recent"]) == orphaned


@pytest.mark.parametrize("mode", ["controller", "federated"])
def test_startup_jobs_mode_is_explicit(monkeypatch, mode) -> None:
    monkeypatch.setenv("JOBS_READ_MODE", mode)
    monkeypatch.setenv("CONTROLLER_URL", "http://controller:9002")
    monkeypatch.setenv("WORKER_TOKEN", "token")
    assert Config().mesh.jobs_read_mode == mode


def test_default_controller_mode_does_not_follow_controller_url(monkeypatch) -> None:
    monkeypatch.delenv("JOBS_READ_MODE", raising=False)
    monkeypatch.setenv("CONTROLLER_URL", "http://remote.invalid:9002")
    assert Config().mesh.jobs_read_mode == "controller"


@pytest.mark.parametrize("mode,url,token", [("auto", "url", "t"), ("federated", "", "t"), ("federated", "url", "")])
def test_invalid_jobs_configuration_fails_startup(monkeypatch, mode, url, token) -> None:
    monkeypatch.setenv("JOBS_READ_MODE", mode)
    monkeypatch.setenv("CONTROLLER_URL", url)
    monkeypatch.setenv("WORKER_TOKEN", token)
    with pytest.raises(ValueError, match="JOBS_READ_MODE"):
        Config()


def _client(monkeypatch, orch) -> TestClient:
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(orch))


@pytest.mark.parametrize("facade", [False, True])
@pytest.mark.parametrize("failure", ["absent", "sqlite", "query"])
def test_jobs_api_unavailable_is_503(controller, monkeypatch, facade, failure) -> None:
    import src.control.db as db_mod

    client = _client(monkeypatch, controller if facade else SimpleNamespace())
    if failure == "absent":
        getter = lambda: None
    elif failure == "query":
        database = get_db()
        monkeypatch.setattr(database, "list_jobs", Mock(side_effect=sqlite3.OperationalError("query failed")))
        getter = lambda: database
    else:
        getter = Mock(side_effect=sqlite3.OperationalError("private database path"))
    monkeypatch.setattr(db_mod, "get_db", getter)
    monkeypatch.setattr(control_api, "_db", getter)
    response = client.get("/api/jobs", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 503
    assert response.json() == {"detail": "jobs_unavailable"}


@pytest.mark.parametrize("facade", [False, True])
def test_jobs_api_healthy_empty(controller, monkeypatch, facade) -> None:
    client = _client(monkeypatch, controller if facade else SimpleNamespace())
    response = client.get("/api/jobs", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {"running": [], "recent": []}


def test_federated_api_requires_union_facade(controller, monkeypatch) -> None:
    monkeypatch.setattr(config.mesh, "jobs_read_mode", "federated")
    client = _client(monkeypatch, SimpleNamespace())
    response = client.get("/api/jobs", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 503
    assert response.json() == {"detail": "jobs_unavailable"}


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "limit=x", "ownership=invalid", "session_id=" + "s" * 1025])
def test_jobs_api_rejects_malformed_bounds_before_read(controller, monkeypatch, query) -> None:
    client = _client(monkeypatch, controller)
    controller.list_watched_jobs = Mock(side_effect=AssertionError("must reject before read"))
    assert client.get(f"/api/jobs?{query}", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 422


def test_jobs_api_bound_holds_through_serialization_and_releases(controller, monkeypatch) -> None:
    from src.control.routes import monitoring

    client = _client(monkeypatch, controller)
    slots = threading.BoundedSemaphore(4)
    monkeypatch.setattr(monitoring, "_JOBS_READ_SLOTS", slots, raising=False)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    original = monitoring.JSONResponse.render

    def render(response, content) -> bytes:
        nonlocal active
        if content == {"running": [], "recent": []}:
            with lock:
                active += 1
                if active == 4:
                    entered.set()
            assert release.wait(10)
        return original(response, content)

    monkeypatch.setattr(monitoring.JSONResponse, "render", render)
    controller.list_watched_jobs = Mock(return_value={"running": [], "recent": []})
    with ThreadPoolExecutor(max_workers=4) as pool:
        requests = [pool.submit(client.get, "/api/jobs", headers={"Authorization": f"Bearer {TOKEN}"}) for _ in range(4)]
        try:
            assert entered.wait(10)
            for _ in range(96):
                response = client.get("/api/jobs", headers={"Authorization": f"Bearer {TOKEN}"})
                assert response.status_code == 429
                assert response.json() == {"detail": "jobs_busy"}
            assert controller.list_watched_jobs.call_count == 4
        finally:
            release.set()
        assert all(f.result().status_code == 200 for f in requests)
    monkeypatch.setattr(monitoring.JSONResponse, "render", original)
    controller.list_watched_jobs = Mock(side_effect=sqlite3.OperationalError("failed"))
    assert client.get("/api/jobs", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 503
    assert all(slots.acquire(blocking=False) for _ in range(4))
    for _ in range(4):
        slots.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["controller", "federated"])
async def test_completion_fetch_is_off_loop_and_uses_selected_source(controller, monkeypatch, mode) -> None:
    import src.control.db as db_mod

    monkeypatch.setattr(config.mesh, "jobs_read_mode", mode)
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    threads: list[int] = []
    calls: list[str] = []

    def fetch() -> int:
        threads.append(threading.get_ident())
        calls.append("local")
        entered.set()
        assert release.wait(10)
        return 0

    def get_database() -> SimpleNamespace:
        threads.append(threading.get_ident())
        return SimpleNamespace(stage_local_job_completions=fetch,
                               stage_job_completions=lambda *args: None,
                               pending_job_completions=lambda **kwargs: [])

    def remote(cursor: str) -> dict:
        threads.append(threading.get_ident())
        calls.append("remote")
        return {"jobs": [], "next_cursor": None}

    monkeypatch.setattr(db_mod, "get_db", get_database)
    controller._remote_job_completion_page = remote
    poll = asyncio.create_task(controller._poll_job_completions_once())
    try:
        async def heartbeat() -> None:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            await asyncio.sleep(0)

        await asyncio.wait_for(heartbeat(), timeout=2)
        assert not poll.done() and calls == ["local"]
        release.set()
        await asyncio.wait_for(poll, timeout=2)
        assert calls == (["local", "remote"] if mode == "federated" else ["local"])
        assert threads and all(t != loop_thread for t in threads)
    finally:
        release.set()
        if not poll.done():
            poll.cancel()
            with pytest.raises(asyncio.CancelledError):
                await poll


@pytest.mark.asyncio
async def test_completion_reader_obtains_a_thread_local_sqlite_connection(controller, monkeypatch) -> None:
    db = get_db()
    loop_connection = db._conn()
    db.register_job(job_id="terminal", node_id="n", label="job")
    db.complete_job("terminal", exit_code=0)
    original = db.stage_local_job_completions
    connections: list[sqlite3.Connection] = []

    def fetch() -> int:
        connections.append(db._conn())
        return original()

    monkeypatch.setattr(db, "stage_local_job_completions", fetch)
    monkeypatch.setattr(db, "pending_job_completions", lambda **kwargs: [])
    await controller._poll_job_completions_once()
    assert db.get_job_completion("terminal") is not None
    assert len(connections) == 1 and connections[0] is not loop_connection


@pytest.mark.asyncio
async def test_federated_completion_requires_durable_local_receipts(controller, monkeypatch) -> None:
    import src.control.db as db_mod

    monkeypatch.setattr(config.mesh, "jobs_read_mode", "federated")
    monkeypatch.setattr(db_mod, "get_db", lambda: None)
    controller._remote_job_completion_page = Mock()
    with pytest.raises(RuntimeError, match="receipt database unavailable"):
        await controller._poll_job_completions_once()
    controller._remote_job_completion_page.assert_not_called()
