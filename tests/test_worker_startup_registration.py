"""Regression coverage for worker startup registration resilience."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import src.worker.agent as agent_mod


def _worker() -> agent_mod.WorkerAgent:
    worker = agent_mod.WorkerAgent.__new__(agent_mod.WorkerAgent)
    worker.cfg = SimpleNamespace(node_id="test-worker")
    worker._shutdown = asyncio.Event()
    return worker


def test_startup_registration_retries_without_exiting(monkeypatch) -> None:
    worker = _worker()
    attempts: list[int] = []
    delays: list[float] = []

    def register() -> None:
        attempts.append(1)
        if len(attempts) < 4:
            raise TimeoutError("gateway response timed out")

    async def run_sync(callable_) -> None:
        callable_()

    async def wait_for_retry(delay: float) -> None:
        delays.append(delay)

    worker._register = register
    worker._wait_for_registration_retry = wait_for_retry
    monkeypatch.setattr(agent_mod.asyncio, "to_thread", run_sync)

    asyncio.run(worker._register_until_success())

    assert len(attempts) == 4
    assert delays == [5.0, 10.0, 20.0]


def test_startup_registration_stops_retrying_when_shutting_down(monkeypatch) -> None:
    worker = _worker()
    attempts: list[int] = []

    def register() -> None:
        attempts.append(1)
        raise TimeoutError("gateway response timed out")

    async def run_sync(callable_) -> None:
        callable_()

    async def stop_worker(_delay: float) -> None:
        worker._shutdown.set()

    worker._register = register
    worker._wait_for_registration_retry = stop_worker
    monkeypatch.setattr(agent_mod.asyncio, "to_thread", run_sync)

    asyncio.run(worker._register_until_success())

    assert len(attempts) == 1


def test_registration_allows_for_gateway_side_sqlite_work() -> None:
    worker = _worker()
    worker.cfg = SimpleNamespace(
        node_id="test-worker",
        tailscale_ip="127.0.0.1",
        api_port=9001,
        backends=["claude"],
        max_concurrent=1,
        projects_root="",
        controller_url="http://controller:9002",
        list_repos=lambda: [],
    )
    worker._incarnation_id = "incarnation"
    posted: list[tuple[str, dict, int]] = []

    class _Http:
        def post(self, path: str, body: dict, timeout: int) -> None:
            posted.append((path, body, timeout))

    worker._http = _Http()

    worker._register()

    assert posted[0][0] == "/nodes/register"
    assert posted[0][2] == agent_mod._REGISTRATION_TIMEOUT_SECONDS == 30
