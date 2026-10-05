"""Session routes — list/create, instructions, transcript/timeline/usage, lifecycle ops,
uploads, cache heartbeats.

Mounted by ``control_api.build_control_api``; route map: docs/backend/ARCHITECTURE.md
"Sessions". Module-level helpers are looked up as ``core.<name>`` so tests that monkeypatch
``control_api`` keep reaching these handlers.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse

from src.control import control_api as core
from src.control.control_api import (
    _ID_STR_MAX,
    _KEEP_BODY_MAX_BYTES,
    _REASON_STATUS,
    PRINCIPAL_HEADER,
    BindBody,
    CacheHeartbeatBody,
    CreateSessionBody,
    EffortBody,
    InspectBody,
    InstructionBody,
    KeepSessionBody,
    ModelBody,
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

    @router.get("/api/sessions")
    def api_sessions(
        limit: int = Query(200, ge=1, le=1000),
        keep_pinned: Optional[bool] = Query(default=None),
    ) -> JSONResponse:
        try:
            # [A83] Pass the existing DB handle so the batched secondary-reason
            # derivation runs on the read path (no N+1, no timer — #145/#147).
            views = orchestrator.session_service.list_views(
                limit=limit, keep_pinned=keep_pinned, db=core._db()
            )
            sessions = [v.to_dict() for v in views]
        except Exception as e:
            logger.warning("control_api_sessions_failed err=%s", e)
            sessions = []
        return JSONResponse({"sessions": sessions})

    @router.get("/api/cache-heartbeats")
    def api_cache_heartbeats(
        session_id: Optional[str] = Query(default=None, max_length=_ID_STR_MAX),
        limit: int = Query(100, ge=1, le=500),
    ) -> JSONResponse:
        db = core._db()
        if db is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "db_unavailable"})
        return JSONResponse({
            "ok": True,
            "heartbeats": db.list_cache_heartbeats(session_id=session_id, limit=limit),
        })

    @router.post("/api/sessions/{session_id}/cache-heartbeat")
    def api_enable_cache_heartbeat(session_id: str, body: CacheHeartbeatBody) -> JSONResponse:
        db = core._db()
        if db is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "db_unavailable"})
        session = orchestrator.session_store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail={"ok": False, "reason": "session_not_found"})
        expires_at = None
        if body.duration_sec:
            from datetime import datetime, timedelta, timezone
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=body.duration_sec)).isoformat()
        hb = db.ensure_cache_heartbeat_owner(
            session_id,
            reason="manual",
            owner_type="operator",
            owner_id="manual",
            expected_runtime_sec=body.duration_sec,
            expires_at=expires_at,
            max_beats=body.max_beats,
        )
        if hb is None:
            raise HTTPException(status_code=409, detail={"ok": False, "reason": "cache_heartbeat_not_enabled"})
        return JSONResponse({"ok": True, "heartbeat": hb})

    @router.delete("/api/sessions/{session_id}/cache-heartbeat")
    def api_stop_cache_heartbeat(session_id: str) -> JSONResponse:
        db = core._db()
        if db is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "db_unavailable"})
        changed = 0
        for hb in db.list_cache_heartbeats(session_id=session_id, limit=20):
            if str(hb.get("status") or "") in {"observe_only", "active"}:
                changed += 1 if db.stop_cache_heartbeat(str(hb.get("id") or ""), "operator_disabled") else 0
        return JSONResponse({"ok": True, "stopped": changed})

    @router.get("/api/sessions/{session_id}/messages")
    def api_session_messages(
        session_id: str,
        limit: int = Query(200, ge=1, le=1000),
    ) -> JSONResponse:
        """The session's real conversation, from the session record's task_history
        (src.control.transcript) — full per-turn user_message → result_summary,
        oldest→newest. 404 only on a path-traversal escape; a session with no turns
        yet returns ``{"messages": []}`` (a real empty conversation)."""
        from src.control import transcript as _transcript
        turns = _transcript.get_transcript(
            core._results_dir(), core._sessions_dir(), session_id, limit=limit
        )
        if turns is None:
            raise HTTPException(status_code=404, detail="not_found")
        return JSONResponse({"messages": turns})

    @router.get("/api/sessions/{session_id}/timeline")
    def api_session_timeline(
        session_id: str,
        limit: int = Query(50, ge=1, le=200),
        cursor: Optional[str] = Query(default=None),
    ) -> JSONResponse:
        """Durable, bounded session activity timeline.

        Service boundary checklist:
        - concurrency: read-only bounded DB queries; no scarce resource held
          beyond the request.
        - memory: per-source reads are capped, endpoint limit is 1..200.
        - request size: path id plus bounded query params only.
        - timeout/degraded: no filesystem scans or SSE log parsing; DB/telemetry
          failures degrade through coverage fields, not fabricated states.
        - malformed input: bad cursors normalize to the first page.
        - backing resources: unavailable DB returns empty durable response with
          unavailable coverage.
        """
        db = core._db()
        session = orchestrator.session_service.store.get(session_id)
        session_row = core._session_payload(session) if session is not None else None
        # [A83] Derive the session's secondary reason on the read path (batched;
        # zero reads for BUSY/terminal — never on a timer, #145/#147) and attach
        # it to the timeline head + response.
        session_reason = None
        if db is not None and session is not None:
            try:
                from src.core.session_reason import derive_session_reasons
                derived = derive_session_reasons(db, [session]).get(session_id)
                session_reason = derived.to_dict() if derived is not None else None
            except Exception as e:
                logger.warning("control_api_timeline_reason_failed err=%s", e)
        from src.control.session_timeline import build_session_timeline
        response = build_session_timeline(
            db=db,
            telemetry_store=core._telemetry_store(),
            session_id=session_id,
            session_row=session_row,
            session_reason=session_reason,
            limit=limit,
            cursor=cursor,
        )
        return JSONResponse(response.model_dump(mode="json"))

    @router.get("/api/sessions/{session_id}/usage")
    def api_session_usage(session_id: str) -> JSONResponse:
        """Per-session token totals + approximate USD cost.

        Aggregates authoritative per-request token columns (llm_model_requests,
        de-duplicated) via the existing batched query — so it is inherently
        backfilled for any session with recorded telemetry (no N+1). Cost is
        estimated honestly: an un-priceable model yields ``cost.known=false`` +
        a reason, never a fabricated number.

        Service boundary checklist: read-only, single bounded DB aggregate keyed
        on one path id; no scarce resource held; DB-unavailable degrades to zero
        totals with an unknown-cost estimate, not a fabricated one.
        """
        from src.services.pricing import TokenTotals, estimate_cost

        db = core._db()
        session = orchestrator.session_service.store.get(session_id)
        view = core._session_payload(session) if session is not None else None
        model: Optional[str] = None
        if view is not None:
            model = view.get("model") or view.get("default_model")

        raw: Dict[str, int] = {}
        if db is not None:
            try:
                raw = db.get_session_token_totals([session_id]).get(session_id, {})
            except Exception as e:
                logger.warning("session_usage_totals_failed session_id=%s err=%s", session_id, e)
                raw = {}

        totals = TokenTotals(
            input=int(raw.get("input", 0) or 0),
            output=int(raw.get("output", 0) or 0),
            cache_read=int(raw.get("cache_read", 0) or 0),
            cache_creation=int(raw.get("cache_creation", 0) or 0),
        )
        totals.total = (
            totals.input + totals.output + totals.cache_read + totals.cache_creation
        )
        cost = estimate_cost(model, totals)
        return JSONResponse(
            {
                "session_id": session_id,
                "model": model,
                "tokens": totals.model_dump(),
                "cost": cost.model_dump(),
            }
        )

    @router.post("/api/instructions")
    async def api_instructions(
        body: InstructionBody,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        principal: Optional[str] = Header(default=None, alias=PRINCIPAL_HEADER),
    ) -> JSONResponse:
        """Submit an instruction. With session_id it mirrors the Telegram session
        path (session → BUSY, source=web_session); otherwise a one-off."""
        # The task-harness Level-3 admission gate (orchestrator._enqueue_task)
        # RAISES `HarnessAdmissionBlocked` rather than returning a task_id, so a
        # blocked task can never be mistaken for an accepted one. Translate it to a
        # clean 409 here (not an opaque 500) and, crucially, undo the optimistic
        # BUSY write on the session so it is not stranded with no in-flight task.
        from src.control.turn_queue import TurnQueueError
        from src.orchestrator import HarnessAdmissionBlocked

        async with idem_guard_async("instructions", idempotency_key) as cached:
            if cached is not None:
                return JSONResponse(cached)

            session = None
            if body.session_id:
                session = orchestrator.session_service.store.get(body.session_id)
                if session is None:
                    raise HTTPException(status_code=404, detail="session_not_found")
                if await core._session_turn_queue_enrolled(session.session_id):
                    # [A82 Stage 4a] Enrolled ⇒ managed admission. Acceptance is
                    # durable before this returns and does NOT write BUSY /
                    # last_user_message / last_task_id (queued is not busy).
                    task_id = await core._submit_managed_instruction(
                        orchestrator, body, session, idempotency_key, principal,
                    )
                    session = orchestrator.session_service.store.get(session.session_id)
                    resp = {"ok": True, "task_id": str(task_id),
                            "session": await asyncio.to_thread(core._session_payload, session, with_queue=True)}
                    idem_put("instructions", idempotency_key, resp)
                    return JSONResponse(resp)
                # Status write (BUSY + last_user_message) lives on the service.
                orchestrator.session_service.mark_busy(
                    session.session_id, last_user_message=body.description)
                session = orchestrator.session_service.store.get(session.session_id)
                try:
                    task_id = await orchestrator.submit_instruction(
                        description=body.description,
                        session_id=session.session_id,
                        cwd=session.repo_path or body.cwd,
                        target_files=body.target_files,
                        source="web_session",
                        parent_flow_run_id=body.parent_flow_run_id,
                        join_case_id=body.case_id,
                        extra_metadata=core._instruction_extra_metadata(body),
                        **core._enrollment_kw(orchestrator, False),
                    )
                except HarnessAdmissionBlocked as blocked:
                    # No task ran — return the session to IDLE so it stays usable.
                    orchestrator.session_service.mark_idle(session.session_id)
                    raise core._harness_blocked_http(blocked)
                except TurnQueueError as err:
                    # [A82 Stage 7] e.g. enrollment_in_progress: nothing queued.
                    orchestrator.session_service.mark_idle(session.session_id)
                    raise core._turn_queue_http(err)
                session.last_task_id = task_id
                orchestrator.session_service.store.save(session)
            else:
                try:
                    task_id = await orchestrator.submit_instruction(
                        description=body.description,
                        cwd=body.cwd,
                        target_files=body.target_files,
                        source="web_oneoff",
                        parent_flow_run_id=body.parent_flow_run_id,
                        join_case_id=body.case_id,
                        extra_metadata=core._instruction_extra_metadata(body),
                    )
                except HarnessAdmissionBlocked as blocked:
                    raise core._harness_blocked_http(blocked)

            resp = {"ok": True, "task_id": task_id, "session": core._session_payload(session)}
            idem_put("instructions", idempotency_key, resp)
            return JSONResponse(resp)

    @router.post("/api/sessions")
    def api_create_session(
        body: CreateSessionBody,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        with idem_guard("create_session", idempotency_key) as cached:
            if cached is not None:
                return JSONResponse(cached)

            from src.control.session_node_resolver import resolve_unpinned_session_node
            from src.core.interfaces import SessionOrigin

            node_id = body.node_id or resolve_unpinned_session_node(
                backend=body.backend,
                repo_path=body.repo_path,
            ) or "__local__"

            result = orchestrator.session_service.create_session(
                backend=body.backend,
                repo_path=body.repo_path,
                model=body.model,
                node_id=node_id,
                origin=SessionOrigin(channel="web", kind="user"),
                role_boot=body.role_boot,
                continued_from=body.continued_from,
                bind_chat=False,
            )
            env = core._command_envelope(result)
            if not result.ok:
                raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
            idem_put("create_session", idempotency_key, env)
            return JSONResponse(env)

    # REVISIT (2026-10-05): no caller; the web origin only echoes - validate what it is for.
    @router.post("/api/sessions/{session_id}/bind")
    def api_bind_session(session_id: str, body: BindBody) -> JSONResponse:
        if body.chat_id is None:
            # Web has no chat binding today; verify the session exists and echo it.
            session = orchestrator.session_service.store.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="session_not_found")
            return JSONResponse({"ok": True, "reason": "", "session": core._session_payload(session)})
        result = orchestrator.session_service.bind_active(body.chat_id, session_id)
        env = core._command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        return JSONResponse(env)

    @router.post("/api/sessions/{session_id}/stop")
    def api_stop_session(session_id: str) -> JSONResponse:
        session = orchestrator.session_service.store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        # [A82 Stage 4b] Enrolled: stop = cancel the turn owning the ACTIVE slot
        # (ledger truth, never last_task_id); the turn row, not a whole-session
        # CANCELLED save, is the truth. None ⇒ legacy (unchanged below).
        from src.control.turn_queue import TurnQueueError

        stop_managed = getattr(orchestrator, "stop_managed_session_turn", None)
        try:
            # [A82 Stage 6] "Stop active" = a PERSISTENT operator queue pause,
            # committed BEFORE the cancel (freeing the slot can never launch the
            # next queued instruction), then cancel ONLY the active turn. Waiting
            # work stays visible and paused until the explicit resume route (a
            # new admission does not clear it — design §7). Same service as
            # Telegram's stop (``pause_queue=True``).
            managed = stop_managed(session, pause_queue=True) if callable(stop_managed) else None
        except TurnQueueError as err:
            raise core._turn_queue_http(err)
        if managed is not None:
            return JSONResponse({"ok": True, "cancelled": managed[0], "task_id": managed[1]})
        cancelled = False
        if session.last_task_id:
            cancelled = bool(orchestrator.cancel_task(session.last_task_id))
            if cancelled:
                # Status write lives on the service (parity with Telegram /session_cancel).
                orchestrator.session_service.mark_cancelled(session_id)
        return JSONResponse({"ok": True, "cancelled": cancelled, "task_id": session.last_task_id})

    @router.post("/api/sessions/{session_id}/compact")
    async def api_compact_session(session_id: str, request: Request) -> JSONResponse:
        session = orchestrator.session_service.store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        from src.control.turn_queue import TurnQueueError

        # [A82 Stage 4b] Idempotency-Key = the managed compaction's durable
        # operation id (only consulted for an enrolled session).
        idem = (request.headers.get("Idempotency-Key") or "").strip()[:256] or None
        try:
            result = await (
                orchestrator.compact_session(session_id, operation_id=idem)
                if idem else orchestrator.compact_session(session_id)
            )
        except TurnQueueError as err:
            raise core._turn_queue_http(err)
        body: Dict[str, Any] = {
            "ok": bool(getattr(result, "success", False)),
            "output": getattr(result, "output", ""),
            "errors": list(getattr(result, "errors", []) or []),
        }
        managed = getattr(result, "parsed_output", None)
        if isinstance(managed, dict) and managed.get("managed"):
            body.update({"queued": True, "task_id": managed.get("task_id"),
                         "status": managed.get("status")})
        return JSONResponse(body)

    @router.post("/api/sessions/{session_id}/close")
    async def api_close_session(session_id: str) -> JSONResponse:
        # backend.close may block (local backend) → off-thread, like Telegram.
        import asyncio
        result = await asyncio.to_thread(
            orchestrator.session_service.close_session,
            session_id,
            backends=getattr(orchestrator, "_backends", {}),
        )
        env = core._command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        return JSONResponse(env)

    @router.post("/api/sessions/{session_id}/restore")
    def api_restore_session(session_id: str) -> JSONResponse:
        result = orchestrator.session_service.restore_session(session_id)
        env = core._command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        return JSONResponse(env)

    @router.post("/api/sessions/{session_id}/keep")
    async def api_keep_session(session_id: str, request: Request) -> JSONResponse:
        """Set the operator keep marker and searchable note.

        This is not mesh affinity pinning. It only persists operator intent for
        later retrieval and survives close because it is ordinary session metadata.

        Service boundary checklist:
        - concurrency: single session read + save; last write wins, matching the
          existing model/effort setters.
        - memory/request size: raw body capped at 4096 bytes; note max 4000 chars.
        - timeout: no backend/worker call, only local store/DB persistence.
        - malformed input: bad JSON/types return 422 with a stable reason.
        - backing resources: missing session returns 404; DB shadow-write failures
          follow existing SessionStore JSON-first persistence behavior.
        """
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > _KEEP_BODY_MAX_BYTES:
                    raise HTTPException(status_code=413, detail={"ok": False, "reason": "payload_too_large"})
            except ValueError:
                raise HTTPException(status_code=400, detail={"ok": False, "reason": "bad_content_length"})
        raw = await request.body()
        if len(raw) > _KEEP_BODY_MAX_BYTES:
            raise HTTPException(status_code=413, detail={"ok": False, "reason": "payload_too_large"})
        try:
            body = KeepSessionBody.model_validate_json(raw)
        except Exception:
            raise HTTPException(status_code=422, detail={"ok": False, "reason": "invalid_keep_payload"})
        result = orchestrator.session_service.set_keep(
            session_id,
            keep_pinned=body.keep_pinned,
            keep_note=body.keep_note or "",
        )
        env = core._command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        return JSONResponse(env)

    @router.post("/api/sessions/{session_id}/model")
    def api_set_model(session_id: str, body: ModelBody) -> JSONResponse:
        result = orchestrator.session_service.set_model(session_id, body.model)
        env = core._command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        return JSONResponse(env)

    @router.post("/api/sessions/{session_id}/effort")
    async def api_set_effort(session_id: str, request: Request) -> JSONResponse:
        max_bytes = 256
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise HTTPException(status_code=413, detail={"ok": False, "reason": "payload_too_large"})
            except ValueError:
                raise HTTPException(status_code=400, detail={"ok": False, "reason": "bad_content_length"})
        raw = await request.body()
        if len(raw) > max_bytes:
            raise HTTPException(status_code=413, detail={"ok": False, "reason": "payload_too_large"})
        try:
            body = EffortBody.model_validate_json(raw)
        except Exception:
            raise HTTPException(status_code=422, detail={"ok": False, "reason": "invalid_effort"})
        result = orchestrator.session_service.set_effort(session_id, body.effort)
        env = core._command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        return JSONResponse(env)

    # --- approvals (Move H) — durable approval gate -----------------------

    @router.post("/api/sessions/{session_id}/upload")
    async def api_upload_file(
        session_id: str,
        file: UploadFile = File(...),
        instruction: Optional[str] = Form(default=None),
    ) -> JSONResponse:
        """Upload a file to the session's uploads/ directory.

        Local sessions write directly to session.repo_path/uploads/. Remote mesh
        sessions mirror Telegram caption semantics: staged files ride as task
        metadata so the owning worker pulls them before backend execution.
        """
        session = orchestrator.session_service.store.get(session_id)
        if session is None:
            raise core._upload_error(404, "session_not_found")
        if not session.repo_path:
            raise core._upload_error(400, "no_repo_path")

        raw_name = file.filename or "upload"
        content = await file.read()
        return JSONResponse(await core._store_session_upload(
            orchestrator, session, raw_name, content, instruction=instruction))

    # --- inspect / jobs / git (U3.5 tier 2 — thin wraps over existing services) ---

    @router.post("/api/sessions/{session_id}/inspect")
    async def api_inspect(session_id: str, body: InspectBody) -> JSONResponse:
        """Run a repo inspection op routed to the session's owning node — the same
        NodeInspector path Telegram uses. Read-only."""
        session = orchestrator.session_service.store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        params = {k: v for k, v in {
            "path": body.path, "limit": body.limit, "sort_by_recent": body.sort_by_recent,
        }.items() if v is not None}
        from src.control.node_inspector import InspectError, get_inspector
        try:
            result = await get_inspector().run(session, body.op, params)
        except InspectError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return JSONResponse(result)

    return router
