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

import json
import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse, StreamingResponse
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


class InspectBody(BaseModel):
    op: str
    path: Optional[str] = None
    limit: Optional[int] = None
    sort_by_recent: Optional[bool] = None


class GitCommitBody(BaseModel):
    task_id: str
    task_description: Optional[str] = None
    create_branch: bool = True
    push_branch: bool = False


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


def _control_api_docs_enabled() -> bool:
    """Whether to expose the interactive Swagger/ReDoc/OpenAPI endpoints.

    Off by default — those endpoints are unauthenticated and leak the full API
    shape. Set CONTROL_API_DOCS=true to re-enable them for local development.
    """
    import os
    return os.getenv("CONTROL_API_DOCS", "").lower() == "true"


async def event_stream_frames(
    *,
    since: int = 0,
    is_disconnected=None,
    sleep=None,
    max_iterations: Optional[int] = None,
):
    """Async generator of SSE frame strings tailing events.ndjson (U4).

    Extracted from the route so it is testable without the HTTP transport
    (Starlette's TestClient buffers streaming responses and can't drive an endless
    stream). ``is_disconnected`` is an async predicate to stop on client hangup;
    ``sleep`` is the inter-poll await (injectable); ``max_iterations`` bounds the
    loop in tests. Each frame is ``data: {...}\\n\\n`` or an SSE comment keep-alive.
    """
    import asyncio as _asyncio

    if is_disconnected is None:
        async def is_disconnected():  # pragma: no cover - default never disconnects
            return False
    if sleep is None:
        sleep = lambda: _asyncio.sleep(1.0)  # noqa: E731

    offset = since
    data = observability.read_recent_events(since_offset=offset)
    offset = data.get("offset", offset)
    if data.get("events"):
        yield f"data: {json.dumps(data)}\n\n"
    else:
        yield ": connected\n\n"

    iterations = 0
    while True:
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        if await is_disconnected():
            break
        data = observability.read_recent_events(since_offset=offset)
        if data.get("events"):
            yield f"data: {json.dumps(data)}\n\n"
            offset = data.get("offset", offset)
        else:
            yield ": keep-alive\n\n"
        await sleep()


def _bearer_from_header(request) -> Optional[str]:
    """Extract a Bearer token from the Authorization header, if present."""
    auth = request.headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return None


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
    # Disable FastAPI's built-in (unauthenticated) docs endpoints. /docs, /redoc and
    # /openapi.json leak the full API shape to anyone who can reach the port and have
    # no reason to be open even on the tailnet (defense in depth). The human-facing
    # map lives in docs/ARCHITECTURE.md; a developer who wants live Swagger can flip
    # CONTROL_API_DOCS=true to re-enable them locally.
    _docs_on = _control_api_docs_enabled()
    app = FastAPI(
        title="AI-Team Control API",
        version="1.0",
        docs_url="/docs" if _docs_on else None,
        redoc_url="/redoc" if _docs_on else None,
        openapi_url="/openapi.json" if _docs_on else None,
    )
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
    def api_tasks(
        limit: int = Query(50, ge=1, le=500),
        sectioned: bool = Query(False),
    ) -> JSONResponse:
        """Task list. Default = flat ``{tasks:[...]}`` (UI-2 shape, unchanged).

        ``?sectioned=true`` (Move G′) returns the supervised lifecycle: each task
        gains a derived ``ui_state`` + ``section``, grouped into
        ``{sections: {attention, running, queued, recent}}``. The supervised state
        overlays the owning session's status onto the raw mesh status (e.g. an
        in-flight task whose session AWAITING_INPUT → ``waiting_for_input``), which
        the flat mesh status alone cannot express.
        """
        db = _db()
        tasks = db.list_tasks(limit=limit) if db is not None else []
        if not sectioned:
            return JSONResponse({"tasks": tasks})

        from src.core.task_lifecycle import derive_task_state, section_for_state

        # One bounded read → {session_id: session_status} for the overlay. Avoids
        # an N-query join; missing sessions (oneoff / pruned) overlay as None.
        session_status: Dict[str, str] = {}
        try:
            for v in orchestrator.session_service.list_views(limit=500):
                d = v.to_dict()
                if d.get("session_id"):
                    session_status[d["session_id"]] = d.get("status")
        except Exception as e:
            logger.warning("control_api_tasks_session_overlay_failed err=%s", e)

        sections: Dict[str, List[Dict[str, Any]]] = {
            "attention": [], "running": [], "queued": [], "recent": [],
        }
        for t in tasks:
            sess_status = session_status.get(t.get("session_id")) if t.get("session_id") else None
            ui_state = derive_task_state(t.get("status", ""), sess_status)
            section = section_for_state(ui_state)
            t = {**t, "ui_state": ui_state, "section": section}
            sections[section].append(t)
        return JSONResponse({"sections": sections})

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

    @app.get("/api/events/stream")
    async def api_events_stream(
        request: Request,
        since: int = Query(0, ge=0),
        token: Optional[str] = Query(default=None),
    ) -> StreamingResponse:
        """Server-Sent Events stream of the event log (U4).

        Tails ``events.ndjson`` via the same ``read_recent_events`` reader the poll
        uses, so it sees ALL events — including remote worker events that only land
        in the shared file (the forward-compatible seam for the future broker-backed
        bus; see CONTROL_SURFACE_UNIFICATION §12). Auth is via the ``token`` query
        param because the browser ``EventSource`` API cannot set an Authorization
        header. Each frame: ``data: {"events": [...], "offset": N}``.
        """
        expected = _dashboard_token()
        if not expected:
            raise HTTPException(status_code=500, detail="DASHBOARD_TOKEN not configured")
        supplied = token or _bearer_from_header(request)
        if supplied != expected:
            raise HTTPException(status_code=401, detail="Invalid token")

        return StreamingResponse(
            event_stream_frames(since=since, is_disconnected=request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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

        session = None
        if body.session_id:
            session = orchestrator.session_service.store.get(body.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="session_not_found")
            # Status write (BUSY + last_user_message) lives on the service.
            orchestrator.session_service.mark_busy(
                session.session_id, last_user_message=body.description)
            session = orchestrator.session_service.store.get(session.session_id)
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
            if cancelled:
                # Status write lives on the service (parity with Telegram /session_cancel).
                orchestrator.session_service.mark_cancelled(session_id)
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

    # --- inspect / jobs / git (U3.5 tier 2 — thin wraps over existing services) ---

    @app.post("/api/sessions/{session_id}/inspect", dependencies=[Depends(_require_auth)])
    async def api_inspect(session_id: str, body: InspectBody) -> JSONResponse:
        """Run a repo inspection op routed to the session's owning node — the same
        NodeInspector path Telegram uses. Read-only."""
        session = orchestrator.session_service.store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        params = {k: v for k, v in {
            "path": body.path, "limit": body.limit, "sort_by_recent": body.sort_by_recent,
        }.items() if v is not None}
        from src.control.node_inspector import get_inspector, InspectError
        try:
            result = await get_inspector().run(session, body.op, params)
        except InspectError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return JSONResponse(result)

    @app.get("/api/jobs", dependencies=[Depends(_require_auth)])
    def api_jobs(limit: int = Query(20, ge=1, le=50)) -> JSONResponse:
        db = _db()
        if db is None:
            return JSONResponse({"running": [], "recent": []})
        running = db.list_jobs(status="running", limit=limit)
        recent = db.list_jobs(limit=limit)
        return JSONResponse({"running": running, "recent": recent})

    @app.post("/api/git/status", dependencies=[Depends(_require_auth)])
    def api_git_status() -> JSONResponse:
        from src.services.git_automation import GitAutomationService
        return JSONResponse(GitAutomationService().get_git_status_summary())

    @app.post("/api/git/commit", dependencies=[Depends(_require_auth)])
    def api_git_commit(body: GitCommitBody) -> JSONResponse:
        from src.services.git_automation import GitAutomationService
        result = GitAutomationService().safe_commit_task(
            task_id=body.task_id,
            task_description=body.task_description or f"Task {body.task_id} changes",
            create_branch=body.create_branch,
            push_branch=body.push_branch,
        )
        return JSONResponse(result)

    @app.post("/api/git/commit_all", dependencies=[Depends(_require_auth)])
    def api_git_commit_all(body: GitCommitBody) -> JSONResponse:
        from src.services.git_automation import GitAutomationService
        result = GitAutomationService().commit_all_staged(
            task_id=body.task_id,
            task_description=body.task_description or f"Task {body.task_id} changes",
            create_branch=body.create_branch,
            push_branch=body.push_branch,
        )
        return JSONResponse(result)

    # --- serve the built Web UI from the gateway (U5) ---------------------
    _mount_web_ui(app)

    return app


def _web_dist_dir() -> "Path":
    """Path to the built Web UI (web/dist), relative to the repo root."""
    from pathlib import Path
    # control_api.py is src/control/control_api.py → repo root is parents[2].
    return Path(__file__).resolve().parents[2] / "web" / "dist"


def _mount_web_ui(app: FastAPI) -> None:
    """Serve web/dist at / with the DASHBOARD_TOKEN injected into index.html (U5).

    The gateway, reachable only over the tailnet (bind host), bakes the token into
    the served page so a trusted device needs no token prompt — while /api/* still
    enforces the token (defense in depth). The token is injected as
    ``window.__DASHBOARD_TOKEN__``; the UI's auth store reads it and skips the gate.
    A built UI is optional: if web/dist is absent (dev — vite serves the UI and
    proxies /api here), the mount is skipped silently.
    """
    from pathlib import Path
    from fastapi.responses import HTMLResponse, FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = _web_dist_dir()
    index_file = dist / "index.html"
    if not index_file.exists():
        logger.info("event=web_ui_not_mounted reason=no_dist dir=%s", dist)
        return

    def _index_html() -> str:
        html = index_file.read_text(encoding="utf-8")
        token = _dashboard_token() or ""
        # Inject BEFORE the first <script> so the global exists before the app boots.
        inject = (
            f'<script>window.__DASHBOARD_TOKEN__ = {json.dumps(token)};</script>'
        )
        if "<head>" in html:
            return html.replace("<head>", "<head>" + inject, 1)
        return inject + html

    # Static assets (JS/CSS/img) served directly from web/dist/assets.
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    # include_in_schema=False: these serve HTML, not JSON, and their HTMLResponse
    # return annotation breaks OpenAPI schema generation (the /openapi.json 500 seen
    # when CONTROL_API_DOCS=true). Excluding them keeps the schema buildable.
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def _web_index() -> HTMLResponse:
        return HTMLResponse(_index_html())

    # SPA fallback: any non-/api, non-asset path returns index (client-side routing).
    dist_resolved = dist.resolve()

    @app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
    def _web_spa(full_path: str) -> HTMLResponse:
        # Let real files (favicon, manifest, …) resolve if present; else SPA index.
        # SECURITY: confine the resolved path to web/dist. ``full_path`` is
        # attacker-controlled and may contain ``..`` / percent-encoded ``..`` that
        # the router does not normalize; without this check, a request like
        # ``/%2e%2e/%2e%2e/.env`` would escape web/dist and serve arbitrary files
        # (unauthenticated — this route has no token). On any escape, fall through
        # to the SPA index rather than serving the file.
        if full_path:
            candidate = (dist / full_path).resolve()
            if (candidate == dist_resolved or dist_resolved in candidate.parents) \
                    and candidate.is_file():
                return FileResponse(str(candidate))  # type: ignore[return-value]
        return HTMLResponse(_index_html())

    logger.info("event=web_ui_mounted dir=%s", dist)


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
