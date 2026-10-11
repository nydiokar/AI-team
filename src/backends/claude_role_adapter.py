"""Claude adapter for provider-neutral roles (M3 Phase 3.1 / A38).

Maps an :class:`~src.core.roles.AgentRoleDefinition` onto Claude Code's session
options. This is the ONLY module that knows Claude-specific shapes (the
``system_prompt`` preset dict and the ``mcp__manager__*`` tool names); the role
definition itself stays provider-neutral.

Design (operator directive 2026-07-12): **preserve the Claude Code preset and
append** the role's stable instructions — do NOT replace the preset with the full
manual invocation prompt.
"""
from __future__ import annotations

from typing import Dict, List

from src.core.roles import (
    AgentRoleDefinition,
    BASE_TOOL_PROFILE,
    MANAGER_TOOL_PROFILE,
    WORKER_TOOL_PROFILE,
)

# Concrete Claude Code MCP tool names granted by each declared tool profile. This
# is the SINGLE place where a profile's abstract id resolves to Claude tool names;
# `claude_driver` composes BASE_TOOL_PROFILE + a role's own profile via
# ``profile_tool_names`` and never hard-codes a tool name itself.
#
# `base_v1`    = UNIVERSAL tools every role gets (identity + watched jobs), layered
#                under every role. Add a universal tool here ONCE and all roles get
#                it. Granted only when its server (jobs) is configured on the node.
# `manager_v1` = the complete Case-aware surface exposed by ``mcp_manager``.
# `worker_v1`  = EMPTY on purpose: a Worker gets base + the driver defaults, nothing
#                more. It must NOT hold the manager surface (no dispatch_worker /
#                open_case / close_case / record_review).
#
# Keep each list synchronized with its MCP server: naming a tool in a role prompt
# without granting it here makes the capability unreachable. The CI pin tests
# (test_claude_driver_manager_tools.py) fail the build if base_v1/manager_v1 drift
# from the advertised jobs/manager surfaces.
_PROFILE_TOOLS: Dict[str, List[str]] = {
    BASE_TOOL_PROFILE: [
        # Read-only, caller-only identity (no args, no network) — every agent may
        # ask "what is my own session id?".
        "mcp__jobs__whoami",
        # Register a long-running detached shell job (read-only to the session).
        "mcp__jobs__watch_job",
    ],
    MANAGER_TOOL_PROFILE: [
        "mcp__manager__list_nodes",
        "mcp__manager__dispatch_worker",
        "mcp__manager__wait_for_worker",
        "mcp__manager__whoami",
        "mcp__manager__check_inbox",
        "mcp__manager__open_case",
        "mcp__manager__get_case",
        "mcp__manager__read_session_history",
        "mcp__manager__close_case",
        "mcp__manager__record_review",
        "mcp__manager__reconcile_waits",
        "mcp__manager__arm_wait_group",
        "mcp__manager__get_case_brief",
        "mcp__manager__publish_spec",
        "mcp__manager__publish_artifact",
        "mcp__manager__record_spec_review",
        "mcp__manager__decompose_case",
        "mcp__manager__release_worker",
    ],
    WORKER_TOOL_PROFILE: [],
}


def claude_system_prompt(role: AgentRoleDefinition) -> Dict[str, object]:
    """Build Claude's ``system_prompt`` value: the Claude Code preset with the
    role's stable instructions appended (SDK ``SystemPromptPreset`` shape)."""
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": role.system_instructions,
    }


def profile_tool_names(profile: str) -> List[str]:
    """Concrete Claude tool names for any declared tool profile — the ONE resolver.

    Unknown profile ⇒ empty list (least privilege). This is the general seam the
    driver uses for base + role profiles alike; ``manager_tool_names`` /
    ``worker_tool_names`` / ``base_tool_names`` are thin named wrappers over it.
    """
    return list(_PROFILE_TOOLS.get(profile, []))


def base_tool_names() -> List[str]:
    """Static ``base_v1`` (universal) tool names layered under every role."""
    return profile_tool_names(BASE_TOOL_PROFILE)


def manager_tool_names() -> List[str]:
    """Static ``manager_v1`` tool names — no role object / file read required.

    Used by the driver's tool-assembly so scoping a manager session's grant does
    not depend on loading the role artifact.
    """
    return profile_tool_names(MANAGER_TOOL_PROFILE)


def worker_tool_names() -> List[str]:
    """Static ``worker_v1`` tool names — mirrors :func:`manager_tool_names`.

    Honestly EMPTY: a Worker gets base + the driver defaults, nothing more. Kept as
    an explicit resolver (not an inline ``[]``) so the worker grant has the same
    seam as the manager grant and can grow if a real need appears.
    """
    return profile_tool_names(WORKER_TOOL_PROFILE)
