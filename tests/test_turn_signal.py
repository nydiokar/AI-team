"""A105: cross-process hints, shared subscribers and recovery; no network/CLI."""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.control import control_api
from src.control import turn_scheduler as ts


async def test_shared_signal_fans_out_and_coalesces_from_threads() -> None:
    with ts.subscribe_turn_changes() as first, ts.subscribe_turn_changes() as second:
        await asyncio.to_thread(lambda: [ts.notify_turn_queue_changed() for _ in range(1000)])
        await asyncio.wait_for(asyncio.gather(first.wait(30), second.wait(30)), 1)
        first.clear()
        second.clear()
        assert not await first.wait(0.01)
        assert not await second.wait(0.01)
    assert not ts._SUBSCRIBERS


async def test_signal_during_pass_is_retained_and_cancellation_unsubscribes() -> None:
    with ts.subscribe_turn_changes() as changed:
        changed.clear()  # before the DB pass, never after it
        ts.notify_turn_queue_changed()
        await asyncio.sleep(0)
        assert await changed.wait(0.1)
        changed.clear()
        assert not await changed.wait(0.01)
    assert not ts._SUBSCRIBERS


async def test_internal_signal_requires_auth_and_empty_body(monkeypatch) -> None:
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "signal-test")
    app = control_api.build_control_api(SimpleNamespace())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
        with ts.subscribe_turn_changes() as changed:
            assert (await client.post("/internal/turn-signal")).status_code == 401
            headers = {"Authorization": "Bearer signal-test"}
            assert (await client.post("/internal/turn-signal", headers=headers, content=b"garbage")).status_code == 413
            assert not await changed.wait(0.01)
            response = await client.post("/internal/turn-signal", headers=headers)
            assert response.status_code == 202
            assert await changed.wait(0.1)


async def test_sender_is_single_flight_and_retains_hint_during_send(monkeypatch) -> None:
    from src.control import turn_signal_transport as transport

    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[float] = []

    async def send(url: str, token: str) -> None:
        calls.append(asyncio.get_running_loop().time())
        entered.set()
        await release.wait()

    monkeypatch.setattr(transport, "_send_signal", send)
    task = asyncio.create_task(transport.forward_turn_changes("http://gateway:9003/internal/turn-signal", "secret"))
    try:
        await asyncio.sleep(0)
        for _ in range(1000):
            ts.notify_turn_queue_changed()
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(1000):
            ts.notify_turn_queue_changed()
        await asyncio.sleep(0.02)
        assert len(calls) == 1
        release.set()
        async with asyncio.timeout(1):
            while len(calls) < 2:
                await asyncio.sleep(0.01)
        assert calls[1] - calls[0] >= 0.1
        await asyncio.sleep(0.15)
        assert len(calls) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not ts._SUBSCRIBERS


async def test_sender_exception_does_not_log_request_credentials(monkeypatch, caplog) -> None:
    from src.control import turn_signal_transport as transport

    send = AsyncMock(side_effect=UnicodeError("credential-that-must-not-be-logged"))
    monkeypatch.setattr(transport, "_send_signal", send)
    caplog.set_level("DEBUG", logger=transport.__name__)
    task = asyncio.create_task(transport.forward_turn_changes("http://gateway:9003/internal/turn-signal", "secret"))
    try:
        await asyncio.sleep(0)
        ts.notify_turn_queue_changed()
        async with asyncio.timeout(1):
            while send.await_count < 1:
                await asyncio.sleep(0)
        assert "turn_signal_send_failed" in caplog.text
        assert "credential-that-must-not-be-logged" not in caplog.text
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("loop_name", ["_wake_dispatcher_loop", "_managed_effects_loop"])
async def test_consumers_react_before_poll_and_recover_without_signal(monkeypatch, loop_name) -> None:
    import src.control.db as db_mod
    from src.orchestrator import TaskOrchestrator

    calls = 0

    async def passed(*args) -> int:
        nonlocal calls
        calls += 1
        return 0

    obj = SimpleNamespace(running=True, _wake_dispatcher_tick_once=passed,
                          _drain_managed_turn_effects_once=passed)
    monkeypatch.setattr(db_mod, "get_db", lambda: SimpleNamespace(any_session_enrolled=lambda: True))
    task = asyncio.create_task(getattr(TaskOrchestrator, loop_name)(obj, 30))
    try:
        async with asyncio.timeout(1):
            while calls < 1:
                await asyncio.sleep(0.01)
        ts.notify_turn_queue_changed()
        async with asyncio.timeout(1):
            while calls < 2:
                await asyncio.sleep(0.01)
        assert calls == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not ts._SUBSCRIBERS

    calls = 0
    task = asyncio.create_task(getattr(TaskOrchestrator, loop_name)(obj, 0.02))
    try:
        async with asyncio.timeout(1):
            while calls < 2:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not ts._SUBSCRIBERS


async def test_signaled_effects_pass_checks_even_when_enrollment_is_empty(monkeypatch) -> None:
    import src.control.db as db_mod
    from src.orchestrator import TaskOrchestrator

    drain = AsyncMock(return_value=0)
    obj = SimpleNamespace(running=True, _drain_managed_turn_effects_once=drain)
    monkeypatch.setattr(db_mod, "get_db", lambda: SimpleNamespace(any_session_enrolled=lambda: False))
    task = asyncio.create_task(TaskOrchestrator._managed_effects_loop(obj, 30))
    try:
        async with asyncio.timeout(1):
            while drain.await_count < 1:
                await asyncio.sleep(0.01)
        ts.notify_turn_queue_changed()
        async with asyncio.timeout(1):
            while drain.await_count < 2:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_consumer_loops_use_shared_subscription_only() -> None:
    from src.orchestrator import TaskOrchestrator

    for method in (TaskOrchestrator._wake_dispatcher_loop, TaskOrchestrator._managed_effects_loop):
        source = inspect.getsource(method)
        assert "subscribe_turn_changes" in source
        assert "asyncio.Event(" not in source


@pytest.mark.parametrize("failure", ["connect", "timeout", "oversized", "refused", "cancel"])
async def test_http_sender_bounds_failures_and_closes_socket(monkeypatch, failure) -> None:
    from src.control import turn_signal_transport as transport

    writer = SimpleNamespace(write=lambda data: None, drain=AsyncMock(),
                             close=AsyncMock(), wait_closed=AsyncMock())
    # StreamWriter.close is synchronous.
    closed: list[bool] = []
    writer.close = lambda: closed.append(True)

    async def readline() -> bytes:
        if failure in ("timeout", "cancel"):
            await asyncio.Future()
        if failure == "oversized":
            raise ValueError("separator exceeds stream limit")
        return b"HTTP/1.1 503 unavailable\r\n"

    connect = AsyncMock(return_value=(SimpleNamespace(readline=readline), writer))
    if failure == "connect":
        connect.side_effect = OSError("gateway down")
    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(transport, "SIGNAL_TIMEOUT_SEC", 0.02)
    task = asyncio.create_task(transport._send_signal("http://gateway:9003/internal/turn-signal", "secret"))
    if failure == "cancel":
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises((OSError, ValueError, TimeoutError, asyncio.CancelledError)):
        await asyncio.wait_for(task, 1)
    assert closed == ([] if failure == "connect" else [True])
    if failure != "connect":
        writer.wait_closed.assert_called_once()
    assert connect.call_args.kwargs["limit"] == 1024


async def test_http_sender_request_is_empty_authenticated_and_has_no_redirect(monkeypatch) -> None:
    from src.control import turn_signal_transport as transport

    requests: list[bytes] = []
    writer = SimpleNamespace(write=requests.append, drain=AsyncMock(),
                             close=lambda: None, wait_closed=AsyncMock())
    reader = SimpleNamespace(readline=AsyncMock(return_value=b"HTTP/1.1 202 Accepted\r\n"))
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(return_value=(reader, writer)))
    await transport._send_signal("http://gateway:9003/internal/turn-signal", "secret")
    assert requests == [(b"POST /internal/turn-signal HTTP/1.1\r\nHost: gateway:9003\r\n"
                        b"Authorization: Bearer secret\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")]


async def test_internal_signal_caps_concurrent_slow_reads_and_recovers(monkeypatch) -> None:
    from src.control.routes.internal import build_router

    async def authenticated() -> None:
        pass

    router = build_router(authenticated)
    endpoint = router.routes[0].endpoint
    from starlette.requests import Request

    release = asyncio.Event()
    started = 0

    async def receive() -> dict:
        nonlocal started
        started += 1
        await release.wait()
        return {"type": "http.request", "body": b"", "more_body": False}

    def request() -> Request:
        return Request({"type": "http", "headers": []}, receive)

    tasks = [asyncio.create_task(endpoint(request())) for _ in range(4)]
    try:
        async with asyncio.timeout(1):
            while started < 4:
                await asyncio.sleep(0)
        from fastapi import HTTPException

        for _ in range(96):
            with pytest.raises(HTTPException) as exc:
                await endpoint(request())
            assert exc.value.status_code == 429
        # A cancelled reader must return its permit; the other three stay held.
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        replacement = asyncio.create_task(endpoint(request()))
        tasks.append(replacement)
        async with asyncio.timeout(1):
            while started < 5:
                await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks[1:])
        assert await endpoint(request()) == {"ok": True}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("gateway_down", [False, True])
def test_terminal_http_commit_reaches_real_inbox_and_scheduler_without_poll(tmp_path, monkeypatch, gateway_down) -> None:
    """H3: real result API → committed inbox → signal → wake admission/activation.

    The HTTP hop is in-process ASGI; paid backends and network are never used.
    A separate sender subscription models the task-server process-local channel.
    """
    from contextlib import contextmanager

    from src.control import node_inspector, task_server
    from src.control import turn_admission as ta
    from src.control import turn_signal_transport as transport
    from tests.inbox_seed import seed_child
    from tests.test_agent_inbox_delivery import _case, _env, _wakes

    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())
    db, obj = _env(tmp_path, monkeypatch)
    case_id = _case(db)
    seed_child(db, case_id, "child-signal", requester="sess-1")
    monkeypatch.setattr(task_server, "get_db", lambda: db)
    monkeypatch.setattr(task_server, "_worker_token", lambda: "signal-test")
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "signal-test")
    monkeypatch.setattr(node_inspector, "_nudge_worker", AsyncMock())
    obj.running = True

    async def scenario() -> None:
        sender_changes = ts.TurnChangeSubscription()

        @contextmanager
        def sender_subscription():
            try:
                yield sender_changes
            finally:
                sender_changes.close()

        monkeypatch.setattr(transport, "subscribe_turn_changes", sender_subscription)
        monkeypatch.setattr(task_server, "_hint_turn_scheduler", sender_changes.hint)
        gateway_app = control_api.build_control_api(obj)
        sent = 0

        async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway_app), base_url="http://gateway") as gateway:
            async def send(url: str, token: str) -> None:
                nonlocal sent
                assert db.get_task("child-signal")["status"] == "completed"
                sent += 1
                if gateway_down:
                    await asyncio.Future()
                response = await gateway.post("/internal/turn-signal", headers={"Authorization": f"Bearer {token}"})
                assert response.status_code == 202

            monkeypatch.setattr(transport, "_send_signal", send)
            scheduler = ts.TurnScheduler(db, obj._prepare_managed_turn, safety_net_sec=30,
                                         allowance=ta.SharedWaitingAllowance(),
                                         recover_lineage=obj._recover_managed_lineage,
                                         void_lineage=obj._void_withdrawn_lineage_async)
            tasks = [asyncio.create_task(obj._wake_dispatcher_loop(30)),
                     asyncio.create_task(scheduler.run()),
                     asyncio.create_task(transport.forward_turn_changes("http://gateway:9003/internal/turn-signal", "signal-test"))]
            try:
                # Let the initial empty inbox/scheduler passes finish before commit.
                await asyncio.sleep(0.1)
                assert _wakes(db) == []
                started = asyncio.get_running_loop().time()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(task_server.app), base_url="http://task-server") as carrier:
                    response = await carrier.post("/tasks/child-signal/result-managed", headers={"Authorization": "Bearer signal-test"},
                                                  json={"node_id": "worker-a", "claim_token": "tok-seed", "success": True, "output": "done"})
                assert response.status_code == 200, response.text
                if gateway_down:
                    assert asyncio.get_running_loop().time() - started < 1
                    assert db.get_task("child-signal")["status"] == "completed"
                    assert _wakes(db) == []
                    return
                async with asyncio.timeout(2):
                    while not _wakes(db) or _wakes(db)[0]["status"] != "pending":
                        await asyncio.sleep(0.01)
                assert asyncio.get_running_loop().time() - started < 1
                assert len(_wakes(db)) == 1
                assert sent == 1
                assert "child-signal" in _wakes(db)[0]["prompt"]
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        assert not ts._SUBSCRIBERS

    asyncio.run(scenario())


async def test_lifespan_owns_sender_and_cleans_up_on_exception(monkeypatch) -> None:
    from config import config
    from src.control import task_server
    from src.control import turn_signal_transport as transport

    async def background() -> None:
        await asyncio.Future()

    stopped: list[bool] = []
    monkeypatch.setattr(config.system, "local_execution_enabled", False)
    monkeypatch.setattr(config.mesh, "worker_token", "signal-test")
    monkeypatch.setenv("GATEWAY_TURN_SIGNAL_URL", "http://gateway:9003/internal/turn-signal")
    monkeypatch.setattr(task_server, "get_registry", lambda: SimpleNamespace(start=lambda: None, stop=lambda: stopped.append(True)))
    for name in ("_stale_claim_reaper_loop", "_mesh_health_sampler_loop", "_telemetry_projection_flusher_loop"):
        monkeypatch.setattr(task_server, name, background)
    monkeypatch.setattr(task_server, "_drain_projection", lambda: ([], []))
    send = AsyncMock()
    monkeypatch.setattr(transport, "_send_signal", send)
    with pytest.raises(RuntimeError, match="lifespan interrupted"):
        async with task_server._lifespan(task_server.app):
            task_server._hint_turn_scheduler()
            async with asyncio.timeout(1):
                while send.await_count < 1:
                    await asyncio.sleep(0)
            assert send.call_args.args == ("http://gateway:9003/internal/turn-signal", "signal-test")
            raise RuntimeError("lifespan interrupted")
    assert stopped == [True]
    assert not ts._SUBSCRIBERS


@pytest.mark.parametrize("body", [b"", b"garbage"])
async def test_internal_signal_rejects_stalled_or_chunked_garbage(body) -> None:
    from fastapi import HTTPException
    from starlette.requests import Request

    from src.control.routes.internal import build_router

    async def authenticated() -> None:
        pass

    async def receive() -> dict:
        if not body:
            await asyncio.Future()
        return {"type": "http.request", "body": body, "more_body": True}

    endpoint = build_router(authenticated).routes[0].endpoint
    with ts.subscribe_turn_changes() as changed:
        with pytest.raises(HTTPException) as exc:
            await endpoint(Request({"type": "http", "headers": []}, receive))
        assert exc.value.status_code == (408 if not body else 413)
        assert not await changed.wait(0.01)
