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

import asyncio
import json
import logging
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Security, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from src.core import observability

# Map a CommandResult.reason (stable machine code) to an HTTP status. The body
# always still carries {ok, reason} so the client owns the wording (no prose here).
_REASON_STATUS = {
    "unknown_backend": 400,
    "unknown_model": 400,
    "invalid_repo_path": 400,
    "session_not_found": 404,
    "not_closed": 409,
    # Move H — approvals
    "not_found": 404,
    "already_resolved": 409,
    "invalid_decision": 400,
    "missing_action": 400,
    # Upload
    "no_repo_path": 400,
    "dangerous_extension": 400,
    "file_too_large": 413,
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


class ApprovalRequestBody(BaseModel):
    action: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    risk: str = "medium"
    reversible: bool = True
    requested_by: str = ""


class ApprovalResolveBody(BaseModel):
    decision: str  # "approved" | "rejected"
    resolved_by: str = ""


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


class UploadResult(BaseModel):
    ok: bool
    filename: str
    size: int
    path: str


def _session_payload(session) -> Optional[Dict[str, Any]]:
    """Render a Session as the canonical SessionView dict (or None)."""
    if session is None:
        return None
    from src.core.view_models import SessionView
    return SessionView.from_session(session).to_dict()


def _command_envelope(result) -> Dict[str, Any]:
    """Uniform JSON for a CommandResult: {ok, reason, session}."""
    env = {
        "ok": result.ok,
        "reason": result.reason,
        "session": _session_payload(result.session),
    }
    detail = getattr(result, "detail", "")
    if detail:
        env["detail"] = detail
    return env

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


def _telemetry_store():
    db = _db()
    if db is None:
        return None
    from src.control.telemetry_store import TelemetryStore
    return TelemetryStore(db)


def _results_dir() -> "Path":
    """The artifact directory (config.system.results_dir), as a Path."""
    from pathlib import Path
    try:
        from config import config as _cfg
        return Path(_cfg.system.results_dir)
    except Exception:
        return Path("results")


def _sessions_dir() -> "Path":
    """The per-session record directory (``state/sessions/<id>.json``).

    Anchored the same way SessionStore does (``<project_root>/state/sessions``).
    This is the conversation source of truth — each record's ``task_history`` holds
    the full per-turn user_message + result_summary the transcript reader serves.
    """
    from pathlib import Path
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "state" / "sessions"


def _list_projects_for_node(node_id: str, limit: int = 20) -> list:
    """List discoverable repos for a node. Mirrors TelegramInterface._repo_choices_for_node.

    Local (__local__): scans PathResolver root for git dirs (same logic as the Telegram
    wizard). Remote: reads the DB node row's `repos` JSON (populated by the worker's
    heartbeat). Returns [{name, path}].
    """
    if node_id == "__local__":
        try:
            from src.services.path_resolver import PathResolver
            from pathlib import Path as _Path
            resolver = PathResolver.from_config()
            root = resolver.base_cwd or resolver.allowed_root
            if not root:
                return []
            root_path = _Path(root).resolve()
            children = [
                c for c in root_path.iterdir()
                if c.is_dir() and not c.name.startswith(".")
            ]
            children.sort(key=lambda c: c.stat().st_mtime, reverse=True)
            repos = [c for c in children if (c / ".git").exists()]
            if len(repos) < limit:
                seen = {c.resolve() for c in repos}
                for c in children:
                    if c.resolve() not in seen:
                        repos.append(c)
                        seen.add(c.resolve())
                        if len(repos) >= limit:
                            break
            return [{"name": c.name, "path": str(c.resolve())} for c in repos[:limit]]
        except Exception as e:
            logger.warning("_list_projects_for_node local err=%s", e)
            return []
    else:
        try:
            import json as _json
            db = _db()
            if db is None:
                return []
            row = db.get_node(node_id)
            if row:
                repos = _json.loads(row.get("repos") or "[]")
                return [{"name": r["name"], "path": r["path"]} for r in repos[:limit]]
        except Exception as e:
            logger.warning("_list_projects_for_node remote node=%s err=%s", node_id, e)
        return []


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
    #
    # CONC-1: the prior implementation was check-then-act (get → miss → execute →
    # put) with no locking, which assumed retries arrive sequentially. FastAPI runs
    # sync (and awaits async) endpoints concurrently, so two requests sharing a key
    # could both miss the cache and both execute the side effect. Fixing get/put
    # atomicity alone is NOT enough — the whole get→execute→put sequence must be
    # serialized PER KEY. We hand out a per-key lock (created under a small registry
    # lock); same-key requests serialize on it so the second caller blocks until the
    # first stores its response, then hits the cache. Different keys never contend.
    _idem: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
    _IDEM_MAX = 512
    _idem_registry_lock = threading.Lock()
    _idem_key_locks: "OrderedDict[tuple, threading.Lock]" = OrderedDict()

    def _idem_put(route: str, key: Optional[str], resp: Dict[str, Any]) -> None:
        if not key:
            return
        with _idem_registry_lock:
            _idem[(route, key)] = resp
            while len(_idem) > _IDEM_MAX:
                _idem.popitem(last=False)

    def _lock_is_free(lock: Any) -> bool:
        """True if the lock is not currently held. Both threading.Lock and
        asyncio.Lock expose .locked(); default to 'held' if some exotic lock
        doesn't, so we never evict something we can't prove is free."""
        try:
            return not lock.locked()
        except Exception:
            return False

    def _idem_lock_for(route: str, key: str, factory) -> Any:
        """Fetch-or-create the per-key lock for (route, key) under the registry lock.

        ``factory`` builds the lock (threading.Lock for sync endpoints, asyncio.Lock
        for the async one) so the same registry serves both without holding the wrong
        lock type across an ``await``."""
        rk = (route, key)
        with _idem_registry_lock:
            lock = _idem_key_locks.get(rk)
            if lock is None:
                lock = factory()
                # Move-to-end so a freshly created/used lock is the youngest.
                _idem_key_locks[rk] = lock
                # Bound the registry, but NEVER evict a lock that is currently held
                # — evicting it would let a concurrent same-key request mint a fresh
                # lock and run in parallel, defeating the guard. We scan from oldest
                # and drop only free locks; a registry full of held locks is allowed
                # to exceed _IDEM_MAX transiently (it drains as work completes).
                if len(_idem_key_locks) > _IDEM_MAX:
                    for ek in list(_idem_key_locks.keys()):
                        if len(_idem_key_locks) <= _IDEM_MAX:
                            break
                        if ek == rk:
                            continue
                        el = _idem_key_locks[ek]
                        if _lock_is_free(el):
                            del _idem_key_locks[ek]
            else:
                _idem_key_locks.move_to_end(rk)
            return lock

    @contextmanager
    def _idem_guard(route: str, key: Optional[str]):
        """Serialize the check-execute-store sequence for a single (route, key) in a
        SYNC endpoint (FastAPI runs these in a threadpool, so a blocking lock is safe).

        Yields the cached response if one already exists (caller returns it), else
        None (caller executes, then calls _idem_put). Holding the per-key lock across
        the caller's work is what makes idempotency concurrency-safe."""
        if not key:
            yield None
            return
        lock = _idem_lock_for(route, key, threading.Lock)
        with lock:
            with _idem_registry_lock:
                cached = _idem.get((route, key))
            yield cached

    @asynccontextmanager
    async def _idem_guard_async(route: str, key: Optional[str]):
        """Async counterpart of _idem_guard for ``async def`` endpoints, which run ON
        the event loop. A threading.Lock held across ``await`` would block the loop
        thread and deadlock a second same-key request; an asyncio.Lock yields instead."""
        if not key:
            yield None
            return
        lock = _idem_lock_for(route, key, asyncio.Lock)
        async with lock:
            with _idem_registry_lock:
                cached = _idem.get((route, key))
            yield cached

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
        ``{sections: {attention, running, queued, failed, recent}}``. The supervised state
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
            "attention": [], "running": [], "queued": [], "failed": [], "recent": [],
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

    # --- artifacts / files (UI-4) -----------------------------------------
    # The phone review loop: "what did the agent change?" Reads the on-disk
    # results/<task_id>.json artifacts via the pure src.control.artifacts helpers
    # (confined to results_dir — path-traversal rejected like the SPA resolver).

    @app.get("/api/artifacts", dependencies=[Depends(_require_auth)])
    def api_artifacts(limit: int = Query(50, ge=1, le=500)) -> JSONResponse:
        """Newest-first artifact summaries. Canonical source is mesh_tasks (DB);
        falls back to results/*.json only when the DB is unavailable."""
        from src.control import artifacts as _artifacts
        from src.control.db import get_db
        rows = _artifacts.list_artifacts_db(get_db(), limit=limit)
        if rows is None:
            rows = _artifacts.list_artifacts(_results_dir(), limit=limit)
        return JSONResponse({"artifacts": rows})

    @app.get("/api/artifacts/{task_id}", dependencies=[Depends(_require_auth)])
    def api_artifact(task_id: str) -> JSONResponse:
        """One artifact's full header + normalized changed files (RemoteFile rows).

        Canonical source is mesh_tasks (DB); falls back to the results/*.json file
        only when the DB has no such task. 404 (``not_found``) on a missing id OR a
        path-traversal escape — the confined file read collapses both to None."""
        from src.control import artifacts as _artifacts
        from src.control.db import get_db
        artifact = _artifacts.get_artifact_db(get_db(), task_id)
        if artifact is None:
            artifact = _artifacts.get_artifact(_results_dir(), task_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="not_found")
        return JSONResponse({
            "artifact": artifact,
            "files": _artifacts.to_remote_files(artifact),
        })

    @app.get("/api/sessions/{session_id}/messages", dependencies=[Depends(_require_auth)])
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
            _results_dir(), _sessions_dir(), session_id, limit=limit
        )
        if turns is None:
            raise HTTPException(status_code=404, detail="not_found")
        return JSONResponse({"messages": turns})

    @app.get("/api/sessions/{session_id}/timeline", dependencies=[Depends(_require_auth)])
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
        db = _db()
        session = orchestrator.session_service.store.get(session_id)
        session_row = _session_payload(session) if session is not None else None
        from src.control.session_timeline import build_session_timeline
        response = build_session_timeline(
            db=db,
            telemetry_store=_telemetry_store(),
            session_id=session_id,
            session_row=session_row,
            limit=limit,
            cursor=cursor,
        )
        return JSONResponse(response.model_dump(mode="json"))

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

    @app.get("/api/turns", dependencies=[Depends(_require_auth)])
    def api_turns(
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        backend: Optional[str] = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> JSONResponse:
        store = _telemetry_store()
        turns = (
            store.list_turns(
                session_id=session_id,
                status=status,
                backend=backend,
                limit=limit,
            )
            if store is not None
            else []
        )
        return JSONResponse({"turns": turns})

    @app.get("/api/turns/{turn_id}", dependencies=[Depends(_require_auth)])
    def api_turn_detail(turn_id: str) -> JSONResponse:
        store = _telemetry_store()
        turn = store.get_turn(turn_id) if store is not None else None
        if turn is None:
            raise HTTPException(status_code=404, detail="turn_not_found")
        return JSONResponse(turn)

    @app.get("/api/turns/{turn_id}/diagnostics", dependencies=[Depends(_require_auth)])
    def api_turn_diagnostics(turn_id: str) -> JSONResponse:
        store = _telemetry_store()
        diagnostics = store.diagnostics(turn_id) if store is not None else None
        if diagnostics is None:
            raise HTTPException(status_code=404, detail="turn_not_found")
        return JSONResponse(diagnostics)

    @app.get("/api/turns/{turn_id}/graph", dependencies=[Depends(_require_auth)])
    def api_turn_graph(turn_id: str, expand_tools: bool = False) -> JSONResponse:
        store = _telemetry_store()
        graph = store.graph(turn_id, expand_tools=expand_tools) if store is not None else None
        if graph is None:
            raise HTTPException(status_code=404, detail="turn_not_found")
        return JSONResponse(graph)

    @app.get("/api/turns/{turn_id}/events", dependencies=[Depends(_require_auth)])
    def api_turn_events(
        turn_id: str,
        after: Optional[str] = None,
        limit: int = Query(500, ge=1, le=5000),
    ) -> JSONResponse:
        store = _telemetry_store()
        if store is None or store.get_turn(turn_id) is None:
            raise HTTPException(status_code=404, detail="turn_not_found")
        return JSONResponse({"events": store.list_events(turn_id, after=after, limit=limit)})

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
        async with _idem_guard_async("instructions", idempotency_key) as cached:
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
        with _idem_guard("create_session", idempotency_key) as cached:
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

    # --- approvals (Move H) — durable approval gate -----------------------

    def _approval_service():
        """The orchestrator's ApprovalService if it wired one (with a real
        on-approve dispatch callback); else a queue-only service over the shared
        DB. Built lazily so the stub orchestrator in tests works too."""
        svc = getattr(orchestrator, "approval_service", None)
        if svc is not None:
            return svc
        db = _db()
        if db is None:
            return None
        from src.services.approval_service import ApprovalService
        return ApprovalService(db)

    @app.get("/api/approvals", dependencies=[Depends(_require_auth)])
    def api_list_approvals(
        status: Optional[str] = Query(default="pending"),
        limit: int = Query(50, ge=1, le=200),
    ) -> JSONResponse:
        """The queryable pending queue (rebuilds the UI after a restart). Pass
        ``status=`` (empty) to list all."""
        svc = _approval_service()
        if svc is None:
            return JSONResponse({"approvals": []})
        approvals = svc.list(status=status or None, limit=limit)
        return JSONResponse({"approvals": approvals})

    @app.post("/api/approvals", dependencies=[Depends(_require_auth)])
    def api_request_approval(
        body: ApprovalRequestBody,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        """Record a pending approval (the seam a gated action calls). Exposed so the
        queue is exercisable end-to-end before a backend emits these natively."""
        with _idem_guard("request_approval", idempotency_key) as cached:
            if cached is not None:
                return JSONResponse(cached)
            svc = _approval_service()
            if svc is None:
                raise HTTPException(status_code=503, detail="approvals_unavailable")
            result = svc.request(
                action=body.action, session_id=body.session_id, task_id=body.task_id,
                risk=body.risk, reversible=body.reversible, requested_by=body.requested_by,
            )
            if not result.ok:
                raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400),
                                    detail={"ok": False, "reason": result.reason})
            approval_id = result.reason  # request() carries the id on reason
            resp = {"ok": True, "approval": svc.get(approval_id)}
            _idem_put("request_approval", idempotency_key, resp)
            return JSONResponse(resp)

    @app.post("/api/approvals/{approval_id}/resolve", dependencies=[Depends(_require_auth)])
    async def api_resolve_approval(approval_id: str, body: ApprovalResolveBody) -> JSONResponse:
        """Approve or reject a pending approval. The guarded transition makes a
        double-resolve return 409 (already_resolved), not a second dispatch."""
        svc = _approval_service()
        if svc is None:
            raise HTTPException(status_code=503, detail="approvals_unavailable")
        result = await svc.resolve(approval_id, body.decision, resolved_by=body.resolved_by)
        if not result.ok:
            raise HTTPException(status_code=_REASON_STATUS.get(result.reason, 400),
                                detail={"ok": False, "reason": result.reason})
        return JSONResponse({"ok": True, "reason": "", "approval": svc.get(approval_id)})

    # --- projects / models / upload (Telegram parity) ---

    @app.get("/api/projects", dependencies=[Depends(_require_auth)])
    def api_projects(
        node_id: str = Query("__local__"),
        limit: int = Query(20, ge=1, le=50),
    ) -> JSONResponse:
        """Discoverable repos for a node. Drives the web repo picker (parity with
        the Telegram /session_new guided wizard). Local: scans WORKER_PROJECTS_ROOT /
        PathResolver root. Remote: reads the node's advertised repos from the DB."""
        projects = _list_projects_for_node(node_id, limit=limit)
        return JSONResponse({"projects": projects})

    @app.get("/api/models", dependencies=[Depends(_require_auth)])
    def api_models(
        backend: Optional[str] = Query(default=None),
    ) -> JSONResponse:
        """Model catalog for a backend (or all backends). Drives the web model picker
        (parity with Telegram /model). Static catalog from config/models.py."""
        from config.models import BACKEND_MODELS, options as _options
        if backend:
            opts = _options(backend)
            return JSONResponse({
                "backend": backend,
                "models": [{"name": o.name, "is_default": o.is_default} for o in opts],
            })
        result = {}
        for be, opts_list in BACKEND_MODELS.items():
            result[be] = [{"name": o.name, "is_default": o.is_default} for o in opts_list]
        return JSONResponse({"models": result})

    @app.post("/api/sessions/{session_id}/upload", dependencies=[Depends(_require_auth)])
    async def api_upload_file(session_id: str, file: UploadFile) -> JSONResponse:
        """Upload a file to session.repo_path/uploads/ (local sessions). Mirrors
        TelegramInterface._handle_document for local sessions. Remote (mesh) sessions
        are not supported in v1 — the client should check session.workspace.targetId."""
        import re as _re
        import os as _os
        from pathlib import Path as _Path

        session = orchestrator.session_service.store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        if not session.repo_path:
            raise HTTPException(status_code=400, detail="no_repo_path")

        raw_name = file.filename or "upload"
        ext = _os.path.splitext(raw_name)[1].lower()
        _BLOCKED_EXTS = {
            ".exe", ".bat", ".cmd", ".com", ".msi", ".msp", ".scr", ".pif",
            ".vbs", ".vbe", ".ps1", ".psm1", ".psd1", ".wsf", ".wsh", ".hta",
            ".jar", ".dll", ".reg", ".lnk",
        }
        if ext in _BLOCKED_EXTS:
            raise HTTPException(status_code=400, detail="dangerous_extension")

        try:
            from config import config as _cfg
            max_mb = getattr(_cfg.telegram, "upload_max_mb", 0)
        except Exception:
            max_mb = 0

        safe_name = _re.sub(r"[^\w.\-]", "_", raw_name)[:200] or "upload"
        if not safe_name.strip("._"):
            safe_name = "upload"

        upload_dir = _Path(session.repo_path) / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = (upload_dir / safe_name).resolve()

        # Path-traversal guard: dest must stay inside upload_dir.
        if not str(dest).startswith(str(upload_dir.resolve())):
            raise HTTPException(status_code=400, detail="dangerous_extension")

        content = await file.read()
        if max_mb > 0 and len(content) > max_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail="file_too_large")

        # Deduplicate filename if it already exists.
        if dest.exists():
            stem, suffix = _os.path.splitext(safe_name)
            counter = 1
            while dest.exists():
                dest = (upload_dir / f"{stem}_{counter}{suffix}").resolve()
                counter += 1
            safe_name = dest.name

        dest.write_bytes(content)
        logger.info(
            "event=web_upload session=%s file=%s size=%d", session_id, safe_name, len(content)
        )
        return JSONResponse({
            "ok": True,
            "filename": safe_name,
            "size": len(content),
            "path": f"uploads/{safe_name}",
        })

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
        list_watched_jobs = getattr(orchestrator, "list_watched_jobs", None)
        if callable(list_watched_jobs):
            return JSONResponse(list_watched_jobs(limit=limit))

        db = _db()
        if db is None:
            return JSONResponse({"running": [], "recent": []})
        running = db.list_jobs(status="running", limit=limit)
        recent = db.list_jobs(limit=limit)
        return JSONResponse({"running": running, "recent": recent})

    @app.get("/api/mesh/health", dependencies=[Depends(_require_auth)])
    def api_mesh_health(limit: int = Query(24, ge=1, le=200)) -> JSONResponse:
        """Read-only mesh health trend and reconcile backlog for the Web UI."""
        db = _db()
        current: Dict[str, Any] = {}
        recent: List[Dict[str, Any]] = []
        if db is not None:
            try:
                current = db.stats()
                recent = db.list_mesh_health_samples(limit=limit)
            except Exception as e:
                logger.warning("control_api_mesh_health_failed err=%s", e)
        reconcile_status = getattr(orchestrator, "mesh_reconcile_status", None)
        reconcile = (
            reconcile_status()
            if callable(reconcile_status)
            else {
                "total": 0,
                "pending": 0,
                "reconciled": 0,
                "invalid": 0,
                "oldest_pending_at": None,
                "latest_reconciled_at": None,
            }
        )
        return JSONResponse({
            "current": current,
            "history": {"recent": recent},
            "reconcile": reconcile,
        })

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
        # DX-1: an unmatched GET under /api/ is a missing endpoint, not a client
        # route — return a real 404 JSON error instead of letting it fall through
        # to the SPA index (which would 200 with HTML and mask the bug). The named
        # /api/* routes above are registered first and still win; only genuinely
        # unknown /api paths reach here.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
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
