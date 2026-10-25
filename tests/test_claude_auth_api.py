from __future__ import annotations

import asyncio
import json
import socket
import time
import urllib.request

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from src.control import control_api
from src.control.routes.monitoring import build_router
from src.control.routes.monitoring import _auth_backends
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


def test_backend_code_reaches_selected_node(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def auth_guard() -> None:
        pass

    received: list[tuple[str, object]] = []

    class Response:
        def __enter__(self) -> Response: return self
        def __exit__(self, *_: object) -> None: pass
        def read(self, _: int) -> bytes: return b'{"ok":true}'

    def send(request: urllib.request.Request, timeout: float) -> Response:
        assert request.data is not None
        received.append((request.full_url, json.loads(request.data)))
        assert request.get_header("Authorization") == "Bearer mesh-secret"
        assert timeout == 3
        return Response()

    monkeypatch.setattr("src.control.routes.monitoring.urllib.request.urlopen", send)
    app = FastAPI()
    app.include_router(_router(monkeypatch, auth_guard))
    payload: dict[str, str] = {"login_id": "a" * 32, "input": "code#state\n"}
    with TestClient(app) as client:
        response = client.post("/api/nodes/kanebra/backend-auth/claude/input", json=payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert received == [("http://100.88.11.88:9001/backend-auth/claude/input", payload)]


@pytest.mark.parametrize("node", [
    {"capabilities": {"backends": ["claude", "codex"]}},
    {"backends": ["claude", "codex"]},
    {"backends": '["claude", "codex"]'},
    {"capabilities": {}, "backends": '["claude", "codex"]'},
])
def test_backend_shapes(node) -> None:
    assert _auth_backends(node) == ["claude", "codex"]


@pytest.mark.parametrize("value", ['not-json', '{}', '"claude"', None, 1])
def test_malformed_backends_fail_closed(value) -> None:
    assert _auth_backends({"backends": value}) == []


@pytest.mark.parametrize("backends", [["claude"], '["claude"]'])
def test_db_fallback_node_allows_login(monkeypatch, backends) -> None:
    async def auth_guard() -> None:
        pass
    router = build_router(_StubOrchestrator(), require_auth=auth_guard)
    from src.control import node_registry
    node = {"node_id": "kanebra", "backends": backends, "tailscale_ip": "100.88.11.88", "api_port": 9001}
    # Exercise the real lookup and annotation, rather than replacing the node helper.
    monkeypatch.setattr(node_registry, "get_registry", lambda: type("Registry", (), {"is_empty": lambda _: True})())
    monkeypatch.setattr(control_api, "_db", lambda: type("DB", (), {"get_node": lambda _, _id: node})())
    monkeypatch.setattr(control_api, "_annotate_node_liveness", lambda value: value.update(live=True))
    monkeypatch.setattr(control_api, "_worker_token", lambda: "mesh-secret")
    monkeypatch.setattr(control_api, "_is_tailnet", lambda _: True)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, _): return b'{"login_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","status":"running"}'
    monkeypatch.setattr("src.control.routes.monitoring.urllib.request.urlopen", lambda *_, **__: Response())
    assert _endpoint(router, "/api/nodes/{node_id}/backend-auth/{backend}/start").endpoint("kanebra", "claude")["status"] == "running"


@pytest.mark.parametrize("response,detail", [
    (b"x" * 524289, "worker_response_too_large"),
    (b"garbage", "worker_invalid_response"),
    (b"[]", "worker_invalid_response"),
    (b'{"state":"broken"}', "worker_invalid_response"),
])
def test_invalid_upstream_status(monkeypatch, response, detail) -> None:
    async def auth_guard() -> None: pass
    router = _router(monkeypatch, auth_guard)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, _): return response
    monkeypatch.setattr("src.control.routes.monitoring.urllib.request.urlopen", lambda *_, **__: Response())
    with pytest.raises(HTTPException) as exc:
        _endpoint(router, "/api/nodes/{node_id}/backend-auth/{backend}/status").endpoint("kanebra", "claude")
    assert exc.value.detail == detail


@pytest.mark.parametrize("change,backend,detail", [
    ({"live": False}, "claude", "node_offline"),
    ({"capabilities": {"backends": []}}, "claude", "claude_backend_unavailable"),
    ({}, "shell", "backend_unsupported"),
    ({"tailscale_ip": "bad"}, "claude", "node_address_unavailable"),
    ({"api_port": "bad"}, "claude", "node_address_unavailable"),
    ({"api_port": 99999}, "claude", "node_address_unavailable"),
])
def test_gateway_rejects_ineligible_nodes(monkeypatch, change, backend, detail) -> None:
    async def guard() -> None: pass
    router = _router(monkeypatch, guard)
    node = {"live": True, "capabilities": {"backends": ["claude"]}, "tailscale_ip": "100.88.11.88", **change}
    monkeypatch.setattr("src.control.routes.monitoring._node_for_claude_auth", lambda _: node)
    with pytest.raises(HTTPException) as exc:
        _endpoint(router, "/api/nodes/{node_id}/backend-auth/{backend}/start").endpoint("node", backend)
    assert exc.value.detail == detail


def test_gateway_auth_request_limits_and_methods(monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    async def guard() -> None: pass
    app = FastAPI()
    control_api._preparse_byte_guard(app)
    app.include_router(_router(monkeypatch, guard))
    with TestClient(app) as client:
        path = "/api/nodes/node/backend-auth/claude/input"
        assert client.post(path, content=b"x" * 8193).status_code == 413
        assert client.get(path).status_code == 405
        assert client.post(path, json={"login_id": "a" * 32, "offset": "0"}).status_code == 422
        assert client.post(path, json=[]).status_code == 422


def test_gateway_missing_credentials_tailnet_timeout_and_saturation(monkeypatch) -> None:
    async def guard() -> None: pass
    router = _router(monkeypatch, guard)
    endpoint = _endpoint(router, "/api/nodes/{node_id}/backend-auth/{backend}/start").endpoint
    monkeypatch.setattr(control_api, "_worker_token", lambda: "")
    with pytest.raises(HTTPException, match="worker_auth_unavailable"):
        endpoint("node", "claude")
    monkeypatch.setattr(control_api, "_worker_token", lambda: "token")
    monkeypatch.setattr(control_api, "_is_tailnet", lambda _: False)
    with pytest.raises(HTTPException, match="node_address_not_tailnet"):
        endpoint("node", "claude")
    monkeypatch.setattr(control_api, "_is_tailnet", lambda _: True)
    def timeout(*_, **__): raise TimeoutError()
    monkeypatch.setattr("src.control.routes.monitoring.urllib.request.urlopen", timeout)
    with pytest.raises(HTTPException, match="worker_unreachable"):
        endpoint("node", "claude")
    from src.control.routes.monitoring import _BoundedAuthRoute
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI(); app.include_router(router)
    for _ in range(8): _BoundedAuthRoute._slots.acquire()
    try:
        with TestClient(app) as client:
            assert client.post("/api/nodes/node/backend-auth/claude/start").status_code == 429
    finally:
        for _ in range(8): _BoundedAuthRoute._slots.release()


def test_worker_rejects_malformed_unsupported_and_cross_provider_requests(monkeypatch) -> None:
    from src.worker.claude_auth import backend_auth_session, BackendAuthError
    def mismatch(*_): raise BackendAuthError("backend_mismatch")
    monkeypatch.setattr(backend_auth_session, "validate_backend", mismatch)
    monkeypatch.setattr(backend_auth_session, "status", lambda backend: {"backend": backend, "state": "signed_out"})
    async def exercise() -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        task = asyncio.create_task(_run_nudge_listener("127.0.0.1", port, asyncio.Event(), worker_token="secret"))
        try:
            await asyncio.sleep(0.03)
            cases = [
                ("GET", "/backend-auth/claude/status", b"{}", "secret", 200),
                ("GET", "/backend-auth/codex/status", b"{}", "wrong", 401),
                ("POST", "/backend-auth/shell/start", b"{}", "secret", 400),
                ("GET", "/backend-auth/claude/start", b"{}", "secret", 404),
                ("POST", "/backend-auth/claude/start", b"[]", "secret", 400),
                ("POST", "/backend-auth/claude/start", b"{", "secret", 400),
                ("POST", "/backend-auth/claude/start", b"x" * 8193, "secret", 400),
                ("POST", "/backend-auth/codex/poll", json.dumps({"login_id": "a" * 32}).encode(), "secret", 400),
            ]
            for method, path, body, token, code in cases:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(f"{method} {path} HTTP/1.1\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 3)
                writer.close(); await writer.wait_closed()
                assert response.startswith(f"HTTP/1.1 {code} ".encode())
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(exercise())


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


def test_gateway_caches_status_refreshes_and_records_verified_login(monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def guard() -> None:
        pass

    calls: list[str] = []
    state: list[str] = ["signed_out"]

    from typing import Self

    class Response:
        def __init__(self, data: dict[str, object]) -> None:
            self.data = data
        def __enter__(self) -> Self:
            return self
        def __exit__(self, *_: object) -> None:
            pass
        def read(self, _: int) -> bytes:
            return json.dumps(self.data).encode()

    def send(request: urllib.request.Request, timeout: float) -> Response:
        action = request.full_url.rsplit("/", 1)[-1]
        calls.append(action)
        if action == "status":
            return Response({"state": state[0]})
        if action == "poll":
            return Response({"status": "exited", "login_id": "a" * 32, "authenticated": True, "output": "", "offset": 0})
        return Response({"status": "running", "login_id": "a" * 32})

    monkeypatch.setattr("src.control.routes.monitoring.urllib.request.urlopen", send)
    app = FastAPI()
    app.include_router(_router(monkeypatch, guard))
    prefix: str = "/api/nodes/kanebra/backend-auth/claude"
    with TestClient(app) as client:
        first = client.get(f"{prefix}/status").json()
        assert first["checked_at"]
        assert first["login_verified_at"] is None
        assert client.get(f"{prefix}/status").json() == first
        assert calls == ["status"]
        client.get(f"{prefix}/status?refresh=true")
        assert calls == ["status", "status"]
        assert client.post(f"{prefix}/start").status_code == 200
        completed = client.post(f"{prefix}/poll", json={"login_id": "a" * 32}).json()
        assert completed["login_verified_at"]
        observed = client.get(f"{prefix}/status").json()
        assert observed["state"] == "signed_in"
        assert observed["login_verified_at"] == completed["login_verified_at"]
        assert calls == ["status", "status", "start", "poll"]
        # A cached observation never bypasses current node eligibility.
        monkeypatch.setattr("src.control.routes.monitoring._node_for_claude_auth", lambda _: {"live": False})
        assert client.get(f"{prefix}/status").status_code == 409


@pytest.mark.parametrize("status,body,reason", [
    (400, b'{"reason":"login_in_progress"}', "login_in_progress"),
    (400, b'{"reason":"login_not_found"}', "login_not_found"),
    (400, b'{"reason":"unexpected secret text"}', "worker_rejected_request"),
    (400, b'garbage', "worker_rejected_request"),
    (401, b'', "worker_auth_mismatch"),
    (404, b'', "worker_auth_route_unavailable"),
])
def test_worker_error_reasons_are_bounded_allowlisted_and_logged(monkeypatch, caplog, status, body, reason) -> None:
    import io
    import urllib.error
    async def guard() -> None: pass
    router = _router(monkeypatch, guard)
    def reject(*_, **__):
        raise urllib.error.HTTPError("http://worker/", status, "Error", {}, io.BytesIO(body))
    monkeypatch.setattr("src.control.routes.monitoring.urllib.request.urlopen", reject)
    endpoint = _endpoint(router, "/api/nodes/{node_id}/backend-auth/{backend}/start").endpoint
    with pytest.raises(HTTPException) as exc:
        endpoint("kanebra", "claude")
    assert exc.value.detail == reason
    assert "node=kanebra backend=claude action=start" in caplog.text
    assert "unexpected secret text" not in caplog.text
