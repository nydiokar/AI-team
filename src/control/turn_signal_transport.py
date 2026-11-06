"""A105: a bounded, hint-only task-server → gateway HTTP nudge.

One lifespan-owned sender, one pending bit, no per-commit task or payload queue.
The durable DB and existing consumer timers recover a lost nudge.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from src.control.turn_scheduler import subscribe_turn_changes

logger = logging.getLogger(__name__)
SIGNAL_INTERVAL_SEC = 0.1
SIGNAL_TIMEOUT_SEC = 2.0


async def _send_signal(url: str, token: str) -> None:
    """Internal HTTP only: no redirects/proxies/body reads; total IO deadline.

    asyncio streams keep socket IO cancellable. Native DNS resolution follows
    asyncio's executor lifecycle; it cannot be forcibly interrupted by timeout.
    The response line is capped at 1 KiB; no response body is retained.
    """
    target = urlsplit(url)
    if (target.scheme != "http" or not target.hostname or target.username
            or target.password or target.query or target.fragment
            or target.path != "/internal/turn-signal"
            or any(c in token for c in "\r\n")):
        raise ValueError("invalid turn signal configuration")
    writer: asyncio.StreamWriter | None = None
    deadline: float = asyncio.get_running_loop().time() + SIGNAL_TIMEOUT_SEC
    try:
        async with asyncio.timeout_at(deadline):
            reader, writer = await asyncio.open_connection(
                target.hostname, target.port or 80, limit=1024,
            )
            request = (
                f"POST {target.path} HTTP/1.1\r\nHost: {target.netloc}\r\n"
                f"Authorization: Bearer {token}\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            status = await reader.readline()
            if status.split()[:2] != [b"HTTP/1.1", b"202"]:
                raise OSError("turn signal refused")
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(
                    writer.wait_closed(),
                    timeout=max(0.0, min(0.1, deadline - asyncio.get_running_loop().time())),
                )
            except (TimeoutError, OSError):
                pass


async def forward_turn_changes(url: str, token: str) -> None:
    """Forward coalesced shared hints, at most ten sends/second, single flight."""
    with subscribe_turn_changes() as changes:
        while True:
            await changes.wait(None)
            changes.clear()
            started: float = asyncio.get_running_loop().time()
            try:
                await _send_signal(url, token)
            except Exception:  # noqa: BLE001 — hint failure never reaches a carrier request
                # Encoding/connection errors may include request credentials.
                logger.debug("event=turn_signal_send_failed")
            await asyncio.sleep(max(
                0.0, SIGNAL_INTERVAL_SEC - (asyncio.get_running_loop().time() - started),
            ))
