"""Provider-neutral agent-role definitions (M3 Phase 3.1 / A38).

The canonical seam between a *role* (stable identity + its declared skills, tool
profile, and output contract) and any *provider backend* that runs it. This module
imports **no** provider SDK types — a Claude adapter (or a future Codex adapter) maps
an :class:`AgentRoleDefinition` onto its backend in ``src/backends/``.

Dynamic per-invocation data (the current objective, Case id, branch, trigger) lives in
:class:`ManagerInvocation` and is delivered as the first assignment turn — it is NEVER
folded into ``system_instructions``.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# --- Manager role constants ------------------------------------------------

MANAGER_ROLE_ID: str = "manager"
MANAGER_TOOL_PROFILE: str = "manager_v1"

# Skill boundaries recorded for later (§Layer 2). M3.1 builds NO generic skill
# loader — the first loop's procedure is inlined in the role prompt. These names
# reserve the future `docs/harness/skills/<skill>/SKILL.md` packages.
MANAGER_SKILLS: List[str] = [
    "ground-and-frame",
    "open-or-decompose-case",
    "dispatch-worker",
    "supervise-or-redirect",
    "review-delivery",
    "bounded-rework",
    "close-or-derive",
]

# Decisions the Manager may return at a review gate (§decision vocabulary).
ManagerDecisionKind = Literal["close", "rework", "derive", "block", "escalate"]
MANAGER_ALLOWED_DECISIONS: List[str] = ["close", "rework", "derive", "block", "escalate"]

# Canonical role artifact, resolved relative to the repo root (this file is
# src/core/roles.py ⇒ repo root is parents[2]).
_MANAGER_ROLE_DOC: Path = (
    Path(__file__).resolve().parents[2] / "docs" / "harness" / "roles" / "manager.md"
)


class AgentRoleDefinition(BaseModel):
    """A provider-neutral role: stable identity + what it declares it needs.

    Provider adapters consume this; nothing here is Claude-specific.
    """

    role_id: str
    system_instructions: str
    declared_skills: List[str] = Field(default_factory=list)
    tool_profile: str
    output_contract: Optional[str] = None


class ManagerInvocation(BaseModel):
    """Dynamic per-invocation payload for a Manager boot — delivered as the first
    assignment turn, kept OUT of the system role."""

    case_id: str
    objective: str
    context_refs: List[str] = Field(default_factory=list)
    branch: Optional[str] = None
    trigger: str = "operator"
    allowed_decisions: List[str] = Field(default_factory=lambda: list(MANAGER_ALLOWED_DECISIONS))


class ManagerDecision(BaseModel):
    """The Manager's decision result at a review gate."""

    decision: ManagerDecisionKind
    rationale: str
    evidence: List[str] = Field(default_factory=list)


def load_manager_role() -> AgentRoleDefinition:
    """Load the canonical Manager role from ``docs/harness/roles/manager.md``.

    Raises :class:`FileNotFoundError` with a clear message if the artifact is
    missing — a Manager must never boot with an empty identity.
    """
    if not _MANAGER_ROLE_DOC.is_file():
        raise FileNotFoundError(
            f"Manager role profile not found at {_MANAGER_ROLE_DOC}; cannot boot a Manager session."
        )
    instructions: str = _MANAGER_ROLE_DOC.read_text(encoding="utf-8").strip()
    if not instructions:
        raise FileNotFoundError(
            f"Manager role profile at {_MANAGER_ROLE_DOC} is empty; cannot boot a Manager session."
        )
    return AgentRoleDefinition(
        role_id=MANAGER_ROLE_ID,
        system_instructions=instructions,
        declared_skills=list(MANAGER_SKILLS),
        tool_profile=MANAGER_TOOL_PROFILE,
        output_contract="ManagerDecision",
    )


def render_first_assignment(inv: ManagerInvocation) -> str:
    """Render the dynamic invocation as the Manager's first assignment turn.

    This is the ONLY place the objective/Case/branch/trigger enter the session —
    as a user turn, never as the system role.
    """
    lines: List[str] = [
        "You are being invoked as the Manager for a new objective.",
        f"Case: {inv.case_id}",
        f"Objective: {inv.objective}",
    ]
    if inv.branch:
        lines.append(f"Working branch context: {inv.branch}")
    if inv.context_refs:
        lines.append("Context references: " + ", ".join(inv.context_refs))
    lines.append(f"Trigger: {inv.trigger}")
    lines.append("Allowed decisions: " + ", ".join(inv.allowed_decisions))
    lines.append(
        "Ground the objective in code and git first, then decide your first move."
    )
    return "\n".join(lines)
