"""[A82 Stage 3 rework 4, m2] Streamed request-body byte cap (pure ASGI).

A ``Content-Length`` check alone is bypassed by a chunked upload, and FastAPI
reads (and parses) the whole body before any route dependency runs. This
middleware counts the bytes as they are RECEIVED for the matching routes and
answers a structured 413 as soon as the cap is exceeded — before the body is
parsed or reaches the route. Buffering is bounded by the cap itself.
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Tuple

Scope = Dict[str, Any]
Message = Dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

_TOO_LARGE = json.dumps({"detail": {"ok": False, "reason": "payload_too_large"}}).encode()


class BodyCapMiddleware:
    """``rules`` is a list of ``(path_regex, max_bytes)``; the first matching
    rule for a POST/PUT/PATCH request applies. Other requests pass through
    untouched (streamed, unbuffered)."""

    def __init__(self, app: Callable[..., Awaitable[None]], rules: List[Tuple[str, int]]) -> None:
        self.app = app
        self.rules = [(re.compile(p), int(n)) for p, n in rules]

    def _cap_for(self, scope: Scope) -> int:
        if scope.get("type") != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            return 0
        path = scope.get("path") or ""
        for rx, cap in self.rules:
            if rx.fullmatch(path):
                return cap
        return 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        cap = self._cap_for(scope)
        if not cap:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    if int(value) > cap:
                        await _reject(send)
                        return
                except ValueError:
                    await _reject(send, status=400, reason="invalid_content_length")
                    return
        chunks: List[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"") or b""
            total += len(body)
            if total > cap:
                await _reject(send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        buffered = b"".join(chunks)
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _reject(send: Send, status: int = 413, reason: str = "payload_too_large") -> None:
    body = _TOO_LARGE if reason == "payload_too_large" else json.dumps(
        {"detail": {"ok": False, "reason": reason}}
    ).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
