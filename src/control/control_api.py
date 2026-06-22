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

Read-only for U1. Write endpoints (``submit_instruction`` / ``SessionService``)
arrive in U3; WS/SSE push in U4. The HTML shell and standalone launcher are gone.

All ``/api/*`` endpoints require ``Authorization: Bearer {DASHBOARD_TOKEN}``
(falls back to ``WORKER_TOKEN``).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.core import observability

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
