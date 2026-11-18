"""Offline quota ingest: real HTTP validation, bounded work and cancellation."""
from __future__ import annotations

import asyncio
import sqlite3
import threading

import httpx
import pytest

from src.control import task_server as ts
from src.services import quota_window_coordinator as qwc


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ts, "_worker_token", lambda: "quota-test")
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ts.app), base_url="http://test",
        headers={"Authorization": "Bearer quota-test"},
    )


@pytest.mark.asyncio
async def test_quota_http_ingests_codex_without_provider_process(client, tmp_path, monkeypatch) -> None:
    from config import config

    monkeypatch.setattr(config.quota, "db_path", str(tmp_path / "quota.db"))
    async with client:
        response = await client.post("/telemetry/quota-observation", json={
            "node_id": "n", "provider": "codex",
            "limits": {"primary": {"used_percent": 36.0, "window_minutes": 300,
                                   "resets_at": 2000000000}},
        })
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    store = qwc.QuotaWindowStore(config.quota.db_path)
    states = store.status()["window_states"]
    assert next(s for s in states if s["bucket_id"] == "codex_five_hour")["used_percent"] == 36.0
    assert next(s for s in states if s["bucket_id"] == "codex_seven_day")["used_percent"] is None


@pytest.mark.asyncio
async def test_quota_http_bounds_and_validation(client) -> None:
    async with client:
        assert (await client.post("/telemetry/quota-observation", json={"node_id": "n"},
                                  headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.post("/telemetry/quota-observation", content=b"{")).status_code == 422
        assert (await client.post("/telemetry/quota-observation", content=b"x" * 16385)).status_code == 413

        async def chunks():
            yield b"x" * 9000
            yield b"x" * 9000

        assert (await client.post("/telemetry/quota-observation", content=chunks())).status_code == 413


@pytest.mark.asyncio
async def test_quota_http_dependency_failure_is_structured(client, monkeypatch) -> None:
    async def broken(**kwargs):
        raise sqlite3.OperationalError("offline")

    monkeypatch.setattr(qwc, "ingest_worker_quota_observation", broken)
    async with client:
        response = await client.post("/telemetry/quota-observation", json={"node_id": "n"})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_quota_slow_body_times_out_and_releases_capacity(client, monkeypatch) -> None:
    monkeypatch.setattr(ts, "_QUOTA_OBSERVATION_BODY_TIMEOUT_SEC", 0.02)
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(ts, "_QUOTA_OBSERVATION_SLOTS", slots)

    async def slow_body():
        yield b"{"
        await asyncio.sleep(5)

    async with client:
        response = await client.post("/telemetry/quota-observation", content=slow_body())
    assert response.status_code == 408
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.asyncio
async def test_quota_cancelled_waiters_keep_actual_work_bounded(client, monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(ts, "_QUOTA_OBSERVATION_SLOTS", slots)

    async def blocked(**kwargs):
        started.set()
        assert release.wait(5)
        return {"accepted": True}

    monkeypatch.setattr(qwc, "ingest_worker_quota_observation", blocked)
    async with client:
        request = asyncio.create_task(client.post("/telemetry/quota-observation", json={"node_id": "n"}))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            responses = await asyncio.gather(*[
                client.post("/telemetry/quota-observation", json={"node_id": "n"})
                for _ in range(100)
            ])
            assert all(response.status_code == 429 for response in responses)
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
        async with asyncio.timeout(3):
            while not slots.acquire(blocking=False):
                await asyncio.sleep(0.01)
        slots.release()
