"""Cost, metrics and quota routes.

Mounted by ``control_api.build_control_api``; route map: docs/backend/ARCHITECTURE.md "Cost,
metrics, quota". Module-level helpers are looked up as ``core.<name>`` so tests that
monkeypatch ``control_api`` keep reaching these handlers.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING, Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from src.control import app_metrics
from src.control import control_api as core
from src.control.routes._responses import TokenTotals, UsdSummary

if TYPE_CHECKING:
    from src.control.db import MeshDB


# --- OpenAPI success-response models (documentation only; handlers return JSONResponse) ---


class ModelSpend(BaseModel):
    model: str
    usd_total: float
    known: bool
    reason: str
    tokens_total: int


class Totals(BaseModel):
    tokens: TokenTotals
    usd: UsdSummary


class SeriesBucket(BaseModel):
    bucket: str
    dim: str
    tokens: TokenTotals
    usd: UsdSummary
    models: list[ModelSpend]


class Unattributed(BaseModel):
    tokens: TokenTotals
    usd: UsdSummary
    models: list[ModelSpend]


class CostExplorerOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ok: bool
    dimension: str
    granularity: str
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    repo_path: Optional[str] = None
    series: list[SeriesBucket]
    totals: Totals
    unattributed: Unattributed


class CaseMeta(BaseModel):
    status: str
    objective_lock: Optional[str] = None
    created_at: Optional[str] = None


class SessionSpend(BaseModel):
    session_id: str
    role: str
    tokens: TokenTotals
    usd: UsdSummary
    models: list[ModelSpend]


class MgrVsWorkers(BaseModel):
    manager: Totals
    workers: Totals
    workers_share_pct: Optional[float] = None
    worker_sessions: int


class CaseUsageOut(BaseModel):
    ok: bool
    flow_run_id: str
    case: CaseMeta
    sessions: list[SessionSpend]
    mgr_vs_workers: MgrVsWorkers
    totals: Totals


class TopRow(BaseModel):
    session_id: str
    repo_path: Optional[str] = None
    backend: Optional[str] = None
    role: Optional[str] = None
    tokens: TokenTotals
    usd: UsdSummary
    models: list[ModelSpend]


class CostTopOut(BaseModel):
    ok: bool
    by: str
    limit: int
    rows: list[TopRow]
    totals: Totals


class ProjectRow(BaseModel):
    repo_path: str
    tokens: TokenTotals


class CostProjectsOut(BaseModel):
    ok: bool
    limit: int
    projects: list[ProjectRow]


class AlertBudgets(BaseModel):
    daily_budget_usd: float
    session_burn_usd: float
    case_total_usd: float


class AlertRow(BaseModel):
    rule: str
    scope: str
    value_usd: float
    budget_usd: float
    pct: float


class AlertEnforcement(BaseModel):
    enabled: bool
    mechanism: str
    governor_sdk_max_budget_usd: Optional[float] = None


class CostAlertsOut(BaseModel):
    ok: bool
    enabled: bool
    budgets: AlertBudgets
    alerts: list[AlertRow]
    enforcement: AlertEnforcement


class RouteStat(BaseModel):
    component: str
    method: str
    route: str
    n: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    slow: int
    err5xx: int


class Rollup(BaseModel):
    ts: str
    window_s: float
    lag_p95_ms: float
    lag_max_ms: float
    lag_over_100ms: int
    req_total: int
    req_slow: int
    req_5xx: int
    host_avg: dict[str, float]
    host_max: dict[str, float]
    routes: list[RouteStat]


class MetricsSystemOut(BaseModel):
    enabled: bool
    rollup_seconds: int
    count: int
    rollups: list[Rollup]


class MetricsHealthOut(BaseModel):
    status: str
    cause: str
    headline: str
    detail: str


class SystemAlertsOut(BaseModel):
    ok: bool
    alerts: list[dict]


_QUOTA_READ_SLOTS = threading.BoundedSemaphore(4)


class QuotaWindowsOut(BaseModel):
    enabled: bool
    mode: str
    adapters: list[dict]
    buckets: list[dict]
    latest_snapshots: list[dict]
    window_states: list[dict]
    prewarm: dict
    observed_usage: list[dict] = Field(default_factory=list)


def build_router(orchestrator: Any, *, require_auth: Callable[..., Any]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_auth)])

    def _cost_db() -> MeshDB:
        db = core._db()
        if db is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "db_unavailable"})
        return db

    @router.get(
        "/api/cost/explorer",
        responses={200: {"model": CostExplorerOut, "description": "Spend breakdown along one dimension, bucketed and priced"}},
    )
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

    @router.get(
        "/api/cases/{case_id}/usage",
        responses={200: {"model": CaseUsageOut, "description": "Per-case cost breakdown with manager/workers split"}},
    )
    def api_case_usage(case_id: str) -> JSONResponse:
        """Per-case cost breakdown (the manager/workers split): each session's
        tokens + USD, and the mgr_vs_workers summary. 404 for an unknown case."""
        from src.control.cost_read_model import assemble_case_usage
        result = assemble_case_usage(_cost_db(), case_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"ok": False, "reason": "case_not_found"})
        return JSONResponse(result)

    @router.get(
        "/api/cost/top",
        responses={200: {"model": CostTopOut, "description": "Top spender sessions ranked by USD or tokens"}},
    )
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

    @router.get(
        "/api/cost/projects",
        responses={200: {"model": CostProjectsOut, "description": "Distinct projects with token usage, ordered by size"}},
    )
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

    @router.get(
        "/api/cost/alerts",
        responses={200: {"model": CostAlertsOut, "description": "Cost budget/burn-rate alerts and enforcement lever"}},
    )
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
            from src.control.db import sdk_governor_ceilings
            governor_budget = sdk_governor_ceilings()["sdk_max_budget_usd"]
        except Exception:
            pass
        result = check_cost_alerts(_cost_db())
        result["enforcement"] = {
            "enabled": runtime_flag_enabled("COST_ALERT_ENFORCE_ENABLED"),
            "mechanism": "sdk_max_budget_usd",
            "governor_sdk_max_budget_usd": governor_budget,
        }
        return JSONResponse(result)

    # REVISIT (2026-10-05): no UI caller yet (tests only); the gateway's own per-minute
    # host/app metrics ring (app_metrics) - wire it into the System tab or drop it.
    @router.get(
        "/api/metrics/system",
        responses={200: {"model": MetricsSystemOut, "description": "Per-minute host/request metric rollups"}},
    )
    def api_metrics_system(minutes: int = Query(60, ge=1, le=180)) -> JSONResponse:
        """Per-minute rollups: event-loop lag, request latency by route, disk/CPU/memory
        pressure (see app_metrics). In-memory ring, newest last; also on disk in
        logs/metrics.ndjson."""
        return JSONResponse(app_metrics.snapshot(minutes))

    @router.get(
        "/api/metrics/health",
        responses={200: {"model": MetricsHealthOut, "description": "One-line gateway health verdict for the UI banner"}},
    )
    def api_metrics_health() -> JSONResponse:
        """Tiny 'what is happening' verdict (ok/warn/bad + cause) for the UI banner."""
        return JSONResponse(app_metrics.current_verdict().model_dump())

    @router.get(
        "/api/system-alerts",
        responses={200: {"model": SystemAlertsOut, "description": "Recent gateway-liveness outages from the external healthcheck"}},
    )
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

    @router.get(
        "/api/quota-windows",
        responses={200: {"model": QuotaWindowsOut, "description": "Quota window coordinator + prewarmer status"}},
    )
    def api_quota_windows() -> JSONResponse:
        if not _QUOTA_READ_SLOTS.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="quota_read_busy")
        try:
            return read_quota_windows()
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="quota_store_unavailable")
        finally:
            _QUOTA_READ_SLOTS.release()

    def read_quota_windows() -> JSONResponse:
        from src.control.home_status import codex_usage
        db = core._db()
        observed = [w.model_dump() for w in codex_usage(db)] if db is not None else []
        coordinator = getattr(orchestrator, "quota_coordinator", None)
        if coordinator is None:
            return JSONResponse({
                "enabled": False,
                "observed_usage": observed,
                "mode": "observe_only",
                "adapters": [],
                "buckets": [],
                "latest_snapshots": [],
                "window_states": [],
            })
        try:
            status = coordinator.read_status()
            status["observed_usage"] = observed
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
