from __future__ import annotations

import asyncio
import json
import socket
import time

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from src.control import control_api
from src.control.routes.monitoring import build_router
from src.services.session_service import SessionService
from src.services.session_store import SessionStore
from src.worker.agent import _run_nudge_listener


class _StubOrchestrator:
    def __init__(self) -> None:
        self.session_service = SessionService(SessionStore(), repo_path_validator=lambda _path: None)
        self.quota_coordinator = None


def _router(monkeypatch, auth_guard):
    monkeypatch.setattr("src.control.control_api._worker_token", lambda: "mesh-secret")
    node = {
        "node_id": "kanebra", "live": True, "tailscale_ip": "100.88.11.88",
        "api_port": 9001, "capabilities": {"backends": ["claude"]},
    }
    monkeypatch.setattr("src.control.routes.monitoring._node_for_claude_auth", lambda _node_id: node)
    monkeypatch.setattr("src.control.control_api._is_tailnet", lambda _address: True)
    return build_router(_StubOrchestrator(), require_auth=auth_guard)


def _endpoint(router, path: str) -> APIRoute:
    route = next((route for route in router.routes if isinstance(route, APIRoute) and route.path == path), None)
    if route is None:
        raise AssertionError(f"Route not registered: {path}")
    return route


def test_control_api_registers_authenticated_claude_auth_routes(monkeypatch) -> None:
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "dashboard-secret")
    monkeypatch.setattr(control_api, "_worker_token", lambda: "mesh-secret")
    app = control_api.build_control_api(_StubOrchestrator())
    operation = app.openapi()["paths"]["/api/nodes/{node_id}/claude-auth/start"]["post"]
    assert operation["security"]


def test_claude_auth_start_proxies_only_to_registered_claude_node(monkeypatch) -> None:
    async def auth_guard() -> None:
        return None

    router = _router(monkeypatch, auth_guard)
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps({"login_id": "a" * 32, "status": "running"}).encode()

    def _urlopen(request, timeout: int):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("src.control.routes.monitoring.urllib.request.urlopen", _urlopen)
    route = _endpoint(router, "/api/nodes/{node_id}/claude-auth/start")
    response = route.endpoint("kanebra")
    assert response["status"] == "running"
    assert captured == {
        "url": "http://100.88.11.88:9001/claude-auth/start",
        "authorization": "Bearer mesh-secret",
        "timeout": 3,
    }


def test_claude_auth_routes_require_auth_guard_and_online_claude(monkeypatch) -> None:
    async def auth_guard() -> None:
        return None

    router = _router(monkeypatch, auth_guard)
    route = _endpoint(router, "/api/nodes/{node_id}/claude-auth/start")
    auth_dependency = route.dependant.dependencies[0].call
    assert auth_dependency is auth_guard

    monkeypatch.setattr("src.control.routes.monitoring._node_for_claude_auth", lambda _node_id: {
        "node_id": "kanebra", "live": False, "tailscale_ip": "100.88.11.88",
        "api_port": 9001, "capabilities": {"backends": ["claude"]},
    })
    with pytest.raises(HTTPException) as offline:
        route.endpoint("kanebra")
    assert offline.value.status_code == 409
    assert offline.value.detail == "node_offline"


def test_worker_auth_rpc_rejects_missing_worker_token() -> None:
    async def exercise() -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        task = asyncio.create_task(_run_nudge_listener(
            "127.0.0.1", port, asyncio.Event(), worker_token="mesh-secret"
        ))
        try:
            deadline = time.monotonic() + 2
            while True:
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.01)
            writer.write(
                b"POST /claude-auth/start HTTP/1.1\r\nContent-Length: 2\r\n"
                b"Content-Type: application/json\r\nConnection: close\r\n\r\n{}"
            )
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            assert response.startswith(b"HTTP/1.1 401 Error")
            assert b'"reason": "unauthorized"' in response
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
