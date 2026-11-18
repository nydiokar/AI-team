"""ASGI construction/lifespan against the per-test database, without agents."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from config import config
from src.control import control_api, node_registry, task_server


def test_gateway_application_health() -> None:
    app = control_api.build_control_api(SimpleNamespace())
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert client.get("/api/sessions").status_code == 401


def test_task_server_lifespan_and_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.system, "local_execution_enabled", False)
    monkeypatch.delenv("GATEWAY_TURN_SIGNAL_URL", raising=False)
    registry = node_registry.NodeRegistry()
    monkeypatch.setattr(node_registry, "_registry", registry)
    with TestClient(task_server.app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert registry._expiry_task is not None
    assert registry._expiry_task.done()
