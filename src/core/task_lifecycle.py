"""Task lifecycle derivation (Move G′) — the supervised UI lifecycle on top of
the raw mesh dispatch status.

The mesh ``mesh_tasks.status`` set is operational
(pending|claimed|processing|completed|failed|failed_node_offline|cancelled): it
tracks *the dispatch queue*, not whether the agent is waiting on a human. The Web
UI's Tasks inbox needs the **supervised** lifecycle — in particular the two states
the flat mesh status can never reach on its own:

  * ``waiting_for_input``    — the owning session is AWAITING_INPUT;
  * ``waiting_for_approval`` — gated on Move H (no live source yet; named only).

So derivation takes BOTH the mesh status and the owning session's status; the
session status overlays the queue status. This module is PURE (no I/O) so it is
unit-testable and has exactly one home for the mapping (frontend
``taskAdapter.deriveTaskState`` is the mirror of the mesh-only half).

The returned state strings match the frontend ``TaskState`` union
(web/src/domain/status.ts) EXACTLY — that contract is the reason this is a
shared vocabulary, not a private enum.
"""
from __future__ import annotations

from typing import Optional

# Canonical UI task states (mirror web/src/domain/status.ts TaskState).
QUEUED = "queued"
DISPATCHING = "dispatching"
RUNNING = "running"
WAITING_FOR_INPUT = "waiting_for_input"
WAITING_FOR_APPROVAL = "waiting_for_approval"  # gated on Move H — not derived live
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
CONNECTION_UNKNOWN = "connection_unknown"

# UI section names (mirror the frontend Tasks screen sections).
SECTION_ATTENTION = "attention"
SECTION_RUNNING = "running"
SECTION_QUEUED = "queued"
SECTION_FAILED = "failed"
SECTION_RECENT = "recent"

# Mesh dispatch status → base UI state (the session-independent half). This is
# the same mapping the frontend taskAdapter.deriveTaskState encodes; keep them
# in sync — a divergence is a bug, not a feature.
_MESH_STATE = {
    "pending": QUEUED,
    "claimed": DISPATCHING,
    "processing": RUNNING,
    "completed": SUCCEEDED,
    "failed": FAILED,
    "failed_node_offline": FAILED,
    "cancelled": CANCELLED,
}

# Terminal states never get overlaid by a (stale) session status — a completed
# task is completed even if its session later goes AWAITING_INPUT for the NEXT turn.
_TERMINAL = {SUCCEEDED, FAILED, CANCELLED}


def derive_task_state(
    mesh_status: str,
    session_status: Optional[str] = None,
) -> str:
    """Canonical UI task state from the mesh status, overlaid by session status.

    ``session_status`` is the owning session's SessionStatus value
    (idle|busy|awaiting_input|error|...), or None for run_oneoff tasks with no
    session. The overlay only applies to a still-active task: when the queue says
    the task is in flight (running/dispatching/queued) but the session is
    AWAITING_INPUT, the supervised truth is ``waiting_for_input``.
    """
    base = _MESH_STATE.get(mesh_status, CONNECTION_UNKNOWN)
    if base in _TERMINAL:
        return base
    # Active task: let the session status reveal the supervised state.
    if session_status == "awaiting_input":
        return WAITING_FOR_INPUT
    if session_status == "error":
        return FAILED
    return base


def section_for_state(state: str) -> str:
    """Map a canonical UI state to its Tasks-inbox section.

    "Attention" is reserved for work that is genuinely BLOCKED on a human and is
    still actionable from the inbox (waiting for input/approval, or a stale
    connection that may resume). A FAILED task is terminal — it isn't waiting on
    you, it's done badly — so it gets its OWN section, separate from the act-now
    queue, instead of permanently bloating attention with dead-ends.
    """
    if state in (WAITING_FOR_INPUT, WAITING_FOR_APPROVAL, CONNECTION_UNKNOWN):
        return SECTION_ATTENTION
    if state in (RUNNING, DISPATCHING):
        return SECTION_RUNNING
    if state == QUEUED:
        return SECTION_QUEUED
    if state == FAILED:
        return SECTION_FAILED
    # succeeded / cancelled → recently completed
    return SECTION_RECENT
