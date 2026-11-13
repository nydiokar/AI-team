"""Gateway SSE over real loopback TCP, including disconnect and reconnection."""

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
import uvloop

from src.control import control_api
from src.core import observability


@pytest.mark.parametrize("loop", ["asyncio", "uvloop"])
def test_gateway_sse_disconnect_and_reconnect(
    loop: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(observability, "_LOGS_DIR", tmp_path)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "isolated-token")
    original = control_api.event_stream_frames
    closed: list[asyncio.Event] = []

    async def tracked_frames(**kwargs):
        done = asyncio.Event()
        closed.append(done)
        try:
            async for frame in original(**kwargs):
                yield frame
        finally:
            done.set()

    monkeypatch.setattr(control_api, "event_stream_frames", tracked_frames)

    async def exercise() -> None:
        app = control_api.build_control_api(SimpleNamespace())
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if task.done():
                            await task
                            pytest.fail("server exited before startup")
                        await asyncio.sleep(0.01)
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                        assert (await client.get("/api/events/stream")).status_code == 401
                        async with client.stream("GET", "/api/events/stream?token=isolated-token") as response:
                            assert response.status_code == 200
                            assert response.headers["content-type"].startswith("text/event-stream")
                            assert await anext(response.aiter_lines()) == ": connected"
                        await asyncio.wait_for(closed[0].wait(), timeout=3)
                        observability.emit_event("dependency_reconnect", session_id="offline")
                        async with client.stream("GET", "/api/events/stream?token=isolated-token") as response:
                            assert "dependency_reconnect" in await anext(response.aiter_lines())
                        await asyncio.wait_for(closed[1].wait(), timeout=3)
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=5)

    factory = uvloop.new_event_loop if loop == "uvloop" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(exercise())
