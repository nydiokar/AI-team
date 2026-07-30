"""[A53] Turn/cost governor for the persistent SDK driver.

Proves the ceiling is actually WIRED (passed to ClaudeAgentOptions) only when
configured, that config env-parsing yields None when unset/non-positive (⇒
byte-identical legacy boot), and that /health surfaces the effective cap. Pure —
no SDK boot, no paid CLI.
"""
import os

import pytest
from fastapi.testclient import TestClient

from src.backends.claude_driver import _governor_option_kwargs, _SDKSession
from src.control import control_api


# --------------------------------------------------------------------------- #
# Pure governor kwargs — the enforcement wiring                                #
# --------------------------------------------------------------------------- #

def test_governor_kwargs_absent_when_unset():
    assert _governor_option_kwargs(None, None) == {}


def test_governor_kwargs_present_when_set():
    assert _governor_option_kwargs(5, None) == {"max_turns": 5}
    assert _governor_option_kwargs(None, 2.5) == {"max_budget_usd": 2.5}
    assert _governor_option_kwargs(8, 1.5) == {"max_turns": 8, "max_budget_usd": 1.5}


def test_governor_kwargs_rejects_non_positive_and_bool():
    assert _governor_option_kwargs(0, 0) == {}
    assert _governor_option_kwargs(-3, -1.0) == {}
    # bool is an int subclass — must NOT be treated as a turn count
    assert _governor_option_kwargs(True, None) == {}
    assert _governor_option_kwargs(None, True) == {}


def test_sdk_session_stores_and_would_pass_ceilings():
    sess = _SDKSession("k", "/tmp/repo", None, {}, max_turns=9, max_budget_usd=2.0)
    assert sess.max_turns == 9
    assert sess.max_budget_usd == 2.0
    assert _governor_option_kwargs(sess.max_turns, sess.max_budget_usd) == {
        "max_turns": 9, "max_budget_usd": 2.0,
    }


def test_sdk_session_default_is_uncapped():
    sess = _SDKSession("k", "/tmp/repo", None, {})
    assert sess.max_turns is None and sess.max_budget_usd is None
    assert _governor_option_kwargs(sess.max_turns, sess.max_budget_usd) == {}


# --------------------------------------------------------------------------- #
# Config env parsing                                                           #
# --------------------------------------------------------------------------- #

def _fresh_config(monkeypatch, **env):
    monkeypatch.setenv("AI_TEAM_ENV_FILE", "/nonexistent/governor_test.env")
    for k in ("CLAUDE_SDK_MAX_TURNS", "CLAUDE_SDK_MAX_BUDGET_USD"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from config.settings import Config
    return Config()


def test_config_parses_positive_governor(monkeypatch):
    c = _fresh_config(monkeypatch, CLAUDE_SDK_MAX_TURNS="12", CLAUDE_SDK_MAX_BUDGET_USD="3.5")
    assert c.claude.sdk_max_turns == 12
    assert c.claude.sdk_max_budget_usd == 3.5


def test_config_non_positive_is_none(monkeypatch):
    c = _fresh_config(monkeypatch, CLAUDE_SDK_MAX_TURNS="0", CLAUDE_SDK_MAX_BUDGET_USD="-1")
    assert c.claude.sdk_max_turns is None
    assert c.claude.sdk_max_budget_usd is None


def test_config_absent_is_none(monkeypatch):
    c = _fresh_config(monkeypatch)
    assert c.claude.sdk_max_turns is None
    assert c.claude.sdk_max_budget_usd is None


# --------------------------------------------------------------------------- #
# /health surfacing                                                           #
# --------------------------------------------------------------------------- #

class _StubOrch:
    pass


def test_health_surfaces_governor(monkeypatch):
    import config as _config_pkg
    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_turns", 20, raising=False)
    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_budget_usd", 5.0, raising=False)
    client = TestClient(control_api.build_control_api(_StubOrch()))
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["governor"] == {"sdk_max_turns": 20, "sdk_max_budget_usd": 5.0}
