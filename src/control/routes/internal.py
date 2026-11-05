"""Hint-only controller transport; authority remains in the turn/inbox DB."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.requests import ClientDisconnect

from src.control.turn_scheduler import notify_turn_queue_changed


def build_router(require_auth: Callable[..., Awaitable[None]]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_auth)])
    slots = threading.BoundedSemaphore(4)

    @router.post("/internal/turn-signal", status_code=202)
    async def turn_signal(request: Request) -> dict[str, bool]:
        if not slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="turn_signal_busy")
        try:
            # Empty bodies only, including chunked input; never buffer a body.
            declared = request.headers.get("content-length")
            if declared is not None and declared != "0":
                raise HTTPException(status_code=413, detail="turn_signal_body_not_empty")
            try:
                async with asyncio.timeout(1):
                    async for chunk in request.stream():
                        if chunk:
                            raise HTTPException(status_code=413, detail="turn_signal_body_not_empty")
            except (TimeoutError, ClientDisconnect):
                raise HTTPException(status_code=408, detail="turn_signal_body_incomplete") from None
            notify_turn_queue_changed()
            return {"ok": True}
        finally:
            slots.release()

    return router
