"""[M3 A34] Session wiring for the Manager MCP tools (claude_driver).

HERMETIC: no SDK boot, no subprocess, no network. Exercises the pure allowed-tool
assembly (`_session_allowed_tools`) + the `_mcp_manager_configured` predicate.

Invariants proved:
  1. No role ⇒ no manager tools, even when ~/.claude.json has the manager server.
  2. Role-boot Manager gets the complete Manager MCP surface, defaults preserved;
     without the manager server it drops to least-privilege.
  3. The jobs grant is independent and unchanged by the manager wiring.

Run: `pytest tests/test_claude_driver_manager_tools.py -q` (plain pytest — cost-guard clean).
"""
from __future__ import annotations

import json

import pytest

from src.backends import claude_driver as cd
from src.backends.claude_role_adapter import base_tool_names, manager_tool_names


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _write_claude_json(tmp_path, monkeypatch, servers: dict) -> None:
    """Point Path.home() at tmp_path and write a ~/.claude.json with mcpServers."""
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8")
    monkeypatch.setattr(cd.Path, "home", staticmethod(lambda: tmp_path))


_LEGACY_MANAGER_TOOLS = {
    "mcp__manager__dispatch_worker",
    "mcp__manager__wait_for_worker",
}

_ROLE_MANAGER_TOOLS = {
    "mcp__manager__list_nodes",
    "mcp__manager__dispatch_worker",
    "mcp__manager__wait_for_worker",
    "mcp__manager__whoami",
    "mcp__manager__check_inbox",
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


# --------------------------------------------------------------------------- #
# Assembly — per-role grant
# --------------------------------------------------------------------------- #

def test_default_has_no_manager_tools(monkeypatch):
    """No role + no manager server ⇒ exactly _DEFAULT_TOOLS (+jobs off)."""
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: False)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: False)
    tools = cd._session_allowed_tools()
    assert tools == list(cd._DEFAULT_TOOLS)


def test_roleless_session_gets_no_manager_tools_even_when_configured(monkeypatch):
    """Server present but no role ⇒ NO manager tools (grants are per-role only)."""
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: False)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: True)
    assert not (_LEGACY_MANAGER_TOOLS & set(cd._session_allowed_tools()))


def test_role_manager_grants_complete_manager_surface(monkeypatch):
    """Role-boot Manager gets every tool advertised by the current Manager role."""
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: False)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: True)
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
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: True)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: False)
    tools = cd._session_allowed_tools()
    assert "mcp__jobs__watch_job" in tools
    assert not (_LEGACY_MANAGER_TOOLS & set(tools))


# --------------------------------------------------------------------------- #
# Base (universal) tool profile — every role gets it; CI-pinned to the jobs server
# --------------------------------------------------------------------------- #

def test_base_grant_matches_advertised_jobs_surface():
    """base_v1 must grant EXACTLY the jobs server's advertised tools — a new jobs
    tool that should be universal cannot be forgotten (and a privileged one must
    not be smuggled onto the universal floor)."""
    from scripts import mcp_jobs

    advertised = {f"mcp__jobs__{tool['name']}" for tool in mcp_jobs._TOOLS}
    assert set(base_tool_names()) == advertised


def test_base_tools_reach_every_role(monkeypatch):
    """The universal base tools are granted to manager, worker, and any future/
    unmapped role alike (when the jobs server is configured)."""
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: True)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: True)
    for role in ("manager", "worker", "scientific_governor", "some_future_advisory", None):
        tools = cd._session_allowed_tools(role=role)
        assert set(base_tool_names()) <= set(tools), f"base tools missing for role={role!r}"


def test_unmapped_role_is_least_privilege(monkeypatch):
    """An unknown/new role resolves to base-only — it can NEVER inherit the manager
    surface just by existing (least-privilege default in the role→profile registry)."""
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: True)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: True)
    tools = set(cd._session_allowed_tools(role="some_future_advisory"))
    assert not (set(manager_tool_names()) & tools)
    assert set(base_tool_names()) <= tools


# --------------------------------------------------------------------------- #
# A108 — dynamic tool-profile overlay flows through the driver grant end-to-end
# --------------------------------------------------------------------------- #

def _set_dynamic(monkeypatch, *, dynamic, overlay=""):
    import src.control.db as _db
    monkeypatch.setattr(_db, "runtime_flag_enabled",
                        lambda name: dynamic if name == "TOOL_PROFILES_DYNAMIC" else False)
    monkeypatch.setattr(_db, "runtime_flag_value",
                        lambda name: overlay if name == "TOOL_PROFILES_OVERLAY" else None)


def test_overlay_grants_extra_manager_tool_through_driver(monkeypatch):
    """With the gate ON, an overlay-added manager tool reaches the Manager session's
    allowed_tools — no code/redeploy, just the controller value."""
    import json
    from src.backends.claude_role_adapter import static_profile_tools
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: True)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: True)
    _set_dynamic(monkeypatch, dynamic=True, overlay=json.dumps(
        {"profiles": {"manager_v1": static_profile_tools()["manager_v1"] + ["mcp__manager__brand_new"]}}))
    tools = cd._session_allowed_tools(role="manager")
    assert "mcp__manager__brand_new" in tools


def test_gate_off_driver_grant_is_static(monkeypatch):
    """Gate OFF ⇒ the driver grant is exactly the static surface (byte-identical)."""
    import json
    monkeypatch.setattr(cd, "_mcp_jobs_configured", lambda: True)
    monkeypatch.setattr(cd, "_mcp_manager_configured", lambda: True)
    _set_dynamic(monkeypatch, dynamic=False, overlay=json.dumps(
        {"profiles": {"manager_v1": ["mcp__manager__should_be_ignored"]}}))
    tools = cd._session_allowed_tools(role="manager")
    assert "mcp__manager__should_be_ignored" not in tools
    assert _ROLE_MANAGER_TOOLS <= set(tools)
