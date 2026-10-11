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

# --- Universal (base) tool profile -----------------------------------------
#
# Tools EVERY role gets, regardless of role or subtype — the floor of the grant.
# A role's own ``tool_profile`` is layered ON TOP of this (see
# ``tool_profile_for_role`` + ``ROLE_TOOL_PROFILES`` below). This is the single
# provider-neutral home for "a capability every agent should have" (identity,
# watched jobs): add the tool name to the base profile's list in the backend
# adapter ONCE and every current and future role gets it — no role has to opt in,
# and no backend file is edited per new role. Backends that gate MCP access at the
# SERVER level (Codex, OpenCode) ignore this list entirely; only Claude, which
# allowlists per tool, consumes it. Keep it to genuinely universal, low-authority
# tools — anything role-specific belongs in that role's profile, not here.
BASE_TOOL_PROFILE: str = "base_v1"

# --- Manager role constants ------------------------------------------------

MANAGER_ROLE_ID: str = "manager"
MANAGER_TOOL_PROFILE: str = "manager_v1"

# Skill boundaries recorded for later (§Layer 2). M3.1 builds NO generic skill
# loader — the first loop's procedure is inlined in the role prompt. These names
# are role metadata only; executable procedures live as native project skills in
# `.claude/skills/<skill>/SKILL.md` (loaded by role sessions via setting_sources).
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
#
# manager.md defines *who the Manager is* and its authority contract — the stable
# identity loaded once when a Manager session boots. It deliberately contains NO
# current objective, Case/Task, branch/date, or provider config: that arrives
# per-invocation as ManagerInvocation / the first assignment turn (see below),
# never baked into the file. The file is loaded VERBATIM (read_text().strip(),
# no markdown parsing) and appended onto the Claude Code system prompt preset by
# claude_role_adapter.claude_system_prompt() — so anything written in manager.md
# is live model context on every boot. Keep it free of engineering meta-doc
# (this comment is where that belongs instead) and of anything that reads as an
# instruction only a human operator should carry out.
_MANAGER_ROLE_DOC: Path = (
    Path(__file__).resolve().parents[2] / "docs" / "harness" / "roles" / "manager.md"
)

# --- Worker role constants -------------------------------------------------

WORKER_ROLE_ID: str = "worker"
WORKER_TOOL_PROFILE: str = "worker_v1"

# A Worker declares NO harness skills yet — its one-task procedure is inlined in
# the role prompt (worker.md), exactly like the Manager's first loop. This list
# is intentionally EMPTY rather than aspirational; shared procedures reach workers as
# native project skills in `.claude/skills/`, not through this list.
WORKER_SKILLS: List[str] = []

# worker.md mirrors manager.md: the concrete task arrives per-dispatch as the
# dispatch envelope (the first assignment turn), never in this file. Loaded
# verbatim, gated by MANAGER_ROLE_ENABLED + the opt-in role_boot='worker' signal
# threaded from dispatch_worker(role='worker'). Same "no engineering meta-doc,
# no human-only instructions" rule as manager.md above.
_WORKER_ROLE_DOC: Path = (
    Path(__file__).resolve().parents[2] / "docs" / "harness" / "roles" / "worker.md"
)


# --- Scientific Governor role constants ------------------------------------
#
# The Scientific Governor reviews a manager's milestone-completion proposal for a
# research programme (PAST/PRESENT/FUTURE continuity + a six-question validity
# protocol). It is NOT a third live-boot tier: the harness `_role_boot` path only
# promotes `manager` (case_role) and `worker` (role_boot='worker') sessions.
# A governor runs as a WORKER whose stable stance is this role doc, delivered in the
# manager-composed dispatch envelope (see docs/harness/scientific_governor_contract.md).
# This loader makes the role first-class + discoverable (same seam as the Worker
# role) without touching that gating.
SCIENTIFIC_GOVERNOR_ROLE_ID: str = "scientific_governor"
SCIENTIFIC_GOVERNOR_TOOL_PROFILE: str = WORKER_TOOL_PROFILE
SCIENTIFIC_GOVERNOR_SKILLS: List[str] = []

_SCIENTIFIC_GOVERNOR_ROLE_DOC: Path = (
    Path(__file__).resolve().parents[2]
    / "docs" / "harness" / "roles" / "scientific_governor.md"
)


# --- Role → tool-profile registry ------------------------------------------
#
# The ONE provider-neutral place that maps a role id to the (non-base) tool
# profile it layers on top of BASE_TOOL_PROFILE. A new role or subtype is a single
# entry here (plus its profile's concrete tool list in the backend adapter) — the
# backend grant logic reads this map and never needs a per-role branch. An unknown
# or unmapped role resolves to the least-privilege WORKER_TOOL_PROFILE, so a new
# role can NEVER accidentally inherit a more powerful grant (e.g. the manager
# surface) just by existing: it gets base + worker (empty) until it is deliberately
# given its own profile here.
ROLE_TOOL_PROFILES: dict[str, str] = {
    MANAGER_ROLE_ID: MANAGER_TOOL_PROFILE,
    WORKER_ROLE_ID: WORKER_TOOL_PROFILE,
    SCIENTIFIC_GOVERNOR_ROLE_ID: SCIENTIFIC_GOVERNOR_TOOL_PROFILE,
}


def tool_profile_for_role(role_id: Optional[str]) -> str:
    """The (non-base) tool profile for ``role_id`` — least-privilege by default.

    An unknown/unmapped/absent role resolves to ``WORKER_TOOL_PROFILE`` (base only),
    never to a privileged profile. This is the single lookup the backend grant uses
    so adding a role is one ``ROLE_TOOL_PROFILES`` entry, not a backend code change.
    """
    return ROLE_TOOL_PROFILES.get(role_id or "", WORKER_TOOL_PROFILE)


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
    session_id: Optional[str] = None
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


def load_worker_role() -> AgentRoleDefinition:
    """Load the canonical Worker role from ``docs/harness/roles/worker.md``.

    Mirrors :func:`load_manager_role` exactly (same two-branch guard). Raises
    :class:`FileNotFoundError` with a clear message if the artifact is missing OR
    empty — a Worker must never boot with an empty identity.
    """
    if not _WORKER_ROLE_DOC.is_file():
        raise FileNotFoundError(
            f"Worker role profile not found at {_WORKER_ROLE_DOC}; cannot boot a Worker session."
        )
    instructions: str = _WORKER_ROLE_DOC.read_text(encoding="utf-8").strip()
    if not instructions:
        raise FileNotFoundError(
            f"Worker role profile at {_WORKER_ROLE_DOC} is empty; cannot boot a Worker session."
        )
    return AgentRoleDefinition(
        role_id=WORKER_ROLE_ID,
        system_instructions=instructions,
        declared_skills=list(WORKER_SKILLS),
        tool_profile=WORKER_TOOL_PROFILE,
        # Honest description, not a Pydantic model name: no WorkerReport type
        # exists. See the OUTPUT CONTRACT section of worker.md.
        output_contract="worker-report: what was done / skipped / failed, with commit SHAs",
    )


def load_scientific_governor_role() -> AgentRoleDefinition:
    """Load the canonical Scientific Governor role from
    ``docs/harness/roles/scientific_governor.md``.

    Mirrors :func:`load_worker_role` exactly (same two-branch missing/empty guard).
    The governor runs on the ``worker_v1`` tool profile — it reviews and reports, it
    does not dispatch, merge, or close — so it needs no manager grant. Raises
    :class:`FileNotFoundError` if the artifact is missing or empty.
    """
    if not _SCIENTIFIC_GOVERNOR_ROLE_DOC.is_file():
        raise FileNotFoundError(
            f"Scientific Governor role profile not found at {_SCIENTIFIC_GOVERNOR_ROLE_DOC}; "
            "cannot boot a Scientific Governor review."
        )
    instructions: str = _SCIENTIFIC_GOVERNOR_ROLE_DOC.read_text(encoding="utf-8").strip()
    if not instructions:
        raise FileNotFoundError(
            f"Scientific Governor role profile at {_SCIENTIFIC_GOVERNOR_ROLE_DOC} is empty; "
            "cannot boot a Scientific Governor review."
        )
    return AgentRoleDefinition(
        role_id=SCIENTIFIC_GOVERNOR_ROLE_ID,
        system_instructions=instructions,
        declared_skills=list(SCIENTIFIC_GOVERNOR_SKILLS),
        tool_profile=SCIENTIFIC_GOVERNOR_TOOL_PROFILE,
        # Honest description, not a Pydantic model name: the review output is a
        # structured prose packet (six answers, severity-tagged findings, one
        # decision + cheapest-useful next action), not a typed model.
        output_contract=(
            "governor-review: six-question answers, findings tagged "
            "BLOCKER|MATERIAL|RESIDUAL|OPPORTUNITY, one decision "
            "(accept|accept-with-qualifications|request-material-repair|reject), "
            "cheapest-useful next action"
        ),
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
    if inv.session_id:
        lines.append(f"Your session: {inv.session_id}")
    if inv.branch:
        lines.append(f"Working branch context: {inv.branch}")
    if inv.context_refs:
        lines.append("Context references: " + ", ".join(inv.context_refs))
    lines.append(f"Trigger: {inv.trigger}")
    lines.append("Allowed decisions: " + ", ".join(inv.allowed_decisions))
    lines.append(
        "Ground the objective in code and git first, then decide your first move."
    )
    if inv.session_id:
        lines.append(
            "This session is persistent: it stays alive after you close a Case. When this "
            f"Case is done you can open the next objective in THIS SAME session with "
            f"open_case(objective=..., session_id='{inv.session_id}', completion_criteria=...) "
            "— do NOT expect a fresh session per Case."
        )
    return "\n".join(lines)
