"""Control API — the gateway's own in-process HTTP interface.

U1 of docs/CONTROL_SURFACE_UNIFICATION.md. This replaces the standalone
``dashboard.py`` + ``dashboard_main.py`` process. Built by ``build_control_api``
with the **live orchestrator**, so read handlers call the orchestrator's
in-process services and singletons — never a second ``SessionStore`` and never a
file/DB side-read where an in-process source exists.

Why in-process matters (same argument as ``embedded_server.py``): the gateway's
``get_registry()`` singleton and ``SessionService`` are populated in *this*
process. A separate dashboard process could only re-read ``state/mesh.db`` and
re-derive node liveness by hand; sharing the process removes that whole class of
staleness. Telegram and this HTTP surface are now siblings over the same services.

U3 adds the write surface: thin HTTP adapters over the SAME services Telegram
calls (``submit_instruction`` / ``SessionService`` / ``cancel_task`` /
``compact_session``) — no new business logic. Web sessions are tagged
``SessionOrigin(channel="web")``. WS/SSE push is U4; static serving is U5.

All ``/api/*`` endpoints require ``Authorization: Bearer {DASHBOARD_TOKEN}``
(falls back to ``WORKER_TOKEN``).
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from src.core import observability

# Map a CommandResult.reason (stable machine code) to an HTTP status. The body
# always still carries {ok, reason} so the client owns the wording (no prose here).
_REASON_STATUS = {
    "unknown_backend": 400,
    "unknown_model": 400,
    "session_not_found": 404,
    "not_closed": 409,
}


class InstructionBody(BaseModel):
    description: str
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    target_files: Optional[List[str]] = None


class CreateSessionBody(BaseModel):
    backend: str
    repo_path: str
    model: Optional[str] = None
    node_id: Optional[str] = None


class BindBody(BaseModel):
    chat_id: Optional[int] = None


class ModelBody(BaseModel):
    model: Optional[str] = None


def _session_payload(session) -> Optional[Dict[str, Any]]:
    """Render a Session as the canonical SessionView dict (or None)."""
    if session is None:
        return None
    from src.core.view_models import SessionView
    return SessionView.from_session(session).to_dict()


def _command_envelope(result) -> Dict[str, Any]:
    """Uniform JSON for a CommandResult: {ok, reason, session}."""
    return {
        "ok": result.ok,
        "reason": result.reason,
        "session": _session_payload(result.session),
    }

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth — reuse the mesh secret; dashboard-specific override allowed
# ---------------------------------------------------------------------------

def _dashboard_token() -> str:
    try:
        from config import config as _cfg
        return _cfg.mesh.dashboard_token or _cfg.mesh.worker_token
    except Exception:
        import os
        return os.getenv("DASHBOARD_TOKEN", "") or os.getenv("WORKER_TOKEN", "")


def _heartbeat_timeout_sec() -> int:
    try:
        from config import config as _cfg
        return int(_cfg.mesh.node_heartbeat_timeout_sec)
    except Exception:
        return 90


def _annotate_node_liveness(node: Dict[str, Any]) -> None:
    """Derive ``live`` + ``heartbeat_age_sec`` from ``last_heartbeat`` (DB fallback).

    Only used when the in-process registry is empty (standalone-mesh / fallback
    mode), i.e. when we *must* read the shared DB and cannot trust an in-process
    ``status``. When the registry is populated we use its live nodes directly and
    this is never called — the whole reason it existed (separate-process staleness)
    is gone in the embedded path.
    """
    from datetime import datetime, timezone

    node["live"] = False
    node["heartbeat_age_sec"] = None
    raw = node.get("last_heartbeat")
    if not raw:
        return
    try:
        hb = datetime.fromisoformat(str(raw))
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        age = (now - hb).total_seconds()
        node["heartbeat_age_sec"] = round(age, 1)
        node["live"] = age <= _heartbeat_timeout_sec()
    except Exception:
        return


def _db():
    try:
        from src.control.db import get_db
        return get_db()
    except Exception:
        return None


def build_control_api(orchestrator) -> FastAPI:
    """Build the gateway's read API bound to the live orchestrator.

    ``orchestrator`` must expose ``session_service`` (M1 SessionService). Node and
    task reads use the in-process registry / DB. No state is constructed here.
    """
    app = FastAPI(title="AI-Team Control API", version="1.0")
    _bearer = HTTPBearer(auto_error=True)

    def _require_auth(creds: HTTPAuthorizationCredentials = Security(_bearer)) -> None:
        token = _dashboard_token()
        if not token:
            raise HTTPException(status_code=500, detail="DASHBOARD_TOKEN not configured")
        if creds.credentials != token:
            raise HTTPException(status_code=401, detail="Invalid token")

    # Bounded in-process idempotency cache {(<route>, <key>) -> response dict}.
    # In-process is sufficient: the gateway is a single process (the point of U1).
    _idem: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
    _IDEM_MAX = 512

    def _idem_get(route: str, key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        return _idem.get((route, key))

    def _idem_put(route: str, key: Optional[str], resp: Dict[str, Any]) -> None:
        if not key:
            return
        _idem[(route, key)] = resp
        while len(_idem) > _IDEM_MAX:
            _idem.popitem(last=False)

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/sessions", dependencies=[Depends(_require_auth)])
    def api_sessions(limit: int = Query(200, ge=1, le=1000)) -> JSONResponse:
        try:
            views = orchestrator.session_service.list_views(limit=limit)
            sessions = [v.to_dict() for v in views]
        except Exception as e:
            logger.warning("control_api_sessions_failed err=%s", e)
            sessions = []
        return JSONResponse({"sessions": sessions})

    @app.get("/api/tasks", dependencies=[Depends(_require_auth)])
    def api_tasks(limit: int = Query(50, ge=1, le=500)) -> JSONResponse:
        db = _db()
        tasks = db.list_tasks(limit=limit) if db is not None else []
        return JSONResponse({"tasks": tasks})

    @app.get("/api/nodes", dependencies=[Depends(_require_auth)])
    def api_nodes() -> JSONResponse:
        nodes = _live_nodes()
        return JSONResponse({"nodes": nodes})

    @app.get("/api/events", dependencies=[Depends(_require_auth)])
    def api_events(
        since: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
    ) -> JSONResponse:
        """Live event deltas. Pass the returned ``offset`` back as ``since``.

        ``since=0`` returns the tail (cold start). Gap recovery is NOT a replay —
        the client refreshes state from the read endpoints instead.
        """
        data = observability.read_recent_events(limit=limit, since_offset=since)
        return JSONResponse(data)

    # ----------------------------------------------------------------------
    # Write surface (U3) — thin adapters over the same services Telegram calls.
    # ----------------------------------------------------------------------

    @app.post("/api/instructions", dependencies=[Depends(_require_auth)])
    async def api_instructions(
        body: InstructionBody,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        """Submit an instruction. With session_id it mirrors the Telegram session
        path (session → BUSY, source=web_session); otherwise a one-off."""
        cached = _idem_get("instructions", idempotency_key)
        if cached is not None:
            return JSONResponse(cached)

        from src.core.interfaces import SessionStatus

        session = None
        if body.session_id:
            session = orchestrator.session_service.store.get(body.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="session_not_found")
            session.last_user_message = body.description
            session.status = SessionStatus.BUSY
            orchestrator.session_service.store.save(session)
            task_id = await orchestrator.submit_instruction(
                description=body.description,
                session_id=session.session_id,
                cwd=session.repo_path or body.cwd,
                target_files=body.target_files,
                source="web_session",
            )
            session.last_task_id = task_id
            orchestrator.session_service.store.save(session)
        else:
            task_id = await orchestrator.submit_instruction(
                description=body.description,
                cwd=body.cwd,
                target_files=body.target_files,
                source="web_oneoff",
            )

        resp = {"ok": True, "task_id": task_id, "session": _session_payload(session)}
        _idem_put("instructions", idempotency_key, resp)
        return JSONResponse(resp)

    @app.post("/api/sessions", dependencies=[Depends(_require_auth)])
    def api_create_session(
        body: CreateSessionBody,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        cached = _idem_get("create_session", idempotency_key)
        if cached is not None:
            return JSONResponse(cached)

        from src.core.interfaces import SessionOrigin

        result = orchestrator.session_service.create_session(
            backend=body.backend,
            repo_path=body.repo_path,
            model=body.model,
            node_id=body.node_id or "__local__",
            origin=SessionOrigin(channel="web", kind="user"),
            bind_chat=False,
        )
        env = _command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        _idem_put("create_session", idempotency_key, env)
        return JSONResponse(env)

    @app.post("/api/sessions/{session_id}/bind", dependencies=[Depends(_require_auth)])
    def api_bind_session(session_id: str, body: BindBody) -> JSONResponse:
        if body.chat_id is None:
            # Web has no chat binding today; verify the session exists and echo it.
            session = orchestrator.session_service.store.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="session_not_found")
            return JSONResponse({"ok": True, "reason": "", "session": _session_payload(session)})
        result = orchestrator.session_service.bind_active(body.chat_id, session_id)
        env = _command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        return JSONResponse(env)

    @app.post("/api/sessions/{session_id}/stop", dependencies=[Depends(_require_auth)])
    def api_stop_session(session_id: str) -> JSONResponse:
        session = orchestrator.session_service.store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        cancelled = False
        if session.last_task_id:
            cancelled = bool(orchestrator.cancel_task(session.last_task_id))
        return JSONResponse({"ok": True, "cancelled": cancelled, "task_id": session.last_task_id})

    @app.post("/api/sessions/{session_id}/compact", dependencies=[Depends(_require_auth)])
    async def api_compact_session(session_id: str) -> JSONResponse:
        session = orchestrator.session_service.store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        result = await orchestrator.compact_session(session_id)
        return JSONResponse({
            "ok": bool(getattr(result, "success", False)),
            "output": getattr(result, "output", ""),
            "errors": list(getattr(result, "errors", []) or []),
        })

    @app.post("/api/sessions/{session_id}/close", dependencies=[Depends(_require_auth)])
    async def api_close_session(session_id: str) -> JSONResponse:
        # backend.close may block (local backend) → off-thread, like Telegram.
        import asyncio
        result = await asyncio.to_thread(
            orchestrator.session_service.close_session,
            session_id,
            backends=getattr(orchestrator, "_backends", {}),
        )
        env = _command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        return JSONResponse(env)

    @app.post("/api/sessions/{session_id}/restore", dependencies=[Depends(_require_auth)])
    def api_restore_session(session_id: str) -> JSONResponse:
        result = orchestrator.session_service.restore_session(session_id)
        env = _command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        return JSONResponse(env)

    @app.post("/api/sessions/{session_id}/model", dependencies=[Depends(_require_auth)])
    def api_set_model(session_id: str, body: ModelBody) -> JSONResponse:
        result = orchestrator.session_service.set_model(session_id, body.model)
        env = _command_envelope(result)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400), detail=env)
        return JSONResponse(env)

    return app


def _live_nodes() -> List[Dict[str, Any]]:
    """Prefer the in-process registry; fall back to the DB read with liveness.

    Registry-populated (embedded task server running): every node is live with a
    fresh heartbeat the expiry loop maintains *in this process* — no annotation
    needed. Registry empty (standalone-mesh / fallback): read the shared DB and
    annotate, exactly as the old dashboard did, so behavior is preserved.
    """
    try:
        from src.control.node_registry import get_registry
        reg = get_registry()
        if not reg.is_empty():
            out: List[Dict[str, Any]] = []
            for info in reg.list_all():
                d = info.to_dict()
                d["live"] = d.get("status") == "online"
                age = get_registry()._live_state_age_sec(info)
                d["heartbeat_age_sec"] = round(age, 1) if age is not None else None
                out.append(d)
            return out
    except Exception as e:
        logger.warning("control_api_registry_read_failed err=%s", e)

    db = _db()
    nodes = db.list_nodes() if db is not None else []
    for n in nodes:
        _annotate_node_liveness(n)
    return nodes
