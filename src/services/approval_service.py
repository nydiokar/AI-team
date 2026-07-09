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

import logging
import os
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.services.session_service import CommandResult
from src.services.workflow_service import WorkflowService

logger = logging.getLogger(__name__)


def _flow_drive_enabled() -> bool:
    """Whether the Work Control Substrate write path is active (HARNESS_FLOW_DRIVE).

    Mirrors Orchestrator._harness_flow_drive_enabled; default OFF ⇒ the approval
    gate behaves byte-identically to before this seam existed."""
    return os.environ.get("HARNESS_FLOW_DRIVE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )

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
        # [A29] Record the approval on the case substrate (deferred A26 seam):
        # link approval→flow + append `approval.requested`. Best-effort/isolated
        # and flag-guarded — it can NEVER affect the approval gate itself.
        self._record_approval_flow(
            approval_id=approval_id, task_id=task_id,
            event_type="approval.requested", actor="system",
            to_state="pending",
        )
        # The id is what the caller needs; carry it in reason ONLY here where it
        # is an identifier, not an error (documented exception).
        return CommandResult(True, reason=approval_id)

    # -- substrate seam (A29) ---------------------------------------------

    def _record_approval_flow(
        self,
        *,
        approval_id: str,
        task_id: Optional[str],
        event_type: str,
        actor: str,
        to_state: Optional[str] = None,
    ) -> None:
        """Best-effort Work-substrate record for an approval lifecycle change.

        Links the approval to the case that owns its task (via the task's
        flow_run) and appends an append-only case event. Flag-guarded (no-op when
        HARNESS_FLOW_DRIVE is OFF) and fully isolated: any failure logs and
        returns — an approval must resolve/persist regardless. SHADOW only —
        nothing reads these rows to drive execution. Missing task/flow ⇒ no-op
        (never inferred)."""
        try:
            if not _flow_drive_enabled() or not task_id:
                return
            runs = self._db.list_flow_runs(task_id=task_id, limit=1)
            if not runs:
                return
            flow_run_id = runs[0].get("flow_run_id")
            if not flow_run_id:
                return
            # Idempotent link (unique-keyed); safe to repeat across request/resolve.
            self._db.create_flow_link(
                flow_run_id, "approval", approval_id, "approval",
                created_by="system",
            )
            self._db.append_flow_event(
                flow_run_id, event_type, actor,
                to_state=to_state, entity_type="approval", entity_id=approval_id,
            )
        except Exception as e:
            logger.warning(
                "event=approval_flow_record_failed approval_id=%s type=%s err=%s",
                approval_id, event_type, e,
            )

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

        # [A29] Append the terminal `approval.resolved` case event (deferred A26
        # seam). Actor is the operator who decided; to_state carries the decision.
        # Best-effort/isolated/flag-guarded — never affects the gate.
        self._record_approval_flow(
            approval_id=approval_id, task_id=existing.get("task_id"),
            event_type="approval.resolved", actor="operator",
            to_state=decision,
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
