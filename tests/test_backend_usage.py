"""Backend Account + Usage Visibility (#30/#33) tests.

Asserts the honesty contract: provable facts are surfaced; limits/reset/identity
are always null + a reason; usage is never fabricated when absent.
"""
import types

import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.services.backend_usage import build_backend_usage


def _cfg():
    return types.SimpleNamespace(
        claude=types.SimpleNamespace(default_model="sonnet"),
        codex=types.SimpleNamespace(default_model=None),
        opencode=types.SimpleNamespace(default_model="opencode/big-pickle"),
    )


class _FakeTS:
    def __init__(self, by_backend):
        self._by = by_backend

    def list_turns(self, backend=None, limit=200):
        return list(self._by.get(backend, []))


def test_no_telemetry_reports_registry_facts_only():
    v = build_backend_usage(_cfg(), valid_backends=["claude", "codex"], telemetry_store=None)
    assert v["telemetry_available"] is False
    assert v["limits_source"] is None
    claude = next(b for b in v["backends"] if b["backend"] == "claude")
    assert claude["configured_model"] == "sonnet"
    assert claude["usage_coverage"] == "telemetry_unavailable"


def test_limits_and_identity_are_always_null_with_reason():
    # Even WITH telemetry + usage, no backend proves a limit or account identity.
    ts = _FakeTS({"claude": [{"observed_models": ["claude-x"], "metrics": {"input_tokens": 5}}]})
    v = build_backend_usage(_cfg(), valid_backends=["claude"], telemetry_store=ts)
    claude = v["backends"][0]
    assert claude["daily_limit"] is None
    assert claude["weekly_limit"] is None
    assert claude["limit_reset_at"] is None
    assert claude["limit_reason"] == "no_backend_limit_source"
    assert claude["account_identity"] is None
    assert claude["account_identity_reason"] == "no_backend_identity_source"


def test_usage_summed_from_turns():
    ts = _FakeTS({
        "claude": [
            {"observed_models": ["m1"], "requested_model": "sonnet",
             "metrics": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}},
            {"observed_models": ["m1"], "metrics": {"input_tokens": 10, "output_tokens": 5}},
        ]
    })
    v = build_backend_usage(_cfg(), valid_backends=["claude"], telemetry_store=ts)
    claude = v["backends"][0]
    assert claude["recent_turn_count"] == 2
    assert claude["recent_usage"]["input_tokens"] == 110
    assert claude["recent_usage"]["output_tokens"] == 55
    assert claude["usage_coverage"] == "observed"
    assert "m1" in claude["observed_models"] and "sonnet" in claude["observed_models"]


def test_no_turns_gives_null_usage_not_zero():
    ts = _FakeTS({})  # no turns for anyone
    v = build_backend_usage(_cfg(), valid_backends=["codex"], telemetry_store=ts)
    codex = v["backends"][0]
    assert codex["recent_usage"] is None          # NOT {} or 0
    assert codex["recent_turn_count"] == 0
    assert codex["usage_coverage"] == "no_data"


def test_turns_without_usage_fields_are_honest():
    ts = _FakeTS({"claude": [{"observed_models": ["m1"], "metrics": {}}]})
    v = build_backend_usage(_cfg(), valid_backends=["claude"], telemetry_store=ts)
    claude = v["backends"][0]
    assert claude["recent_usage"] is None
    assert claude["usage_coverage"] == "usage_fields_absent"


def test_list_turns_failure_is_survived():
    class _Boom:
        def list_turns(self, backend=None, limit=200):
            raise RuntimeError("db down")

    v = build_backend_usage(_cfg(), valid_backends=["claude"], telemetry_store=_Boom())
    # Falls back to registry facts without raising.
    assert v["backends"][0]["usage_coverage"] == "no_data"


# --- API ---

TOKEN = "test-usage-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    monkeypatch.setattr(control_api, "_telemetry_store", lambda: None)
    return TestClient(control_api.build_control_api(types.SimpleNamespace()))


def test_usage_endpoint_requires_auth(client):
    r = client.get("/api/backends/usage")
    assert r.status_code in (401, 403)


def test_usage_endpoint_shape(client):
    r = client.get("/api/backends/usage", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    body = r.json()
    assert "backends" in body and isinstance(body["backends"], list)
    assert body["limits_source"] is None
    # Every backend row carries the honesty fields.
    for b in body["backends"]:
        assert "limit_reason" in b and b["daily_limit"] is None
        assert "account_identity" in b
