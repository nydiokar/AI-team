"""Managed turn-queue routes (A82) — admit, list, edit, withdraw, pause/resume, enroll,
recovery.

Mounted by ``control_api.build_control_api``; route map: docs/backend/ARCHITECTURE.md "Turn
requests (A82 managed turn queue)". Module-level helpers are looked up as ``core.<name>`` so
tests that monkeypatch ``control_api`` keep reaching these handlers.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.control import control_api as core

# [A82 Stage 5] Authorization scheme of the scoped agent sender capability.
from src.control.agent_sender import SENDER_AUTH_SCHEME as _SENDER_AUTH_SCHEME
from src.control.control_api import (
    InstructionBody,
    TurnQueueControlOut,
    TurnRecoveryResolveBody,
    TurnRequestDetailOut,
    TurnRequestEditBody,
    TurnRequestPageOut,
    TurnRequestReceiptOut,
    TurnRequestSummaryOut,
    logger,
)


def build_router(
    orchestrator: Any, *, require_auth: Callable[..., Any], bearer: HTTPBearer
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/turn-requests/{task_id}/resolve-recovery", dependencies=[Depends(require_auth)])
    def api_resolve_turn_recovery(task_id: str, body: TurnRecoveryResolveBody) -> JSONResponse:
        """[A82 Stage 3 rework] Operator exit for every held managed state.

        * ``claimed`` (never started) → ``requeue`` only: token-fenced release
          to pending (prompt preserved, token cleared).
        * ``running`` / ``recovery_required`` → ``failed``/``cancelled`` only,
          with ``acknowledge_uncertain``: the row enters recovery (if running)
          and is resolved through the Stage-2 ``resolve_recovery`` with the
          operator decision recorded as evidence. Never ``completed`` (no
          result) and never requeued after start (double-execution risk).
        No claim token is needed or returned."""
        from src.control.turn_queue import TurnQueueError, emit_turn_queue_changed

        db = core._db()
        if db is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "db_unavailable"})
        row = db.get_task(task_id)
        if not row or int(row.get("queue_protocol") or 0) != 1:
            raise HTTPException(status_code=404, detail={"ok": False, "reason": "no_managed_turn"})
        status = str(row.get("status") or "")
        token = row.get("claim_token") or ""
        try:
            if status == "claimed":
                if body.decision != "requeue":
                    raise HTTPException(status_code=409, detail={"ok": False, "reason": "unstarted_turn_requeue_only"})
                if not db.release_turn(task_id, token):
                    raise HTTPException(status_code=409, detail={"ok": False, "reason": "state_changed"})
                from src.control.turn_scheduler import notify_managed_released

                notify_managed_released(1)  # [A82 Stage 6 F1 / Stage 7] waiting count rose: count it + refresh
                emit_turn_queue_changed(row.get("session_id"), "released", turn_id=task_id, status="pending")
                return JSONResponse({"ok": True, "task_id": task_id, "status": "pending"})
            if status in ("running", "recovery_required"):
                if body.decision == "requeue":
                    raise HTTPException(status_code=409, detail={"ok": False, "reason": "started_turn_cannot_requeue"})
                if not body.acknowledge_uncertain:
                    raise HTTPException(status_code=409, detail={"ok": False, "reason": "acknowledgement_required"})
                if status == "running":
                    db.enter_recovery(task_id, token, reason=f"operator: {body.note}"[:500])
                evidence = {
                    "source": "operator",
                    "task_id": task_id,
                    "quiescent": True,
                    "terminal": True,
                    "terminal_status": body.decision,
                    "acknowledged_uncertain": True,
                    "note": body.note,
                }
                res = db.resolve_recovery(task_id, token, evidence, resolved_status=body.decision)
                from src.control.turn_scheduler import notify_turn_queue_changed

                notify_turn_queue_changed()  # [A82 Stage 4a] slot freed
                emit_turn_queue_changed(row.get("session_id"), "resolved", turn_id=task_id,
                                        status=str(res.resolved_status))
                return JSONResponse({"ok": True, "task_id": task_id, "status": res.resolved_status})
        except TurnQueueError as e:
            raise HTTPException(status_code=getattr(e, "status_code", 409), detail={"ok": False, "reason": e.code})
        raise HTTPException(status_code=409, detail={"ok": False, "reason": "not_resolvable", "status": status})

    async def _turn_request_principal(
        request: Request,
        creds: Optional[HTTPAuthorizationCredentials] = Security(bearer),
    ) -> Optional[Any]:
        """[A82 Stage 5] The admission resource's two auth scopes (packet §3.13).
        ``Authorization: AITeamSender <capability>`` ⇒ a scoped agent sender,
        validated against THIS target before the body is parsed (returns the
        canonical ``SenderIdentity``). Anything else ⇒ the unchanged operator
        bearer check (returns None). A shared bearer is never an agent
        identity and a capability never authenticates an operator."""
        scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
        if scheme.strip().lower() == _SENDER_AUTH_SCHEME.lower():
            return await core._validate_agent_sender(value.strip(), str(request.path_params.get("session_id") or ""))
        await require_auth(creds)
        return None

    @router.post("/api/sessions/{session_id}/turn-requests")
    async def api_create_turn_request(
        session_id: str, request: Request,
        sender: Optional[Any] = Depends(_turn_request_principal),
    ) -> JSONResponse:
        """Acknowledge a managed instruction only after canonical admission.
        The body (byte-capped by ``BodyCapMiddleware``) is read and validated
        only after authentication: unauthenticated input is 401, never 422."""
        from src.control.turn_queue import TurnAdmission

        body = core._parse_turn_request_body(await request.body(), request.headers.get("content-type"))

        session = orchestrator.session_service.store.get(session_id)
        if session is None:
            if sender is not None:  # validated just now; vanished ⇒ out of scope
                raise HTTPException(status_code=403, detail={"ok": False, "reason": "scope_forbidden"})
            raise HTTPException(status_code=404, detail={"ok": False, "reason": "session_not_found"})
        if sender is not None:
            idem = request.headers.get("idempotency-key")
            if idem is not None and idem != body.operation_id:
                raise HTTPException(status_code=422, detail={"ok": False, "reason": "operation_id_mismatch"})
        if not await core._session_turn_queue_enrolled(session_id):
            raise HTTPException(status_code=409, detail={"ok": False, "reason": "session_not_enrolled"})
        if sender is not None:
            admitted = await core._submit_agent_instruction(orchestrator, body, session, sender)
        else:
            admitted = await core._submit_managed_instruction(
                orchestrator, InstructionBody(description=body.body), session,
                body.operation_id,
            )
        if not isinstance(admitted, TurnAdmission):
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "admission_receipt_missing"})
        # [A82 Stage 6] The receipt reads the COMMITTED row (a replay reports the
        # current status/revision, e.g. after an edit) — never the request.
        db = core._db()
        row: Optional[Dict[str, Any]] = None
        if db is not None:
            try:
                row = await asyncio.to_thread(db.get_turn_request, str(admitted))
            except Exception:  # noqa: BLE001 — committed; the receipt falls back to the admission
                row = None
        receipt = TurnRequestReceiptOut(
            turn_id=str(admitted), task_id=str(admitted),
            status=str((row or {}).get("status") or admitted.status),
            revision=int((row or {}).get("revision") or admitted.revision),
            queue_sequence=(row or {}).get("queue_sequence", admitted.queue_sequence),
            queue_position=(row or {}).get("queue_position"),
            accepted_at=(row or {}).get("created_at"),
            idempotent_replay=bool(admitted.idempotent_replay),
        )
        if sender is not None:
            receipt.source = "agent"
            receipt.sender_session_id = sender.session_id
        else:
            receipt.source = "operator"  # [A82 Stage 6 F5b] server-derived, never the request
        return JSONResponse(receipt.model_dump(), status_code=202)

    def _require_queue_db() -> Any:
        db = core._db()
        if db is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "db_unavailable"})
        return db

    async def _turn_mutation_http(db: Any, task_id: str, err: Exception) -> HTTPException:
        """[A82 Stage 6] A refused queue mutation; a 409 (stale revision or
        consumption race) carries a SAFE current summary so the client refetches
        instead of overwriting (design §4)."""
        http = core._turn_queue_http(err)
        if http.status_code == 409:
            try:
                row = await asyncio.to_thread(db.get_turn_request, task_id)
            except Exception:  # noqa: BLE001 — the 409 itself stays truthful
                row = None
            if row is not None:
                http.detail = {**http.detail, "current": TurnRequestSummaryOut.from_row(
                    {**row, "preview": str(row.get("body") or "")[:2048].encode("utf-8")[:2048]
                     .decode("utf-8", errors="ignore")},
                ).model_dump()}
        return http

    def _if_match_revision(raw: str) -> int:
        """[A82 Stage 6 F5c] The expected revision from ``If-Match``: a bare
        ``3`` or a strong entity-tag ``"3"``. A weak tag (``W/"3"``) never
        matches under the strong comparison If-Match requires (RFC 9110
        §13.1.1) ⇒ 412; anything else ⇒ 422."""
        tag: str = raw.strip()
        if tag.startswith("W/"):
            raise HTTPException(status_code=412, detail={"ok": False, "reason": "weak_etag_never_matches"})
        if len(tag) >= 2 and tag[0] == tag[-1] == '"':
            tag = tag[1:-1]
        if not (tag.isascii() and tag.isdigit()) or int(tag) < 1:
            raise HTTPException(status_code=422, detail={"ok": False, "reason": "invalid_if_match"})
        return int(tag)

    async def _operator_human_turn(db: Any, task_id: str) -> Dict[str, Any]:
        row = await asyncio.to_thread(db.get_turn_request, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"ok": False, "reason": "turn_not_found"})
        if row["turn_source"] not in ("human", "operator") or row["turn_kind"] != "instruction":
            raise HTTPException(status_code=403, detail={"ok": False, "reason": "not_human_turn"})
        return row

    @router.get("/api/sessions/{session_id}/turn-requests", dependencies=[Depends(require_auth)],
             response_model=TurnRequestPageOut)
    def api_list_turn_requests(
        session_id: str, cursor: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    ) -> TurnRequestPageOut:
        """Cursor page (run order) of the session's open managed turns."""
        from src.control.turn_queue import TurnQueueError

        db = _require_queue_db()
        try:
            page: Dict[str, Any] = db.list_turn_requests(session_id, after_sequence=cursor, limit=limit)
        except TurnQueueError as err:
            raise core._turn_queue_http(err)
        return TurnRequestPageOut.model_validate({
            **page, "turns": [TurnRequestSummaryOut.from_row(t) for t in page["turns"]],
        })

    @router.get("/api/turn-requests/{task_id}", dependencies=[Depends(require_auth)],
             response_model=TurnRequestDetailOut)
    def api_get_turn_request(task_id: str) -> TurnRequestDetailOut:
        from src.control.turn_queue import TurnQueueError

        db = _require_queue_db()
        try:
            row = db.get_turn_request(task_id)
        except TurnQueueError as err:
            raise core._turn_queue_http(err)
        if row is None:
            raise HTTPException(status_code=404, detail={"ok": False, "reason": "turn_not_found"})
        return TurnRequestDetailOut.from_row(row)

    @router.patch("/api/turn-requests/{task_id}", dependencies=[Depends(require_auth)],
               response_model=TurnRequestDetailOut)
    async def api_edit_turn_request(
        task_id: str, body: TurnRequestEditBody,
        if_match: str = Header(alias="If-Match", max_length=64),
    ) -> TurnRequestDetailOut:
        """Conditional edit of a QUEUED human turn (expected revision in
        If-Match). Sequence, recipient, source and Case never change."""
        from src.control.turn_admission import run_turn_mutation_async
        from src.control.turn_queue import TurnQueueError, emit_turn_queue_changed
        from src.control.turn_scheduler import notify_turn_queue_changed

        revision: int = _if_match_revision(if_match)
        db = _require_queue_db()
        row = await _operator_human_turn(db, task_id)
        try:
            await run_turn_mutation_async(
                lambda: db.revise_turn(task_id, revision, body=body.body, actor="operator"),
            )
        except TurnQueueError as err:
            raise await _turn_mutation_http(db, task_id, err)
        notify_turn_queue_changed()
        emit_turn_queue_changed(row["session_id"], "edited", turn_id=task_id, status="queued")
        current = await asyncio.to_thread(db.get_turn_request, task_id)
        return TurnRequestDetailOut.from_row(current or row)

    @router.post("/api/turn-requests/{task_id}/withdraw", dependencies=[Depends(require_auth)],
              response_model=TurnRequestSummaryOut)
    async def api_withdraw_turn_request(
        task_id: str, if_match: str = Header(alias="If-Match", max_length=64),
    ) -> TurnRequestSummaryOut:
        """Withdraw ONLY this queued human turn (auditable; never a deletion and
        never an execution failure). Its written Case lineage is voided."""
        from src.control.turn_admission import run_turn_mutation_async
        from src.control.turn_queue import TurnQueueError, emit_turn_queue_changed
        from src.control.turn_scheduler import notify_turn_queue_changed

        revision: int = _if_match_revision(if_match)
        db = _require_queue_db()
        row = await _operator_human_turn(db, task_id)
        try:
            await run_turn_mutation_async(
                lambda: db.withdraw_turn(task_id, revision, actor="operator", void_lineage=True),
            )
        except TurnQueueError as err:
            raise await _turn_mutation_http(db, task_id, err)
        void = getattr(orchestrator, "_void_withdrawn_lineage", None)
        if callable(void):
            try:
                await asyncio.to_thread(void, task_id)
            except Exception as e:  # noqa: BLE001 — stays `void`; the scheduler sweep re-runs it
                logger.warning("event=managed_void_lineage_deferred task_id=%s err=%s", task_id, e)
        notify_turn_queue_changed()
        emit_turn_queue_changed(row["session_id"], "withdrawn", turn_id=task_id, status="withdrawn")
        current = await asyncio.to_thread(db.get_turn_request, task_id)
        return TurnRequestSummaryOut.from_row({**(current or row), "preview": ""})

    async def _set_queue_paused(session_id: str, paused: bool) -> TurnQueueControlOut:
        from src.control.turn_admission import set_queue_paused_async
        from src.control.turn_queue import TurnQueueError

        db = _require_queue_db()
        try:
            state = await set_queue_paused_async(db, session_id, paused)
        except TurnQueueError as err:
            raise core._turn_queue_http(err)
        return TurnQueueControlOut.model_validate(state)

    @router.post("/api/sessions/{session_id}/turn-requests/pause", dependencies=[Depends(require_auth)],
              response_model=TurnQueueControlOut)
    async def api_pause_turn_requests(session_id: str) -> TurnQueueControlOut:
        """Persist an operator queue pause (survives restart). Nothing queued
        activates until an explicit resume; the active turn is not touched."""
        return await _set_queue_paused(session_id, True)

    @router.post("/api/sessions/{session_id}/turn-requests/resume", dependencies=[Depends(require_auth)],
              response_model=TurnQueueControlOut)
    async def api_resume_turn_requests(session_id: str) -> TurnQueueControlOut:
        """Clear ONLY the operator pause / operator-stop hold. Recovery, Case,
        provider-deadline and approval gates keep holding."""
        return await _set_queue_paused(session_id, False)

    @router.post("/api/sessions/{session_id}/turn-requests/enroll", dependencies=[Depends(require_auth)])
    async def api_enroll_turn_queue(session_id: str) -> JSONResponse:
        """[A82 Stage 7] Operator enrollment onto the managed turn queue (design
        §10 step 6). Default-OFF flag ``TURN_QUEUE_ENROLLMENT_ENABLED``; typed
        refusal (409 + reason) for a busy / closed session, legacy work, or a
        carrier without managed capability; 503 without the canonical DB."""
        from src.control.turn_queue import TurnQueueError

        enroll = getattr(orchestrator, "enroll_session_turn_queue", None)
        if not callable(enroll):
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "enrollment_unavailable"})
        try:
            changed = await enroll(session_id)
        except TurnQueueError as err:
            raise core._turn_queue_http(err)
        return JSONResponse({"ok": True, "session_id": session_id, "enrolled": True,
                             "changed": bool(changed)})

    @router.post("/api/sessions/{session_id}/turn-requests/unenroll", dependencies=[Depends(require_auth)])
    async def api_unenroll_turn_queue(session_id: str) -> JSONResponse:
        """[A82 Stage 7] Rollback exit (design §10 step 8): remove enrollment only
        when no waiting/active/recovery managed obligation remains (409
        ``managed_obligation_remaining`` otherwise). Not flag-gated."""
        from src.control.turn_queue import TurnQueueError

        unenroll = getattr(orchestrator, "unenroll_session_turn_queue", None)
        if not callable(unenroll):
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "enrollment_unavailable"})
        try:
            changed = await unenroll(session_id)
        except TurnQueueError as err:
            raise core._turn_queue_http(err)
        return JSONResponse({"ok": True, "session_id": session_id, "enrolled": False,
                             "changed": bool(changed)})

    return router
