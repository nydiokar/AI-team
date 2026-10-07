"""Monitoring routes — health, tasks, artifacts, turns, events (+ SSE), nodes, mesh, jobs,
catalogs.

Mounted by ``control_api.build_control_api``; route map: docs/backend/ARCHITECTURE.md
"Monitoring — tasks, turns, events, mesh". Module-level helpers are looked up as
``core.<name>`` so tests that monkeypatch ``control_api`` keep reaching these handlers.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.control import control_api as core
from src.control.control_api import (
    _ID_STR_MAX,
    logger,
)
from src.core import observability


def build_router(orchestrator: Any, *, require_auth: Callable[..., Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> Dict[str, Any]:
        # [A53] Surface the effective SDK-driver governor ceilings so the operator
        # can verify the turn/cost cap is actually configured (read-only). None ⇒
        # no cap enforced ⇒ legacy unbounded session.
        governor: Dict[str, Any] = {"sdk_max_turns": None, "sdk_max_budget_usd": None}
        try:
            from config import config as _cfg
            governor["sdk_max_turns"] = getattr(_cfg.claude, "sdk_max_turns", None)
            governor["sdk_max_budget_usd"] = getattr(_cfg.claude, "sdk_max_budget_usd", None)
        except Exception:
            pass
        # [A82 Stage 8a, review F4] Unauthenticated probe: only whether every
        # open session's (carrier, backend) has a live managed carrier, from the
        # gateway's periodic check (no DB read). None ⇒ not checked yet. The
        # node ids / pins / counts live behind auth: /api/turn-queue/coverage.
        missing = getattr(orchestrator, "_managed_carrier_missing", None)
        coverage_ok = (not missing) if isinstance(missing, list) else None
        return {"status": "ok", "governor": governor, "turn_queue": {"coverage_ok": coverage_ok}}

    @router.get("/api/turn-queue/coverage", dependencies=[Depends(require_auth)])
    def api_turn_queue_coverage() -> Dict[str, Any]:
        """[A82 Stage 8a, review F4] The latest managed-carrier coverage check:
        (carrier, pin, backend, sessions) groups no live managed carrier covers
        and the open retired-backend session count. Read from the gateway's
        cached check (startup + periodic); ``checked`` False until it ran."""
        missing = getattr(orchestrator, "_managed_carrier_missing", None)
        retired = getattr(orchestrator, "_retired_backend_sessions", None)
        checked = isinstance(missing, list)
        return {
            "checked": checked,
            "coverage_ok": (not missing) if checked else None,
            "managed_carrier_missing": missing if checked else [],
            "retired_backend_sessions": retired if isinstance(retired, int) else None,
        }

    @router.get("/api/tasks", dependencies=[Depends(require_auth)])
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
        db = core._db()
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

    @router.get("/api/nodes", dependencies=[Depends(require_auth)])
    def api_nodes() -> JSONResponse:
        nodes = core._live_nodes()
        return JSONResponse({"nodes": nodes})

    # --- artifacts / files (UI-4) -----------------------------------------
    # The phone review loop: "what did the agent change?" Reads the on-disk
    # results/<task_id>.json artifacts via the pure src.control.artifacts helpers
    # (confined to results_dir — path-traversal rejected like the SPA resolver).

    @router.get("/api/artifacts", dependencies=[Depends(require_auth)])
    def api_artifacts(limit: int = Query(50, ge=1, le=500)) -> JSONResponse:
        """Newest-first artifact summaries. Canonical source is mesh_tasks (DB);
        falls back to results/*.json only when the DB is unavailable."""
        from src.control import artifacts as _artifacts
        from src.control.db import get_db
        rows = _artifacts.list_artifacts_db(get_db(), limit=limit)
        if rows is None:
            rows = _artifacts.list_artifacts(core._results_dir(), limit=limit)
        return JSONResponse({"artifacts": rows})

    @router.get("/api/artifacts/{task_id}", dependencies=[Depends(require_auth)])
    def api_artifact(task_id: str) -> JSONResponse:
        """One artifact's full header + normalized changed files (RemoteFile rows).

        Canonical source is mesh_tasks (DB); falls back to the results/*.json file
        only when the DB has no such task. 404 (``not_found``) on a missing id OR a
        path-traversal escape — the confined file read collapses both to None."""
        from src.control import artifacts as _artifacts
        from src.control.db import get_db
        artifact = _artifacts.get_artifact_db(get_db(), task_id)
        if artifact is None:
            artifact = _artifacts.get_artifact(core._results_dir(), task_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="not_found")
        return JSONResponse({
            "artifact": artifact,
            "files": _artifacts.to_remote_files(artifact),
        })

    @router.get("/api/events", dependencies=[Depends(require_auth)])
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

    @router.get("/api/turns", dependencies=[Depends(require_auth)])
    def api_turns(
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        backend: Optional[str] = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> JSONResponse:
        store = core._telemetry_store()
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

    # REVISIT (2026-10-05): no UI caller yet (tests only); meant for worker diagnostics.
    @router.get("/api/turns/{turn_id}", dependencies=[Depends(require_auth)])
    def api_turn_detail(turn_id: str) -> JSONResponse:
        store = core._telemetry_store()
        turn = store.get_turn(turn_id) if store is not None else None
        if turn is None:
            raise HTTPException(status_code=404, detail="turn_not_found")
        return JSONResponse(turn)

    # REVISIT (2026-10-05): no UI caller yet (tests only); meant for worker diagnostics.
    @router.get("/api/turns/{turn_id}/diagnostics", dependencies=[Depends(require_auth)])
    def api_turn_diagnostics(turn_id: str) -> JSONResponse:
        store = core._telemetry_store()
        diagnostics = store.diagnostics(turn_id) if store is not None else None
        if diagnostics is None:
            raise HTTPException(status_code=404, detail="turn_not_found")
        return JSONResponse(diagnostics)

    @router.get("/api/turns/{turn_id}/graph", dependencies=[Depends(require_auth)])
    def api_turn_graph(turn_id: str, expand_tools: bool = False) -> JSONResponse:
        store = core._telemetry_store()
        graph = store.graph(turn_id, expand_tools=expand_tools) if store is not None else None
        if graph is None:
            raise HTTPException(status_code=404, detail="turn_not_found")
        return JSONResponse(graph)

    @router.get("/api/turns/{turn_id}/events", dependencies=[Depends(require_auth)])
    def api_turn_events(
        turn_id: str,
        after: Optional[str] = None,
        limit: int = Query(500, ge=1, le=5000),
    ) -> JSONResponse:
        store = core._telemetry_store()
        if store is None or store.get_turn(turn_id) is None:
            raise HTTPException(status_code=404, detail="turn_not_found")
        return JSONResponse({"events": store.list_events(turn_id, after=after, limit=limit)})

    # --- flows (A23) — read-only FlowRun record surface -------------------
    # "let me see the flows": the first payoff of the durable state machine —
    # state you can query, not grep. READ ONLY: nothing here mutates a flow or
    # drives a transition (those are the orchestrator's write path, not an API).
    # Honest fields: every value is served exactly as the DB holds it, so a NULL
    # §11 column serializes as JSON null — never a fabricated default.

    @router.get("/api/events/stream")
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
        if not core._dashboard_token():
            raise HTTPException(status_code=500, detail="DASHBOARD_TOKEN not configured")
        supplied = token or core._bearer_from_header(request)
        if not core._token_accepted(supplied):
            raise HTTPException(status_code=401, detail="Invalid token")

        return StreamingResponse(
            core.event_stream_frames(since=since, is_disconnected=request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ----------------------------------------------------------------------
    # Write surface (U3) — thin adapters over the same services Telegram calls.
    # ----------------------------------------------------------------------

    @router.get("/api/backends/usage", dependencies=[Depends(require_auth)])
    def api_backends_usage() -> JSONResponse:
        """Honest per-backend account/usage view. Emits ONLY provable facts
        (configured/observed model, recent token usage from telemetry) and returns
        null + a reason for limits/reset/identity, which no backend proves. Never
        fabricates quota data."""
        from config import config as _cfg
        from src.backends.registry import valid_backend_names
        from src.services.backend_usage import build_backend_usage

        view = build_backend_usage(
            _cfg,
            valid_backends=list(valid_backend_names()),
            telemetry_store=core._telemetry_store(),
        )
        return JSONResponse(view)

    # --- A65 cost monitoring: explorer / case usage / top spenders / projects ---

    @router.get("/api/projects", dependencies=[Depends(require_auth)])
    def api_projects(
        node_id: str = Query("__local__"),
        limit: int = Query(20, ge=1, le=50),
    ) -> JSONResponse:
        """Discoverable repos for a node. Drives the web repo picker (parity with
        the Telegram /session_new guided wizard). Local: scans WORKER_PROJECTS_ROOT /
        PathResolver root. Remote: reads the node's advertised repos from the DB."""
        projects = core._list_projects_for_node(node_id, limit=limit)
        return JSONResponse({"projects": projects})

    @router.get("/api/models", dependencies=[Depends(require_auth)])
    def api_models(
        backend: Optional[str] = Query(default=None),
        node_id: str = Query("__local__"),
    ) -> JSONResponse:
        """Return the catalog advertised by the selected execution node."""
        from config.models import BACKEND_MODELS
        from config.models import available_options as _options
        node_models: dict[str, list[dict[str, Any]]] = {}
        if node_id != "__local__":
            db = core._db()
            row = db.get_node(node_id) if db is not None else None
            if row:
                try:
                    node_models = json.loads(row.get("model_capabilities") or "{}")
                except (TypeError, ValueError):
                    node_models = {}

        def serialize(model_backend: str) -> list[dict[str, Any]]:
            # A node only advertises backends it actually discovers a live
            # catalog for (today: Codex, via its app-server). Backends the
            # node hasn't advertised fall back to the static gateway catalog
            # instead of going empty.
            if node_id != "__local__" and model_backend in node_models:
                return list(node_models.get(model_backend) or [])
            return [{"name": o.name, "is_default": o.is_default, "efforts": list(o.supported_efforts or [])} for o in _options(model_backend)]

        if backend:
            return JSONResponse({
                "backend": backend,
                "node_id": node_id,
                "models": serialize(backend),
            })
        result = {}
        for be in BACKEND_MODELS:
            result[be] = serialize(be)
        return JSONResponse({"node_id": node_id, "models": result})

    @router.get("/api/jobs", dependencies=[Depends(require_auth)])
    def api_jobs(
        limit: int = Query(20, ge=1, le=50),
        session_id: Optional[str] = Query(default=None, max_length=_ID_STR_MAX),
        ownership: Optional[str] = Query(default=None, pattern="^(all|unowned)$"),
    ) -> JSONResponse:
        ownership_filter = None if ownership in (None, "all") else ownership
        if session_id and ownership_filter == "unowned":
            raise HTTPException(status_code=400, detail="session_id_conflicts_with_unowned")
        list_watched_jobs = getattr(orchestrator, "list_watched_jobs", None)
        if callable(list_watched_jobs):
            return JSONResponse(
                list_watched_jobs(
                    limit=limit,
                    session_id=session_id,
                    ownership=ownership_filter,
                )
            )

        db = core._db()
        if db is None:
            return JSONResponse({"running": [], "recent": []})
        running = db.list_jobs(
            status="running",
            session_id=session_id,
            ownership=ownership_filter,
            limit=limit,
        )
        recent = db.list_jobs(
            session_id=session_id,
            ownership=ownership_filter,
            limit=limit,
        )
        return JSONResponse({"running": running, "recent": recent})

    @router.get("/api/mesh/health", dependencies=[Depends(require_auth)])
    def api_mesh_health(limit: int = Query(24, ge=1, le=200)) -> JSONResponse:
        """Read-only mesh health trend and reconcile backlog for the Web UI."""
        db = core._db()
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

    return router
