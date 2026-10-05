"""Cost, metrics and quota routes.

Mounted by ``control_api.build_control_api``; route map: docs/backend/ARCHITECTURE.md "Cost,
metrics, quota". Module-level helpers are looked up as ``core.<name>`` so tests that
monkeypatch ``control_api`` keep reaching these handlers.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from src.control import app_metrics
from src.control import control_api as core

if TYPE_CHECKING:
    from src.control.db import MeshDB


def build_router(orchestrator: Any, *, require_auth: Callable[..., Any]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_auth)])

    def _cost_db() -> MeshDB:
        db = core._db()
        if db is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "db_unavailable"})
        return db

    @router.get("/api/cost/explorer")
    def api_cost_explorer(
        dimension: str = Query("project"),
        granularity: str = Query("day"),
        from_ts: Optional[str] = Query(default=None, alias="from"),
        to_ts: Optional[str] = Query(default=None, alias="to"),
        repo_path: Optional[str] = Query(default=None),
        limit: int = Query(100, ge=1, le=5000),
    ) -> JSONResponse:
        """Spend explorer: token+USD breakdown along one dimension
        (project|backend|model|role|case|session), optionally bucketed per UTC day
        and filtered by time window + project. Prices per model with coverage %
        surfaced honestly; unmatched models land in an explicit ``<unknown>``
        bucket, never silently dropped."""
        from src.control.cost_read_model import assemble_explorer
        from src.control.db import _COST_DIMENSIONS
        if dimension not in _COST_DIMENSIONS:
            raise HTTPException(status_code=422, detail={"ok": False, "reason": "unknown_dimension"})
        return JSONResponse(
            assemble_explorer(
                _cost_db(),
                dimension=dimension,
                granularity=granularity,
                from_ts=from_ts or None,
                to_ts=to_ts or None,
                repo_path=repo_path or None,
                limit=limit,
            )
        )

    @router.get("/api/cases/{case_id}/usage")
    def api_case_usage(case_id: str) -> JSONResponse:
        """Per-case cost breakdown (the manager/workers split): each session's
        tokens + USD, and the mgr_vs_workers summary. 404 for an unknown case."""
        from src.control.cost_read_model import assemble_case_usage
        result = assemble_case_usage(_cost_db(), case_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"ok": False, "reason": "case_not_found"})
        return JSONResponse(result)

    @router.get("/api/cost/top")
    def api_cost_top(
        by: str = Query("usd"),
        from_ts: Optional[str] = Query(default=None, alias="from"),
        to_ts: Optional[str] = Query(default=None, alias="to"),
        repo_path: Optional[str] = Query(default=None),
        limit: int = Query(10, ge=1, le=100),
    ) -> JSONResponse:
        """Top spenders: sessions ranked by USD (or raw tokens) within the window.
        Every row carries its dominant model + coverage % so an unpriced burner is
        visible, not silent."""
        from src.control.cost_read_model import assemble_top_sessions
        if by not in ("usd", "tokens"):
            raise HTTPException(status_code=422, detail={"ok": False, "reason": "unknown_sort_by"})
        return JSONResponse(
            assemble_top_sessions(
                _cost_db(),
                from_ts=from_ts or None,
                to_ts=to_ts or None,
                repo_path=repo_path or None,
                by=by,
                limit=limit,
            )
        )

    @router.get("/api/cost/projects")
    def api_cost_projects(
        from_ts: Optional[str] = Query(default=None, alias="from"),
        to_ts: Optional[str] = Query(default=None, alias="to"),
        limit: int = Query(200, ge=1, le=1000),
    ) -> JSONResponse:
        """Distinct projects with usage (token rollup), ordered by size — the
        Cost-tab project-filter dropdown fuel."""
        from src.control.cost_read_model import assemble_projects
        return JSONResponse(
            assemble_projects(
                _cost_db(),
                from_ts=from_ts or None,
                to_ts=to_ts or None,
                limit=limit,
            )
        )

    @router.get("/api/cost/alerts")
    def api_cost_alerts() -> JSONResponse:
        """A65 P3 cost budget/burn-rate alerts. Billable-USD only — never raw
        token/cache-read volume. Thresholds are the env knobs
        COST_ALERT_DAILY_BUDGET_USD / COST_ALERT_SESSION_BURN_USD /
        COST_ALERT_CASE_TOTAL_USD (0 = off); an alert fires when the read-model's
        known USD crosses a set knob. Read-only: alerts surface the existing SDK
        governor ceiling (``sdk_max_budget_usd``) as the enforcement lever and
        never add a new kill mechanism; enforcement stays flag-gated OFF."""
        from src.control.db import runtime_flag_enabled
        from src.services.cost_alerts import check_cost_alerts

        governor_budget: Optional[float] = None
        try:
            from config import config as _cfg
            governor_budget = getattr(_cfg.claude, "sdk_max_budget_usd", None)
        except Exception:
            pass
        result = check_cost_alerts(_cost_db())
        result["enforcement"] = {
            "enabled": runtime_flag_enabled("COST_ALERT_ENFORCE_ENABLED"),
            "mechanism": "sdk_max_budget_usd",
            "governor_sdk_max_budget_usd": governor_budget,
        }
        return JSONResponse(result)

    # REVISIT (2026-10-05): no UI caller yet (tests only); meant for worker diagnostics.
    @router.get("/api/metrics/system")
    def api_metrics_system(minutes: int = Query(60, ge=1, le=180)) -> JSONResponse:
        """Per-minute rollups: event-loop lag, request latency by route, disk/CPU/memory
        pressure (see app_metrics). In-memory ring, newest last; also on disk in
        logs/metrics.ndjson."""
        return JSONResponse(app_metrics.snapshot(minutes))

    @router.get("/api/metrics/health")
    def api_metrics_health() -> JSONResponse:
        """Tiny 'what is happening' verdict (ok/warn/bad + cause) for the UI banner."""
        return JSONResponse(app_metrics.current_verdict().model_dump())

    @router.get("/api/system-alerts")
    def api_system_alerts(
        limit: int = Query(20, ge=1, le=100),
    ) -> JSONResponse:
        """Recent gateway-liveness outages recorded by the external
        ``aiteam-healthcheck.sh`` probe (source of truth: ``system_alerts``
        table). Read-only — the gateway never writes this table, only the
        out-of-process healthcheck script does, so an outage is recorded
        even when the gateway itself is unresponsive."""
        db = core._db()
        rows = db.list_recent_system_alerts(limit=limit) if db is not None else []
        return JSONResponse({"ok": True, "alerts": rows})

    @router.get("/api/quota-windows")
    async def api_quota_windows() -> JSONResponse:
        coordinator = getattr(orchestrator, "quota_coordinator", None)
        if coordinator is None:
            return JSONResponse({
                "enabled": False,
                "mode": "observe_only",
                "adapters": [],
                "buckets": [],
                "latest_snapshots": [],
                "window_states": [],
            })
        try:
            status = coordinator.read_status()
            # The prewarmer's own view (is a window running, when does it end,
            # what did the last activation do) rides on the same read — one
            # place to answer "what is the window rhythm right now?".
            prewarmer = getattr(orchestrator, "quota_prewarmer", None)
            status["prewarm"] = (
                prewarmer.read_status() if prewarmer is not None else {"enabled": False}
            )
            return JSONResponse(status)
        except Exception:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "quota_store_unavailable"})

    # --- projects / models / upload (Telegram parity) ---

    return router
