"""Unit tests for TaskServerClient — no real server, HTTP layer stubbed.

Covers the contract the gateway relies on: failures degrade to None/[] (so an
unreachable server reads as "mesh unhealthy", not a crash), and the node cache
honours its TTL.
"""
import time

import pytest

from src.control.mesh_health import MeshHealth
from src.control.task_server_client import TaskServerClient


@pytest.fixture
def client():
    return TaskServerClient("http://127.0.0.1:9099", "tok", node_cache_ttl=0.2)


def test_is_healthy_true_on_ok(monkeypatch, client):
    monkeypatch.setattr(client, "_get", lambda path, **kw: {"status": "ok", "db": {}})
    assert client.is_healthy() is True


def test_is_healthy_false_after_consecutive_failures(monkeypatch):
    """Sliding window requires 3 consecutive failures before returning False."""
    mh = MeshHealth(window_size=6, failure_threshold=3)
    c = TaskServerClient("http://127.0.0.1:9099", "tok", mesh_health=mh)
    monkeypatch.setattr(c, "_get", lambda path, **kw: None)
    # First two failures are absorbed
    assert c.is_healthy() is True
    assert c.is_healthy() is True
    # Third consecutive failure triggers degradation
    assert c.is_healthy() is False


def test_list_nodes_returns_empty_on_failure(monkeypatch, client):
    monkeypatch.setattr(client, "_get", lambda path, **kw: None)
    assert client.list_nodes() == []


def test_list_nodes_cache_hits_within_ttl(monkeypatch, client):
    calls = {"n": 0}

    def fake_get(path, **kw):
        calls["n"] += 1
        return [{"node_id": "a", "status": "online"}]

    monkeypatch.setattr(client, "_get", fake_get)
    client.list_nodes()
    client.list_nodes()
    assert calls["n"] == 1  # second call served from cache


def test_list_nodes_cache_expires(monkeypatch, client):
    calls = {"n": 0}

    def fake_get(path, **kw):
        calls["n"] += 1
        return [{"node_id": "a", "status": "online"}]

    monkeypatch.setattr(client, "_get", fake_get)
    client.list_nodes()
    time.sleep(0.25)  # exceed the 0.2s TTL
    client.list_nodes()
    assert calls["n"] == 2


def test_list_nodes_returns_stale_cache_when_refresh_fails(monkeypatch, client):
    # First call succeeds and populates the cache.
    monkeypatch.setattr(client, "_get", lambda path, **kw: [{"node_id": "a", "status": "online"}])
    client.list_nodes()
    time.sleep(0.25)
    # Refresh now fails — we should get the stale cache, not [].
    monkeypatch.setattr(client, "_get", lambda path, **kw: None)
    assert client.list_nodes() == [{"node_id": "a", "status": "online"}]


def test_get_node_finds_by_id(monkeypatch, client):
    monkeypatch.setattr(
        client, "_get", lambda path, **kw: [{"node_id": "x"}, {"node_id": "y"}]
    )
    assert client.get_node("y") == {"node_id": "y"}
    assert client.get_node("z") is None


def test_nudge_true_only_on_accept(monkeypatch, client):
    monkeypatch.setattr(client, "_post", lambda path, *a, **kw: {"status": "nudged"})
    assert client.nudge("n1") is True
    monkeypatch.setattr(client, "_post", lambda path, *a, **kw: None)
    assert client.nudge("n1") is False
