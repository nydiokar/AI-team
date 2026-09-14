"""Regression coverage for diagnostics of a blocked embedded task server."""

from __future__ import annotations

import asyncio
import threading

from src.control.embedded_server import EmbeddedTaskServer, _EventLoopStallWatchdog


class _BlockedLoop:
    def call_soon_threadsafe(self, _callback) -> None:
        """Accept callbacks without running them, like a blocked event loop."""


def test_event_loop_watchdog_dumps_stalled_gateway_stack(monkeypatch) -> None:
    dumped = threading.Event()
    monkeypatch.setattr(
        "src.control.embedded_server.faulthandler.dump_traceback",
        lambda **_kwargs: dumped.set(),
    )
    watchdog = _EventLoopStallWatchdog(_BlockedLoop(), "test", threshold_seconds=0.01)
    watchdog.start()
    try:
        assert dumped.wait(timeout=1.0)
    finally:
        watchdog.stop()


def test_embedded_task_server_owns_a_dedicated_event_loop_thread() -> None:
    started = threading.Event()
    served_thread_ids: list[int] = []

    class FakeServer:
        started = False
        should_exit = False

        async def serve(self) -> None:
            served_thread_ids.append(threading.get_ident())
            self.started = True
            started.set()
            while not self.should_exit:
                await asyncio.sleep(0.001)

    server = EmbeddedTaskServer("127.0.0.1", 9002)
    fake_server = FakeServer()
    server._server = fake_server
    thread = threading.Thread(target=server._run_server_thread)
    thread.start()
    try:
        assert started.wait(timeout=1.0)
        assert served_thread_ids == [thread.ident]
        assert served_thread_ids[0] != threading.get_ident()
    finally:
        fake_server.should_exit = True
        thread.join(timeout=1.0)
        assert not thread.is_alive()
