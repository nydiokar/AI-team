"""Approval gate (Move H) — the consumer M4 was missing.

M4 (``WorkflowService``) *emits* ``approval.requested``/``approval.granted`` but
nothing waited on them. This service is the missing consumer: it turns an
approval into a **durable record with a state machine**
(``pending → approved | rejected | expired``) and makes resolution the thing that
*triggers* the gated action.

Why durable, not a blocked coroutine
------------------------------------
The tempting design — ``await asyncio.Event()`` inside the dispatch path until a
human clicks — is wrong twice: it pins a worker slot for the entire human-response
window, and the in-memory event evaporates if the gateway restarts mid-wait (the
approval would be silently lost, violating the restart-resilience invariant). So
"blocks on the decision" is **logical**: the gated action is recorded pending and
NOT dispatched; ``resolve(approved)`` is what runs it. The pending row lives in
SQLite, so it survives restart and the pending queue rebuilds itself.

Separation of concerns
----------------------
- The OBJECT/QUEUE (state) lives in ``db.py`` (Move H table) — this service wraps it.
- The EVENTS stay on the stateless ``WorkflowService`` (CONTROL_CONTRACT §7:
  workflow steps emit events + call services, they don't own state). This service
  CALLS ``WorkflowService.approval_requested/approval_granted`` — it does not
  re-implement them.
- The DISPATCH callback is INJECTED (no orchestrator import) so there is no import
  cycle; the orchestrator/control layer wires "on approve, do X".
"""
from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.services.session_service import CommandResult
from src.services.workflow_service import WorkflowService

# Called when an approval is APPROVED, with the approval row. May be sync or
# async; the resolver awaits it if it returns an awaitable. Optional — a service
# wired without one is a pure queue (the demo/queue case).
OnApprove = Callable[[Dict[str, Any]], Optional[Awaitable[None]]]


class ApprovalService:
    """Durable approval gate over the ``approvals`` table + workflow events."""

    def __init__(
        self,
        db,
        workflow: Optional[WorkflowService] = None,
        on_approve: Optional[OnApprove] = None,
    ) -> None:
        self._db = db
        self._workflow = workflow or WorkflowService()
        self._on_approve = on_approve

    # -- request -----------------------------------------------------------

    def request(
        self,
        *,
        action: str,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        risk: str = "medium",
        reversible: bool = True,
        requested_by: str = "",
        payload: Optional[Dict[str, Any]] = None,
        expires_at: Optional[str] = None,
    ) -> CommandResult:
        """Record a pending approval and emit ``approval.requested``.

        Returns CommandResult(ok, reason) with the new id in ``reason`` is wrong —
        ``reason`` is for codes; the id rides on the returned approval dict via
        ``get``. Callers that need the id read it from the emitted event or the
        pending list. We surface it on the result's session-free envelope by
        returning the id in a dedicated attribute through ``get`` after create.
        """
        if not action:
            return CommandResult(False, reason="missing_action")
        approval_id = f"appr_{uuid.uuid4().hex[:12]}"
        self._db.create_approval(
            approval_id=approval_id,
            action=action,
            session_id=session_id,
            task_id=task_id,
            risk=risk,
            reversible=reversible,
            requested_by=requested_by,
            payload=payload,
            expires_at=expires_at,
        )
        # Emit the M4 event via the stateless workflow service (one source of
        # truth for the event name + correlation).
        if session_id:
            self._workflow.approval_requested(
                session_id=session_id, action=action,
                task_id=task_id, requested_by=requested_by,
            )
        # The id is what the caller needs; carry it in reason ONLY here where it
        # is an identifier, not an error (documented exception).
        return CommandResult(True, reason=approval_id)

    # -- resolve -----------------------------------------------------------

    async def resolve(
        self, approval_id: str, decision: str, resolved_by: str = ""
    ) -> CommandResult:
        """Approve or reject a pending approval.

        ``decision`` ∈ {"approved","rejected"}. The DB transition is guarded
        (only pending → terminal), so a concurrent double-resolve yields exactly
        one winner; the loser gets ``already_resolved``. On approve, the injected
        ``on_approve`` callback fires (this is the "trigger the gated action"
        step) AFTER the state is committed, so a crash between commit and dispatch
        leaves a recoverable approved-but-not-dispatched row rather than a
        dispatched-but-pending one.
        """
        if decision not in ("approved", "rejected"):
            return CommandResult(False, reason="invalid_decision")
        existing = self._db.get_approval(approval_id)
        if existing is None:
            return CommandResult(False, reason="not_found")
        if existing.get("status") != "pending":
            return CommandResult(False, reason="already_resolved")

        won = self._db.resolve_approval(approval_id, decision, resolved_by)
        if not won:
            # Lost a race with a concurrent resolve.
            return CommandResult(False, reason="already_resolved")

        # Emit approval.granted (granted=False for a rejection) via the workflow
        # service — same event name M4 declared, decision carried as `granted`.
        session_id = existing.get("session_id")
        if session_id:
            self._workflow.approval_granted(
                session_id=session_id,
                action=existing.get("action", ""),
                granted=(decision == "approved"),
                task_id=existing.get("task_id"),
                approver=resolved_by,
            )

        if decision == "approved" and self._on_approve is not None:
            row = self._db.get_approval(approval_id)
            result = self._on_approve(row)
            if result is not None and hasattr(result, "__await__"):
                await result

        return CommandResult(True)

    # -- reads -------------------------------------------------------------

    def pending(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._db.list_approvals(status="pending", limit=limit)

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        return self._db.list_approvals(status=status, limit=limit)

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        return self._db.get_approval(approval_id)
