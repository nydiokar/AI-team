"""Build identity: which commit a gateway / task-server / worker process is running.

The image bakes AI_TEAM_GIT_SHA at build time; a worker running from a checkout
falls back to `git rev-parse`. /health surfaces it, and the task-server counts
online nodes whose heartbeat reports a different build (version skew).
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.core import build_info as bi


@pytest.fixture(autouse=True)
def _fresh_cache():
    bi.build_info.cache_clear()
    yield
    bi.build_info.cache_clear()


def test_env_sha_wins_over_git(monkeypatch):
    monkeypatch.setenv("AI_TEAM_GIT_SHA", "abc1234")
    monkeypatch.setattr(bi, "_git_head_sha", lambda: "fffffff")
    assert bi.build_info().git_sha == "abc1234"


@pytest.mark.parametrize("env_value", ["", "unknown"])
def test_falls_back_to_git_when_env_unset(monkeypatch, env_value):
    monkeypatch.setenv("AI_TEAM_GIT_SHA", env_value)
    monkeypatch.setattr(bi, "_git_head_sha", lambda: "0d593c6")
    assert bi.build_info().git_sha == "0d593c6"


def test_unknown_when_no_env_and_no_git(monkeypatch):
    monkeypatch.delenv("AI_TEAM_GIT_SHA", raising=False)
    monkeypatch.setattr(bi, "_git_head_sha", lambda: None)
    assert bi.build_info().git_sha == "unknown"


def test_version_comes_from_package_metadata():
    assert bi.build_info().version  # pyproject [project].version, installed metadata


def test_skew_counts_only_online_nodes_with_a_different_known_build():
    def node(status, sha):
        live = {"build_sha": sha} if sha is not None else None
        return SimpleNamespace(status=status, live_state=live)

    nodes = [
        node("online", "aaaaaaa"),   # same build
        node("online", "aaaaaaa1"),  # same build, longer short-SHA
        node("online", "bbbbbbb"),   # skewed
        node("online", None),        # old worker: no build reported
        node("offline", "ccccccc"),  # offline: ignored
    ]
    assert bi.node_build_skew(nodes, "aaaaaaa") == {"nodes_build_mismatch": 1, "nodes_build_unknown": 1}


def test_gateway_health_reports_build(monkeypatch):
    from src.control import control_api
    from src.services.session_service import SessionService
    from src.services.session_store import SessionStore

    monkeypatch.setenv("AI_TEAM_GIT_SHA", "abc1234")
    orch = SimpleNamespace(session_service=SessionService(SessionStore(), repo_path_validator=lambda _p: None))
    client = TestClient(control_api.build_control_api(orch))
    build = client.get("/health").json()["build"]
    assert build["git_sha"] == "abc1234" and build["version"]


def test_task_server_health_reports_build_and_skew(monkeypatch):
    import src.control.task_server as ts

    monkeypatch.setenv("AI_TEAM_GIT_SHA", "abc1234")
    fake_registry = SimpleNamespace(list_all=lambda: [
        SimpleNamespace(status="online", live_state={"build_sha": "def5678"}),
    ])
    monkeypatch.setattr(ts, "get_registry", lambda: fake_registry)
    monkeypatch.setattr(ts, "get_db", lambda: None)
    body = TestClient(ts.app).get("/health").json()
    assert body["build"]["git_sha"] == "abc1234"
    assert body["build"]["nodes_build_mismatch"] == 1
