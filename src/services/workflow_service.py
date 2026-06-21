"""Transport-neutral workflow events (Cockpit M4).

Implements the **reserved workflow vocabulary** declared in
docs/CONTROL_CONTRACT.md §7 — review / handoff / approval — as the third
transport-neutral inbound entry point, beside ``SessionService`` (lifecycle) and
``orchestrator.submit_instruction`` (dispatch).

The binding rule from the contract (§7), honored here literally:

    workflow steps EMIT EVENTS (§1) and CALL EXISTING SERVICES (§4); they do
    NOT mutate state directly and do NOT require a workflow engine.

So this service is deliberately thin: each method emits one canonical event via
``observability.emit_event`` (correlated to a session/task via ``log_context``)
and returns a machine-code ``CommandResult`` — no prose, no new tables, no state
machine. A surface (Telegram, the M3 dashboard, a future approver UI) calls these
to record a workflow step; consumers render the resulting events from the stream.

``run.requested``/``run.completed`` from §7 are intentionally NOT here — they map
to the existing ``<backend>_finished`` events already emitted by the orchestrator
(§2); re-emitting them would duplicate the stream.
"""
from __future__ import annotations

from typing import Optional

from src.core import observability
from src.services.session_service import CommandResult

# The reserved vocabulary (CONTROL_CONTRACT §7). One source of truth so a typo
# can't fork the event names across surfaces.
EVENT_REVIEW_REQUESTED = "review.requested"
EVENT_REVIEW_COMPLETED = "review.completed"
EVENT_HANDOFF_CREATED = "handoff.created"
EVENT_APPROVAL_REQUESTED = "approval.requested"
EVENT_APPROVAL_GRANTED = "approval.granted"

WORKFLOW_EVENTS = frozenset({
    EVENT_REVIEW_REQUESTED, EVENT_REVIEW_COMPLETED,
    EVENT_HANDOFF_CREATED,
    EVENT_APPROVAL_REQUESTED, EVENT_APPROVAL_GRANTED,
})

# Approval outcomes carried as a field on approval.granted (granted=False = denied).
_VALID_REVIEW_VERDICTS = frozenset({"approved", "changes_requested", "rejected"})


class WorkflowService:
    """Emits the reserved workflow events. No state, no engine, no tables.

    Stateless by construction: it holds no store and mutates nothing. Every
    method correlates its event to the given ``session_id``/``task_id`` (so a
    surface can ``grep`` the workflow trail of a session) and returns a
    ``CommandResult`` whose ``reason`` is a stable code on rejection.
    """

    def review_requested(self, *, session_id: str,
                         task_id: Optional[str] = None,
                         reviewer: str = "", note: str = "") -> CommandResult:
        if not session_id:
            return CommandResult(False, reason="missing_session_id")
        self._emit(EVENT_REVIEW_REQUESTED, session_id=session_id, task_id=task_id,
                   reviewer=reviewer, note=note)
        return CommandResult(True)

    def review_completed(self, *, session_id: str,
                        verdict: str,
                        task_id: Optional[str] = None,
                        reviewer: str = "", note: str = "") -> CommandResult:
        if not session_id:
            return CommandResult(False, reason="missing_session_id")
        if verdict not in _VALID_REVIEW_VERDICTS:
            return CommandResult(False, reason="invalid_verdict")
        self._emit(EVENT_REVIEW_COMPLETED, session_id=session_id, task_id=task_id,
                   verdict=verdict, reviewer=reviewer, note=note)
        return CommandResult(True)

    def handoff_created(self, *, session_id: str,
                       to: str,
                       task_id: Optional[str] = None,
                       reason: str = "") -> CommandResult:
        if not session_id:
            return CommandResult(False, reason="missing_session_id")
        if not to:
            return CommandResult(False, reason="missing_handoff_target")
        self._emit(EVENT_HANDOFF_CREATED, session_id=session_id, task_id=task_id,
                   to=to, handoff_reason=reason)
        return CommandResult(True)

    def approval_requested(self, *, session_id: str,
                          action: str,
                          task_id: Optional[str] = None,
                          requested_by: str = "") -> CommandResult:
        if not session_id:
            return CommandResult(False, reason="missing_session_id")
        if not action:
            return CommandResult(False, reason="missing_action")
        self._emit(EVENT_APPROVAL_REQUESTED, session_id=session_id, task_id=task_id,
                   action=action, requested_by=requested_by)
        return CommandResult(True)

    def approval_granted(self, *, session_id: str,
                        action: str,
                        granted: bool = True,
                        task_id: Optional[str] = None,
                        approver: str = "") -> CommandResult:
        """Record an approval decision. ``granted=False`` records a denial on the
        same event (with ``granted: false``) so both outcomes share one name."""
        if not session_id:
            return CommandResult(False, reason="missing_session_id")
        if not action:
            return CommandResult(False, reason="missing_action")
        self._emit(EVENT_APPROVAL_GRANTED, session_id=session_id, task_id=task_id,
                   action=action, granted=bool(granted), approver=approver)
        return CommandResult(True)

    # ------------------------------------------------------------------

    @staticmethod
    def _emit(name: str, *, session_id: str, task_id: Optional[str],
              **fields) -> None:
        """Emit one workflow event correlated to the session/task.

        Uses log_context so the envelope carries session_id/task_id even though
        emit_event also takes them explicitly — keeping the correlation discipline
        identical to the orchestrator's emitters.
        """
        with observability.log_context(session_id=session_id,
                                       task_id=task_id or ""):
            observability.emit_event(name, session_id=session_id,
                                     task_id=task_id, **fields)
