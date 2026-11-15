"""Worker-side provider dispatch and Codex observation body (no provider access)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.worker.agent import WorkerAgent


def _agent(backends):
    agent = WorkerAgent.__new__(WorkerAgent)
    agent.cfg = SimpleNamespace(
        node_id="kanebra",
        backends=list(backends),
        quota_observe_enabled=True,
    )
    agent._backends = {}
    return agent


@pytest.mark.asyncio
async def test_observe_once_dispatches_every_observable_provider():
    agent = _agent(["codex", "claude"])
    seen = []
    posts = []

    async def fake_build(provider, observed_at):
        seen.append(provider)
        return {"provider": provider, "node_id": "kanebra", "observed_at": observed_at}

    class _HTTP:
        def post(self, path, body, timeout):
            posts.append((path, body, timeout))

    agent._build_quota_observation = fake_build
    agent._http = _HTTP()

    await agent._observe_quota_once()

    assert seen == ["claude", "codex"]
    assert [body["provider"] for _, body, _ in posts] == ["claude", "codex"]
    assert all(path == "/telemetry/quota-observation" for path, _, _ in posts)


@pytest.mark.asyncio
async def test_observe_once_isolates_one_provider_failure():
    agent = _agent(["claude", "codex"])
    posts = []

    async def fake_build(provider, observed_at):
        if provider == "claude":
            raise RuntimeError("claude_read_failed")
        return {"provider": provider, "node_id": "kanebra", "observed_at": observed_at}

    class _HTTP:
        def post(self, path, body, timeout):
            posts.append((path, body, timeout))

    agent._build_quota_observation = fake_build
    agent._http = _HTTP()

    await agent._observe_quota_once()

    assert [body["provider"] for _, body, _ in posts] == ["claude", "codex"]
    assert posts[0][1]["error"] == "RuntimeError"
    assert "error" not in posts[1][1]


@pytest.mark.asyncio
async def test_codex_body_uses_allowlisted_rate_limits():
    agent = _agent(["codex"])

    class Backend:
        def read_account_rate_limits(self, timeout=5.0):
            assert timeout == 5.0
            return {
                "plan_type": "plus",
                "primary": {
                    "used_percent": 36.0,
                    "window_minutes": 300,
                    "resets_at": 1794756717,
                },
            }

    agent._backends = {"codex": Backend()}

    body = await agent._build_codex_quota_observation("2026-11-15T12:00:00Z")

    assert body["provider"] == "codex"
    assert body["node_id"] == "kanebra"
    assert body["limits"]["primary"]["used_percent"] == 36.0
    assert "usage" not in body


@pytest.mark.asyncio
async def test_codex_body_without_windows_is_an_explicit_failure():
    agent = _agent(["codex"])

    class Backend:
        def read_account_rate_limits(self, timeout=5.0):
            return {"plan_type": "plus"}

    agent._backends = {"codex": Backend()}

    with pytest.raises(RuntimeError, match="codex_rate_limits_unavailable"):
        await agent._build_codex_quota_observation("2026-11-15T12:00:00Z")
