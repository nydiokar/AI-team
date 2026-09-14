"""Regression coverage for diagnostics of a blocked embedded task server."""

from __future__ import annotations

import threading

from src.control.embedded_server import _EventLoopStallWatchdog


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
