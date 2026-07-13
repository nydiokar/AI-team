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

from src.core.roles import AgentRoleDefinition, MANAGER_TOOL_PROFILE

# Concrete Claude Code MCP tool names granted by each declared tool profile.
# `manager_v1` = the minimum Case-aware surface the M3.1 vertical slice drives.
_PROFILE_TOOLS: Dict[str, List[str]] = {
    MANAGER_TOOL_PROFILE: [
        "mcp__manager__dispatch_worker",
        "mcp__manager__wait_for_worker",
        "mcp__manager__get_case",
        "mcp__manager__close_case",
        "mcp__manager__record_review",
    ],
}


def claude_system_prompt(role: AgentRoleDefinition) -> Dict[str, object]:
    """Build Claude's ``system_prompt`` value: the Claude Code preset with the
    role's stable instructions appended (SDK ``SystemPromptPreset`` shape)."""
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": role.system_instructions,
    }


def manager_tool_names() -> List[str]:
    """Static ``manager_v1`` tool names — no role object / file read required.

    Used by the driver's tool-assembly so scoping a manager session's grant does
    not depend on loading the role artifact.
    """
    return list(_PROFILE_TOOLS[MANAGER_TOOL_PROFILE])
