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
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EmbeddedTaskServer:
    """Runs the mesh FastAPI app on the current event loop as a managed task."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._server = None  # uvicorn.Server
        self._serve_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Bind and start serving on the gateway's event loop. Idempotent."""
        if self._serve_task is not None and not self._serve_task.done():
            logger.warning("event=embedded_task_server_already_running")
            return

        import uvicorn
        from src.control.task_server import app

        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            # We manage signals at the gateway level; uvicorn must not install its own.
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        # Prevent uvicorn from hijacking SIGINT/SIGTERM — the gateway owns those.
        self._server.install_signal_handlers = lambda: None

        self._serve_task = asyncio.create_task(
            self._serve(), name="embedded-task-server"
        )

        # Wait briefly for the server to come up so registration races don't fail.
        for _ in range(50):  # up to ~5s
            if getattr(self._server, "started", False):
                logger.info(
                    "event=embedded_task_server_started host=%s port=%s",
                    self.host,
                    self.port,
                )
                return
            if self._serve_task.done():
                # serve() exited early (e.g. port already bound). Surface it as a
                # normal RuntimeError so callers' `except Exception` can degrade
                # gracefully — uvicorn raises SystemExit on bind failure, which
                # would otherwise bypass `except Exception` and kill the process.
                exc = self._serve_task.exception()
                self._serve_task = None
                self._server = None
                raise RuntimeError(
                    f"embedded task server failed to start on {self.host}:{self.port}: {exc}"
                )
            await asyncio.sleep(0.1)
        logger.warning(
            "event=embedded_task_server_start_timeout host=%s port=%s",
            self.host,
            self.port,
        )

    async def _serve(self) -> None:
        try:
            await self._server.serve()
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            # Catch BaseException (not just Exception) because uvicorn calls
            # sys.exit(1) -> SystemExit on bind failure. Record it; the start()
            # poller sees the task is done and converts it to a RuntimeError.
            logger.error("event=embedded_task_server_crashed err=%r", e)

    async def stop(self) -> None:
        """Signal uvicorn to shut down and await the serve task."""
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None and not self._serve_task.done():
            try:
                await asyncio.wait_for(self._serve_task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning("event=embedded_task_server_stop_timeout; cancelling")
                self._serve_task.cancel()
                try:
                    await self._serve_task
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info("event=embedded_task_server_stopped")
        self._serve_task = None
        self._server = None
