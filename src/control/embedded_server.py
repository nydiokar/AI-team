"""
Embedded task server — runs the FastAPI mesh task server inside the gateway process.

Before D1 the task server ran as a separate `uvicorn src.control.task_server:app`
process. That meant the gateway's in-memory `NodeRegistry` (via `get_registry()`)
was always empty: the registry lived in the uvicorn process, not the gateway, and
node discovery had to round-trip through SQLite.

Embedding solves that. We run `uvicorn.Server.serve()` as an asyncio task on the
gateway's own event loop, so:
  - the `get_registry()` singleton is shared between the HTTP handlers and the
    orchestrator's dispatch code (same process, same module instance),
  - the registry's heartbeat-expiry loop runs on the gateway loop,
  - one PM2 entry, one `.env`, one DB connection.

Lifecycle is owned by the orchestrator: `start()` on gateway startup,
`stop()` on shutdown. Both are no-ops unless `MESH_ENABLED=true`.
"""

import asyncio
import faulthandler
import logging
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class _EventLoopStallWatchdog:
    """Dump the gateway stack when its shared HTTP/event loop stops progressing."""

    def __init__(self, loop: asyncio.AbstractEventLoop, component: str, threshold_seconds: float = 5.0) -> None:
        self._loop = loop
        self._component = component
        self._threshold_seconds = threshold_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"{component}-loop-watchdog",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            acknowledged = threading.Event()
            started = time.monotonic()
            try:
                self._loop.call_soon_threadsafe(acknowledged.set)
            except RuntimeError:
                return
            if not acknowledged.wait(timeout=self._threshold_seconds):
                logger.error(
                    "event=embedded_event_loop_stalled component=%s elapsed_ms=%.1f",
                    self._component,
                    (time.monotonic() - started) * 1000,
                )
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                while not self._stop.is_set() and not acknowledged.wait(timeout=0.1):
                    pass
            self._stop.wait(timeout=1.0)


class EmbeddedTaskServer:
    """Run the mesh server on an isolated event loop in this process."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._server = None  # uvicorn.Server
        self._server_thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._loop_watchdog: Optional[_EventLoopStallWatchdog] = None

    async def start(self) -> None:
        """Bind and start serving on an isolated event loop. Idempotent."""
        if self._server_thread is not None and self._server_thread.is_alive():
            logger.warning("event=embedded_task_server_already_running")
            return

        import uvicorn
        from src.control.task_server import app

        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None
        self._started.clear()
        self._startup_error = None
        self._server_thread = threading.Thread(
            target=self._run_server_thread,
            name="embedded-task-server",
            daemon=True,
        )
        self._server_thread.start()

        started = await asyncio.to_thread(self._started.wait, 5.0)
        if started and getattr(self._server, "started", False):
            logger.info(
                "event=embedded_task_server_started host=%s port=%s",
                self.host,
                self.port,
            )
            return
        if self._startup_error is not None:
            raise RuntimeError(
                f"embedded task server failed to start on {self.host}:{self.port}: {self._startup_error}"
            )
        logger.warning(
            "event=embedded_task_server_start_timeout host=%s port=%s",
            self.host,
            self.port,
        )

    def _run_server_thread(self) -> None:
        asyncio.run(self._serve_on_dedicated_loop())

    async def _serve_on_dedicated_loop(self) -> None:
        self._loop_watchdog = _EventLoopStallWatchdog(
            asyncio.get_running_loop(), "embedded-task-server"
        )
        self._loop_watchdog.start()
        try:
            serve_task = asyncio.create_task(self._server.serve())
            while not getattr(self._server, "started", False) and not serve_task.done():
                await asyncio.sleep(0.05)
            self._started.set()
            await serve_task
        except BaseException as e:
            self._startup_error = e
            self._started.set()
            logger.error("event=embedded_task_server_crashed err=%r", e)
        finally:
            if self._loop_watchdog is not None:
                self._loop_watchdog.stop()
                self._loop_watchdog = None

    async def stop(self) -> None:
        """Signal the isolated loop to stop and join its server thread."""
        if self._server is not None:
            self._server.should_exit = True
        if self._server_thread is not None and self._server_thread.is_alive():
            await asyncio.to_thread(self._server_thread.join, 10.0)
            if self._server_thread.is_alive():
                logger.warning("event=embedded_task_server_stop_timeout")
        logger.info("event=embedded_task_server_stopped")
        self._server_thread = None
        self._server = None

class EmbeddedControlServer:
    """Runs the gateway's Control API on the gateway event loop as a managed task.

    Same pattern as EmbeddedTaskServer, but the app is built from the live
    orchestrator (``build_control_api(orchestrator)``) so its read handlers share
    the orchestrator's in-process SessionService / NodeRegistry — see
    docs/CONTROL_SURFACE_UNIFICATION.md U1. Replaces the standalone dashboard
    process. Lifecycle owned by the orchestrator (start on startup, stop on
    shutdown).
    """

    def __init__(self, orchestrator, host: str, port: int) -> None:
        self.orchestrator = orchestrator
        self.host = host
        self.port = port
        self._server = None  # uvicorn.Server
        self._serve_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Bind and start serving on the gateway's event loop. Idempotent."""
        if self._serve_task is not None and not self._serve_task.done():
            logger.warning("event=embedded_control_server_already_running")
            return

        import uvicorn
        from src.control.control_api import build_control_api

        app = build_control_api(self.orchestrator)
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        # The gateway owns SIGINT/SIGTERM; uvicorn must not install its own.
        self._server.install_signal_handlers = lambda: None

        self._serve_task = asyncio.create_task(
            self._serve(), name="embedded-control-server"
        )

        for _ in range(50):  # up to ~5s
            if getattr(self._server, "started", False):
                logger.info(
                    "event=embedded_control_server_started host=%s port=%s",
                    self.host,
                    self.port,
                )
                return
            if self._serve_task.done():
                exc = self._serve_task.exception()
                self._serve_task = None
                self._server = None
                raise RuntimeError(
                    f"embedded control server failed to start on {self.host}:{self.port}: {exc}"
                )
            await asyncio.sleep(0.1)
        logger.warning(
            "event=embedded_control_server_start_timeout host=%s port=%s",
            self.host,
            self.port,
        )

    async def _serve(self) -> None:
        try:
            await self._server.serve()
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            logger.error("event=embedded_control_server_crashed err=%r", e)

    async def stop(self) -> None:
        """Signal uvicorn to shut down and await the serve task."""
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None and not self._serve_task.done():
            try:
                await asyncio.wait_for(self._serve_task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning("event=embedded_control_server_stop_timeout; cancelling")
                self._serve_task.cancel()
                try:
                    await self._serve_task
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info("event=embedded_control_server_stopped")
        self._serve_task = None
        self._server = None
