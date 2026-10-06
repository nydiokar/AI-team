"""Manager / Case routes — boot a Manager, open/close/review Cases, waits, spec/decompose,
operator controls.

Mounted by ``control_api.build_control_api``; route map: docs/backend/ARCHITECTURE.md "2b.
Manager / Case surface". Module-level helpers are looked up as ``core.<name>`` so tests that
monkeypatch ``control_api`` keep reaching these handlers.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from src.control import control_api as core
from src.control.control_api import (
    _REASON_STATUS,
    CaseCloseBody,
    CaseDecomposeBody,
    CaseInterruptBody,
    CaseOpenBody,
    CaseOperatorCloseBody,
    CaseOrphanSweepBody,
    CasePublishArtifactBody,
    CaseResumeBody,
    CaseReviewBody,
    CaseSpecBody,
    CaseSpecReviewBody,
    CaseStateBody,
    CaseWaitBody,
    CaseWaitGroupBody,
    ManagerInvokeBody,
    logger,
)


def build_router(
    orchestrator: Any,
    *,
    require_auth: Callable[..., Any],
    idem_put: Callable[..., Any],
    idem_guard: Callable[..., Any],
    idem_guard_async: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_auth)])

    @router.post("/api/manager")
    async def api_manager(
        body: ManagerInvokeBody,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        """[A38] Invoke a Manager: create a Case-owning session, open one Case, and
        deliver the objective as its first assignment. Refuses with 409 when the
        Manager-role path is disabled (MANAGER_ROLE_ENABLED OFF ⇒ new surface inert).
        Translates the Level-3 admission block to a clean 409, like /api/instructions."""
        from src.control.turn_queue import TurnQueueError
        from src.orchestrator import HarnessAdmissionBlocked

        async with idem_guard_async("manager", idempotency_key) as cached:
            if cached is not None:
                return JSONResponse(cached)

            try:
                result = await orchestrator.invoke_manager(
                    objective=body.objective,
                    repo_path=body.repo_path,
                    backend=body.backend,
                    model=body.model,
                    node_id=body.node_id or "__local__",
                    completion_criteria=body.completion_criteria,
                    context_refs=body.context_refs,
                    branch=body.branch,
                    continued_from=body.continued_from,
                    continue_inline=body.continue_inline,
                    continues=body.continues,
                )
            except HarnessAdmissionBlocked as blocked:
                raise core._harness_blocked_http(blocked)
            except TurnQueueError as refused:
                # [A82 pre-cutover rework, F2] Typed managed refusal (503 when
                # no carrier); invoke_manager already left nothing open.
                raise core._turn_queue_http(refused)

            if not result.get("ok"):
                reason = result.get("reason") or "manager_invoke_failed"
                status = 409 if reason == "manager_role_disabled" else _REASON_STATUS.get(reason, 400)
                raise HTTPException(status_code=status, detail={"ok": False, "reason": reason})

            idem_put("manager", idempotency_key, result)
            return JSONResponse(result)

    @router.post("/api/cases")
    def api_open_case(body: CaseOpenBody) -> JSONResponse:
        """[M3.3] Open a NEW Case on an existing Manager session — the seam that lets
        a single persistent Manager session run many Cases (open → dispatch → review →
        close → open the next) without spawning a fresh session each time. Gated by
        ``MANAGER_ROLE_ENABLED`` (409 when OFF ⇒ surface inert). Returns
        ``{ok, case_id}``; 404 for an unknown session, 400 on a Case-birth failure."""
        if not orchestrator._manager_role_enabled():
            raise HTTPException(status_code=409, detail={"ok": False, "reason": "manager_role_disabled"})
        session = orchestrator.session_service.store.get(body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail={"ok": False, "reason": "session_not_found"})
        case_id = orchestrator.open_case(
            body.objective,
            body.session_id,
            role=body.role or "manager",
            completion_criteria=body.completion_criteria,
            round_cap=body.round_cap,
        )
        if not case_id:
            raise HTTPException(status_code=400, detail={"ok": False, "reason": "open_case_failed"})
        return JSONResponse({"ok": True, "case_id": case_id})

    @router.post("/api/cases/{case_id}/close")
    def api_close_case(case_id: str, body: CaseCloseBody) -> JSONResponse:
        """[A38] Authoritative Case closure (A37 ``close_case``) — the Manager's
        Decision surface. Returns the structured ``{ok, closed, reason}``: a
        REFUSAL (``ok:false`` with a human ``reason`` — unmet criteria, open child
        work, pending approval, unknown case) is a normal 200 decision signal the
        Manager must act on, NOT an HTTP error. ``close_case`` never raises."""
        result = orchestrator.close_case(
            case_id,
            outcome=body.outcome,
            actor="manager",
            criteria_reconciliation=body.criteria_reconciliation,
            continuation_plan=body.continuation_plan,
            exhaustion_attestation=body.exhaustion_attestation,
            resolve_pending_approvals=body.resolve_pending_approvals,
        )
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/review")
    def api_record_review(
        case_id: str,
        body: CaseReviewBody,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        """[M3.2] Record a Manager review verdict (accepted|rework_requested|waived)
        as the canonical ``review.*`` flow_event on the Case audit trail. Gated by
        ``REVIEW_EMITTER_ENABLED``: when OFF this route returns 404 (disabled) so
        flag-OFF is byte-identical to pre-M3.2. Mirrors ``api_close_case``."""
        from src.control.db import REVIEW_VERDICT_EVENT_TYPES, review_emitter_enabled
        if not review_emitter_enabled():
            raise HTTPException(status_code=404, detail="not_found")
        if body.verdict not in REVIEW_VERDICT_EVENT_TYPES:
            raise HTTPException(
                status_code=422,
                detail={"ok": False, "reason": "invalid_verdict"},
            )
        with idem_guard("record_review", idempotency_key) as cached:
            if cached is not None:
                return JSONResponse(cached)
            try:
                result = orchestrator.record_review(
                    case_id, verdict=body.verdict, reason=body.reason,
                    task_id=body.task_id, actor="manager",
                )
            except Exception as exc:
                logger.exception(
                    "event=record_review_write_failed flow_run_id=%s task_id=%s verdict=%s",
                    case_id,
                    body.task_id,
                    body.verdict,
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "reason": "review_write_failed",
                        "retryable": True,
                        "error": str(exc)[:500],
                    },
                )
            idem_put("record_review", idempotency_key, result)
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/waits")
    def api_record_worker_wait(case_id: str, body: CaseWaitBody) -> JSONResponse:
        """[A46/M3.3] Record a durable pending-wait marker for a dispatched worker
        (``worker.wait_pending`` flow_event). Gated by ``DURABLE_RELAY_ENABLED``:
        when OFF this route returns 404 (disabled) so flag-OFF is byte-identical.
        The write itself is also flag-gated in the db layer (defence in depth)."""
        from src.control.db import durable_relay_enabled
        if not durable_relay_enabled():
            raise HTTPException(status_code=404, detail="not_found")
        result = orchestrator.record_worker_wait(
            case_id, body.task_id, timeout=body.timeout, actor="manager",
        )
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/waits/reconcile")
    def api_reconcile_worker_waits(case_id: str) -> JSONResponse:
        """[A46/M3.3] Reconcile a Case's outstanding worker waits against the durable
        ``task.finished`` events — resolve finished ones (append ``worker.wait_resolved``)
        and report still-open ones for the Manager to re-arm. Gated by
        ``DURABLE_RELAY_ENABLED`` (404 when OFF ⇒ byte-identical). Idempotent."""
        from src.control.db import durable_relay_enabled
        if not durable_relay_enabled():
            raise HTTPException(status_code=404, detail="not_found")
        result = orchestrator.reconcile_worker_waits(case_id, actor="manager")
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/wait-group")
    def api_arm_wait_group(case_id: str, body: CaseWaitGroupBody) -> JSONResponse:
        """[M3.4] Arm a Manager wait-group so the Wake-Dispatcher autonomously
        re-enters this Case when the group is satisfied. Gated by
        ``CASE_CONTINUATION_ENABLED`` (404 when OFF ⇒ byte-identical). Rejects a
        malformed condition (422) and bounds the member list (413) up front — the
        write itself is also flag-gated in the db layer (defence in depth)."""
        from src.control.db import case_continuation_enabled
        if not case_continuation_enabled():
            raise HTTPException(status_code=404, detail="not_found")
        cond = (body.condition or "ANY").upper()
        if cond not in ("ANY", "ALL", "NAMED"):
            raise HTTPException(
                status_code=422,
                detail={"ok": False, "reason": "invalid_condition"},
            )
        if not body.member_task_ids:
            raise HTTPException(
                status_code=422,
                detail={"ok": False, "reason": "empty_member_task_ids"},
            )
        if len(body.member_task_ids) > 256:
            raise HTTPException(
                status_code=413,
                detail={"ok": False, "reason": "too_many_members"},
            )
        result = orchestrator.arm_wait_group(
            case_id, body.wait_group_id, cond, body.member_task_ids, actor="manager",
        )
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/artifacts")
    def api_publish_artifact(case_id: str, body: CasePublishArtifactBody) -> JSONResponse:
        """[A56/M4] Publish a durable artifact onto a Case (``artifact`` flow_link +
        ``artifact.published`` event). Gated by ``SPEC_AUTHORING_ENABLED`` (404 when
        OFF ⇒ byte-identical). The write is also flag-gated in the db layer."""
        from src.control.db import spec_authoring_enabled
        if not spec_authoring_enabled():
            raise HTTPException(status_code=404, detail="not_found")
        result = orchestrator.publish_artifact(
            case_id, body.artifact_id, kind=body.kind, title=body.title,
            uri=body.uri, actor="manager", metadata=body.metadata,
        )
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/spec")
    def api_publish_spec(case_id: str, body: CaseSpecBody) -> JSONResponse:
        """[A56/M4] Author a spec onto a Case as durable evidence (``spec.authored``).
        Gated by ``SPEC_AUTHORING_ENABLED`` (404 when OFF ⇒ byte-identical)."""
        from src.control.db import spec_authoring_enabled
        if not spec_authoring_enabled():
            raise HTTPException(status_code=404, detail="not_found")
        result = orchestrator.publish_spec(
            case_id, body.spec_id, body.body, title=body.title, actor="manager",
        )
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/spec-review")
    def api_record_spec_review(case_id: str, body: CaseSpecReviewBody) -> JSONResponse:
        """[A56/M4] Score a spec against R1 by a SEPARATE plan-reviewer seat; the
        verdict (accepted|rework_requested) is computed from the scores, not trusted.
        Records ``spec.review_scored`` + the canonical ``review.*`` event. Gated by
        ``SPEC_AUTHORING_ENABLED`` (404 when OFF ⇒ byte-identical)."""
        from src.control.db import spec_authoring_enabled
        if not spec_authoring_enabled():
            raise HTTPException(status_code=404, detail="not_found")
        result = orchestrator.record_spec_review(
            case_id, body.spec_id, body.scores, reviewer=body.reviewer, reason=body.reason,
        )
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/decompose")
    def api_decompose_case(case_id: str, body: CaseDecomposeBody) -> JSONResponse:
        """[A56/M4] Expand an APPROVED objective into a task-DAG of N ``task_attached``
        links on ONE Case (no orphan flow_runs). REFUSES (422 with a structured reason)
        unless the spec's latest scored review PASSED and the DAG is acyclic/well-formed.
        Gated by ``SPEC_AUTHORING_ENABLED`` (404 when OFF ⇒ byte-identical)."""
        from src.control.db import spec_authoring_enabled
        if not spec_authoring_enabled():
            raise HTTPException(status_code=404, detail="not_found")
        result = orchestrator.decompose_case(
            case_id, body.spec_id, body.tasks, actor="manager",
        )
        if not result.get("ok"):
            # A blocked decomposition (unapproved spec / cyclic / malformed DAG) is a
            # structured refusal, not a server error — 422 so the caller can react.
            raise HTTPException(status_code=422, detail=result)
        return JSONResponse(result)

    @router.get("/api/cases/{case_id}/brief")
    def api_get_case_brief(case_id: str) -> JSONResponse:
        """[A54/M3.4 Job 2] The full working state of a Case from the DB ALONE — the
        Manager's single 'where am I on this Case' read for reconstructing after a
        context reset. Read-only (no flag gate — a pure read is always byte-identical
        whether the continuation feature is on or off). 404 for an unknown Case."""
        result = orchestrator.get_case_brief(case_id)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("reason") or "case_not_found")
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/boot-reconcile")
    def api_boot_reconcile_case(case_id: str) -> JSONResponse:
        """[A54/M3.4 Job 2] Boot-time reconstruction hook — reconcile outstanding
        worker waits AND re-arm live wait-groups from the ledger (idempotent). Gated
        by ``DURABLE_RELAY_ENABLED`` (404 when OFF ⇒ byte-identical no-op, exactly
        like the /waits/reconcile route). The write itself is also flag-gated in the
        db layer (defence in depth)."""
        from src.control.db import durable_relay_enabled
        if not durable_relay_enabled():
            raise HTTPException(status_code=404, detail="not_found")
        result = orchestrator.boot_reconcile_case(case_id, actor="manager")
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/interrupt")
    async def api_interrupt_case(case_id: str, body: CaseInterruptBody) -> JSONResponse:
        """[A53] KILL path: cancel a Case's in-flight worker task(s), mark it
        blocked (resumable), record flow.interrupted, escalate once. NOT flag-gated
        — a safety valve must always be reachable. Idempotent. 404 for an unknown
        or already-terminal Case; 200 with ``{ok, cancelled_tasks, already}``."""
        reason = (body.reason or "operator_kill").strip()[:64] or "operator_kill"
        result = await orchestrator.interrupt_case(case_id, actor="operator", reason=reason)
        if not result.get("ok"):
            code = 404 if result.get("reason") in ("case_not_found", "case_closed") else 503
            raise HTTPException(status_code=code, detail=result)
        return JSONResponse(result)

    @router.post("/api/cases/orphans/sweep")
    async def api_sweep_orphaned_cases(body: CaseOrphanSweepBody) -> JSONResponse:
        """Block stale open Cases that have no active Manager session.

        Service boundary checklist: bounded scan (limit 1..500), tiny JSON body,
        no unbounded payload reads, malformed body rejected by Pydantic, DB
        unavailable returns a structured error, and writes reuse ``interrupt_case``
        so cancellation/status/audit behaviour stays on the existing Case path.
        """
        result = await orchestrator.sweep_orphaned_cases(
            limit=body.limit,
            dry_run=body.dry_run,
            reason=body.reason or "manager_session_unavailable",
            close_terminal_orphans=body.close_terminal_orphans,
        )
        if not result.get("ok"):
            raise HTTPException(status_code=503, detail=result)
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/state")
    async def api_set_case_state(case_id: str, body: CaseStateBody) -> JSONResponse:
        """Operator Case state control for non-terminal Cases.

        Service boundary checklist: bounded tiny JSON body, no bulk reads, no
        unbounded payload, malformed state rejected with structured result,
        dependency failure returns 503, and writes go through orchestrator
        methods that reuse the established interrupt/unblock audit paths.
        """
        result = await orchestrator.set_case_state(
            case_id,
            state=body.state,
            actor="operator",
            reason=body.reason or "operator_state_change",
        )
        if not result.get("ok"):
            code = 404 if result.get("reason") == "case_not_found" else 409
            if result.get("reason") == "db_unavailable":
                code = 503
            raise HTTPException(status_code=code, detail=result)
        return JSONResponse(result)

    @router.get("/api/cases/{case_id}/resume-state")
    def api_case_resume_state(case_id: str) -> JSONResponse:
        """[quota-resume] Is this Case quota-paused, when does quota return, what
        would resuming cost, and is a decision already pending?

        Service boundary checklist: read-only; three bounded indexed reads (the
        pause event, the manager link, the session's last turn) keyed on one path
        id; no unbounded payload; no scarce resource held; a missing DB or absent
        quota instrument degrades to an honest ``paused=false`` / ``known=false``
        answer rather than a fabricated one.
        """
        return JSONResponse(orchestrator.case_resume_state(case_id))

    @router.post("/api/cases/{case_id}/resume")
    async def api_case_resume(case_id: str, body: CaseResumeBody) -> JSONResponse:
        """[quota-resume] Resume a Case NOW — the operator's manual equivalent of
        the automatic quota-restore path, deliberately the SAME leased
        ``resume_case`` call so the two can never race into two Managers.

        This is a continuation, not a fork: same Case, same objective, existing
        waits re-armed. 409 on a refusal the operator can act on (a busy Manager,
        a resume already in flight, a terminal Case), 404 on an unknown Case.
        """
        result = await orchestrator.resume_case(
            case_id, mode=body.mode, actor="operator",
        )
        if not result.get("ok"):
            code = {
                "case_not_found": 404, "db_unavailable": 503,
                "no_manager_link": 409, "manager_busy": 409,
                "resume_in_flight": 409, "case_terminal": 409,
                "continuation_disabled": 409,
            }.get(str(result.get("reason") or ""), 500)
            raise HTTPException(status_code=code, detail=result)
        return JSONResponse(result)

    @router.post("/api/cases/{case_id}/operator-close")
    def api_operator_close_case(case_id: str, body: CaseOperatorCloseBody) -> JSONResponse:
        """Manual operator close for stale/non-terminal Cases.

        Service boundary checklist: bounded tiny JSON body, one bounded DB row
        read, no unbounded payload, malformed body rejected by Pydantic, DB
        unavailable returns structured 503, and closure still goes through the
        existing authoritative ``close_case`` path.
        """
        from src.control.db import _parse_completion_criteria

        note = (body.reason or "operator_manual_close").strip()[:256] or "operator_manual_close"
        reconciliation = None
        db = core._db()
        if db is None:
            raise HTTPException(
                status_code=503,
                detail={"ok": False, "closed": False, "reason": "db_unavailable"},
            )
        if body.waive_completion_criteria:
            row = db.get_flow_run(case_id)
            criteria = _parse_completion_criteria(row.get("completion_criteria")) if row else []
            if criteria:
                reconciliation = [
                    {"criterion": c, "status": "waived", "reason": note}
                    for c in criteria
                ]
        result = orchestrator.close_case(
            case_id,
            actor="operator",
            criteria_reconciliation=reconciliation,
        )
        if not result.get("ok"):
            reason = result.get("reason")
            code = 404 if reason and str(reason).startswith("unknown case") else 409
            if reason == "db_unavailable":
                code = 503
            raise HTTPException(status_code=code, detail=result)
        if result.get("closed"):
            try:
                db.append_flow_event(
                    case_id,
                    "case.operator_closed",
                    "operator",
                    payload={"reason": note},
                )
            except Exception as exc:
                logger.warning(
                    "event=operator_close_audit_failed flow_run_id=%s err=%s",
                    case_id,
                    exc,
                )
        return JSONResponse(result)

    return router
