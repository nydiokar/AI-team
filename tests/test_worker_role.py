"""Worker role layer — canonical layers + the opt-in role-boot tier selector.

Mirrors tests/test_manager_role.py + tests/test_claude_driver_manager_tools.py for
the NARROWER Worker role. HERMETIC: no SDK boot, no subprocess, no network, no paid
backend. Plain pytest (cost-guard clean).

Invariants proved:
  * `load_worker_role()` loads a real provider-neutral role AND raises clearly on a
    missing OR empty doc (same two-branch guard as the manager loader);
  * the Claude adapter appends the worker instructions to the preset; the worker
    tool grant is honestly EMPTY (a worker holds NO manager MCP tools);
  * THE CRITICAL ONE — `_role_boot` is BYTE-IDENTICAL for a case_role='worker'
    session WITHOUT the explicit `role_boot` opt-in (tier-0, role-less), and only
    becomes role-ful WITH the opt-in. The default path is actually exercised.

Run: `pytest tests/test_worker_role.py -q`.
"""
from __future__ import annotations

import types

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    monkeypatch.delenv("MANAGER_ROLE_ENABLED", raising=False)
    monkeypatch.delenv("MANAGER_TOOLS_ENABLED", raising=False)


def _driver():
    from src.backends import claude_driver as d
    return d.ClaudeSDKClientDriver.__new__(d.ClaudeSDKClientDriver)


# ---------------------------------------------------------------------------
# Layer 1 — provider-neutral role seam
# ---------------------------------------------------------------------------

def test_load_worker_role_is_neutral_and_stable():
    from src.core.roles import load_worker_role, WORKER_TOOL_PROFILE
    role = load_worker_role()
    assert role.role_id == "worker"
    assert role.tool_profile == WORKER_TOOL_PROFILE == "worker_v1"
    # Stable identity ONLY — no per-dispatch/transient slots leak into the role.
    for token in ("{{SPEC_OR_INTENT}}", "{{BRANCH}}", "{{DATE}}", "{{TASK}}"):
        assert token not in role.system_instructions
    assert role.system_instructions  # non-empty identity


def test_load_worker_role_raises_on_missing_doc(tmp_path, monkeypatch):
    import src.core.roles as roles
    missing = tmp_path / "nope" / "worker.md"
    monkeypatch.setattr(roles, "_WORKER_ROLE_DOC", missing)
    with pytest.raises(FileNotFoundError, match="not found"):
        roles.load_worker_role()


def test_load_worker_role_raises_on_empty_doc(tmp_path, monkeypatch):
    import src.core.roles as roles
    empty = tmp_path / "worker.md"
    empty.write_text("   \n\t\n", encoding="utf-8")  # whitespace-only ⇒ empty after strip
    monkeypatch.setattr(roles, "_WORKER_ROLE_DOC", empty)
    with pytest.raises(FileNotFoundError, match="empty"):
        roles.load_worker_role()


# ---------------------------------------------------------------------------
# Layer 6 — Claude adapter (append to preset) + honest empty tool grant
# ---------------------------------------------------------------------------

def test_claude_adapter_worker_appends_to_preset():
    from src.core.roles import load_worker_role
    from src.backends.claude_role_adapter import claude_system_prompt
    role = load_worker_role()
    sp = claude_system_prompt(role)
    assert sp == {"type": "preset", "preset": "claude_code", "append": role.system_instructions}
    assert role.system_instructions in sp["append"]


def test_worker_tool_names_is_honestly_empty():
    from src.backends.claude_role_adapter import worker_tool_names
    # A worker gets NO extra MCP grant — no manager surface, no dispatch/close/etc.
    assert worker_tool_names() == []


# ---------------------------------------------------------------------------
# Layer 6 — _session_allowed_tools: a worker never gains manager tools
# ---------------------------------------------------------------------------

def test_worker_tool_scoping_byte_identical_when_flag_off(monkeypatch):
    from src.backends import claude_driver as d
    monkeypatch.setattr(d, "_mcp_jobs_configured", lambda: False)
    # Flag OFF: role arg ignored ⇒ identical to the default list.
    assert d._session_allowed_tools(role="worker") == d._session_allowed_tools()
    assert not any("manager" in t for t in d._session_allowed_tools(role="worker"))


def test_worker_tool_scoping_grants_no_manager_tools_when_flag_on(monkeypatch):
    from src.backends import claude_driver as d
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    monkeypatch.setattr(d, "_mcp_jobs_configured", lambda: False)
    monkeypatch.setattr(d, "_mcp_manager_configured", lambda: True)
    worker = d._session_allowed_tools(role="worker")
    # Even with the manager server present + flag ON, a worker holds NO manager tools.
    assert not any("manager" in t for t in worker)
    # And its list equals the plain defaults (worker_tool_names() is empty).
    assert worker == list(d._DEFAULT_TOOLS)


# ---------------------------------------------------------------------------
# CRITICAL — _role_boot tier selector: byte-identity of the default path
# ---------------------------------------------------------------------------

def _worker_session(role_boot=None):
    # A Case-JOINED worker ALWAYS carries case_role='worker' today. `role_boot` is
    # the separate explicit opt-in; None ⇒ the legacy tier-0 default.
    return types.SimpleNamespace(session_id="w1", case_role="worker", role_boot=role_boot)


def test_role_boot_worker_default_is_byte_identical_flag_off():
    """Flag OFF ⇒ (None, None) for a case_role='worker' session (today's boot)."""
    inst = _driver()
    assert inst._role_boot(_worker_session(role_boot=None)) == (None, None)


def test_role_boot_joined_worker_stays_tier0_without_opt_in(monkeypatch):
    """THE constraint: with the master flag ON, a case_role='worker' session that
    was NOT opted-in (role_boot is None) must return the SAME role-less result as
    today — proving we do NOT auto-promote every joined worker."""
    from src.backends import claude_driver as d
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    monkeypatch.setattr(d, "_mcp_manager_configured", lambda: True)
    inst = _driver()

    default_off = inst._role_boot(_worker_session(role_boot=None))  # (flag ON here)
    # Exercise the ACTUAL default branch: case_role='worker', no role_boot.
    assert default_off == (None, None)
    # Byte-identical to a plain role-less session AND to the flag-off result.
    monkeypatch.delenv("MANAGER_ROLE_ENABLED", raising=False)
    assert inst._role_boot(_worker_session(role_boot=None)) == default_off
    plain = types.SimpleNamespace(session_id="x", case_role=None, role_boot=None)
    assert inst._role_boot(plain) == default_off


def test_role_boot_worker_roleful_only_with_explicit_opt_in(monkeypatch):
    """WITH the explicit role_boot='worker' opt-in (and the flag ON) the SAME
    session shape becomes role-ful — a preset system_prompt + a (manager-free)
    tool list. This is the only difference from the default path above."""
    from src.backends import claude_driver as d
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    monkeypatch.setattr(d, "_mcp_jobs_configured", lambda: False)
    inst = _driver()

    sp, tools = inst._role_boot(_worker_session(role_boot="worker"))
    assert sp["type"] == "preset" and sp["preset"] == "claude_code" and sp["append"]
    assert tools == list(d._DEFAULT_TOOLS)          # role-ful, but no manager tools
    assert not any("manager" in t for t in tools)


def test_role_boot_opt_in_still_gated_by_master_flag(monkeypatch):
    """Even with role_boot='worker', the master gate (MANAGER_ROLE_ENABLED) must be
    ON — OFF ⇒ (None, None), so the whole role path stays behind one flag."""
    inst = _driver()
    # flag cleared by the autouse fixture
    assert inst._role_boot(_worker_session(role_boot="worker")) == (None, None)


def test_role_boot_load_failure_falls_back_to_default(monkeypatch):
    """A worker-role load failure must log + fall back to (None, None), never block boot."""
    from src.backends import claude_driver as d
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")

    def _boom():
        raise RuntimeError("worker.md unreadable")

    monkeypatch.setattr(d, "load_worker_role", _boom)
    inst = _driver()
    assert inst._role_boot(_worker_session(role_boot="worker")) == (None, None)
