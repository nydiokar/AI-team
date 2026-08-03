"""[M3 A34] Session wiring for the Manager MCP tools (claude_driver).

HERMETIC: no SDK boot, no subprocess, no network. Exercises the pure, double-gated
allowed-tool assembly (`_session_allowed_tools`) + the two gate predicates
(`_mcp_manager_configured`, `_manager_tools_enabled`).

Invariants proved:
  1. Default (flag OFF, no manager server) ⇒ byte-identical: no manager tools.
  2. Env flag OFF alone suppresses the grant even when ~/.claude.json has it.
  3. ~/.claude.json missing the manager server suppresses the grant even with the flag ON.
  4. Legacy Manager-tools gate grants its original two tools; role-boot grants the complete
     Manager MCP surface, defaults preserved.
  5. The jobs grant is independent and unchanged by the manager wiring.

Run: `pytest tests/test_claude_driver_manager_tools.py -q` (plain pytest — cost-guard clean).
"""
from __future__ import annotations

import json

import pytest

from src.backends import claude_driver as cd
from src.backends.claude_role_adapter import manager_tool_names


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _write_claude_json(tmp_path, monkeypatch, servers: dict) -> None:
    """Point Path.home() at tmp_path and write a ~/.claude.json with mcpServers."""
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8")
    monkeypatch.setattr(cd.Path, "home", staticmethod(lambda: tmp_path))


@pytest.fixture(autouse=True)
def _clean_flag(monkeypatch):
    # These tests exercise the LEGACY (A34) grant path — MANAGER_ROLE_ENABLED OFF.
    # Clear both flags so the file is deterministic regardless of the ambient .env
    # (which may carry MANAGER_ROLE_ENABLED=1 when the live role path is enabled).
    monkeypatch.delenv("MANAGER_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("MANAGER_ROLE_ENABLED", raising=False)
    yield


_LEGACY_MANAGER_TOOLS = {
    "mcp__manager__dispatch_worker",
    "mcp__manager__wait_for_worker",
}

_ROLE_MANAGER_TOOLS = {
    "mcp__manager__dispatch_worker",
    "mcp__manager__wait_for_worker",
    "mcp__manager__open_case",
    "mcp__manager__get_case",
    "mcp__manager__get_case_brief",
    "mcp__manager__read_session_history",
    "mcp__manager__close_case",
    "mcp__manager__record_review",
    "mcp__manager__reconcile_waits",
    "mcp__manager__arm_wait_group",
    "mcp__manager__publish_spec",
    "mcp__manager__publish_artifact",
    "mcp__manager__record_spec_review",
    "mcp__manager__decompose_case",
    "mcp__manager__release_worker",
}


# --------------------------------------------------------------------------- #
# Gate predicates
# --------------------------------------------------------------------------- #

def test_manager_configured_true_when_present(tmp_path, monkeypatch):
    _write_claude_json(tmp_path, monkeypatch, {"manager": {"command": "python"}})
    assert cd._mcp_manager_configured() is True


def test_manager_configured_false_when_absent(tmp_path, monkeypatch):
    _write_claude_json(tmp_path, monkeypatch, {"jobs": {"command": "python"}})
    assert cd._mcp_manager_configured() is False


def test_manager_configured_false_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cd.Path, "home", staticmethod(lambda: tmp_path))  # no file written
    assert cd._mcp_manager_configured() is False


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("nope", False),
])
def test_manager_tools_enabled_flag_parsing(monkeypatch, val, expected):
    monkeypatch.setenv("MANAGER_TOOLS_ENABLED", val)
    assert cd._manager_tools_enabled() is expected


# --------------------------------------------------------------------------- #
# Assembly — the double gate
# --------------------------------------------------------------------------- #

def test_default_has_no_manager_tools(monkeypatch):
    """Flag OFF + no manager server ⇒ byte-identical to _DEFAULT_TOOLS (+jobs off)."""
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: False)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: False)
    tools = cd._session_allowed_tools()
    assert tools == list(cd._DEFAULT_TOOLS)


def test_flag_off_suppresses_even_when_configured(monkeypatch):
    """Server present but flag OFF ⇒ NO manager tools (env kill switch wins)."""
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: False)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: True)
    # flag not set (autouse fixture cleared it)
    assert not (_LEGACY_MANAGER_TOOLS & set(cd._session_allowed_tools()))


def test_flag_on_but_not_configured_suppresses(monkeypatch):
    """Flag ON but no manager server ⇒ NO manager tools (config gate wins)."""
    monkeypatch.setenv("MANAGER_TOOLS_ENABLED", "1")
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: False)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: False)
    assert not (_LEGACY_MANAGER_TOOLS & set(cd._session_allowed_tools()))


def test_legacy_both_gates_grant_original_manager_tools(monkeypatch):
    """The legacy opt-in remains restricted to its original two-tool grant."""
    monkeypatch.setenv("MANAGER_TOOLS_ENABLED", "1")
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: False)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: True)
    tools = cd._session_allowed_tools()
    assert _LEGACY_MANAGER_TOOLS <= set(tools)
    for t in cd._DEFAULT_TOOLS:
        assert t in tools


def test_role_manager_grants_complete_manager_surface(monkeypatch):
    """Role-boot Manager gets every tool advertised by the current Manager role."""
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: False)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: True)
    monkeypatch.setattr(cd, "_manager_role_enabled", lambda: True)
    tools = cd._session_allowed_tools(role="manager")
    assert _ROLE_MANAGER_TOOLS == set(manager_tool_names())
    assert _ROLE_MANAGER_TOOLS <= set(tools)
    for tool in cd._DEFAULT_TOOLS:
        assert tool in tools


def test_role_manager_grant_matches_advertised_mcp_surface():
    """A newly advertised Manager tool cannot become unreachable at role boot."""
    from scripts import mcp_manager

    advertised = {f"mcp__manager__{tool['name']}" for tool in mcp_manager._TOOLS}
    assert _ROLE_MANAGER_TOOLS == advertised


def test_jobs_grant_independent_of_manager(monkeypatch):
    """The jobs watch_job grant is unchanged and orthogonal to the manager gate."""
    monkeypatch.setenv("MANAGER_TOOLS_ENABLED", "1")
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: True)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: False)
    tools = cd._session_allowed_tools()
    assert "mcp__jobs__watch_job" in tools
    assert not (_LEGACY_MANAGER_TOOLS & set(tools))
