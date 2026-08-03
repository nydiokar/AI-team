"""A65 Phase 3 — cost budget / burn-rate alerts (billable-USD only).

Pure computation over the PROVEN cost read-model (``cost_read_model``) — no new
SQL, no new state. Alerts fire ONLY on the honest known-USD figure
(``usd.known``, which prices cache reads at their discounted rate). The P0 audit
proved raw token/cache-read volume fires constantly and means nothing, so it is
never the alert basis.

Thresholds are env-driven numerics — the A62 non-boolean `/api/flags` registry
is their future home (see the R2 seam note in the A65 packet); today they are
documented env knobs:

- ``COST_ALERT_DAILY_BUDGET_USD`` — whole-day known USD (attributed + the
  honest unattributed bucket) over the current UTC day.
- ``COST_ALERT_SESSION_BURN_USD`` — a single session's known USD over the day.
- ``COST_ALERT_CASE_TOTAL_USD`` — a single case's known USD over the day.

Each knob defaults to 0 (off): an unset knob fires nothing. Alerts are active
the moment a knob is set — no master flag (read-only computation). ENFORCEMENT
is a separate, flag-gated switch that lives in the endpoint layer and only
SURFACES the existing SDK governor ceiling; this module never kills anything.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.control.cost_read_model import assemble_explorer, assemble_top_sessions

_DAILY_ENV = "COST_ALERT_DAILY_BUDGET_USD"
_SESSION_ENV = "COST_ALERT_SESSION_BURN_USD"
_CASE_ENV = "COST_ALERT_CASE_TOTAL_USD"


def _env_float(name: str) -> float:
    raw = os.environ.get(name)
    if not raw:
        return 0.0
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return 0.0


def read_budget_knobs() -> Dict[str, float]:
    """The three budget knobs as read from the environment (0.0 = off)."""
    return {
        "daily_budget_usd": _env_float(_DAILY_ENV),
        "session_burn_usd": _env_float(_SESSION_ENV),
        "case_total_usd": _env_float(_CASE_ENV),
    }


def _alert(rule: str, scope: str, value: float, budget: float) -> Dict[str, Any]:
    pct = (value / budget * 100) if budget > 0 else 100.0
    return {
        "rule": rule,
        "scope": scope,
        "value_usd": round(value, 6),
        "budget_usd": round(budget, 6),
        "pct": round(pct, 1),
    }


def check_cost_alerts(
    db: Any, *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Return fired alerts against the configured budgets.

    ``now`` injectable for tests. Every number is the read-model's known USD;
    an unset knob (0.0) contributes no alert. `enabled` is true whenever any
    knob is set — the knob is the on-switch, matching the packet's "alerts on
    by default once knobs are set".
    """
    budgets = read_budget_knobs()
    alerts: List[Dict[str, Any]] = []
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    daily = budgets["daily_budget_usd"]
    if daily > 0:
        explorer = assemble_explorer(
            db, dimension="session", granularity="", from_ts=day_start, limit=5000
        )
        burn = float(explorer["totals"]["usd"]["known"] or 0.0)
        burn += float(explorer["unattributed"]["usd"]["known"] or 0.0)
        if burn > daily:
            alerts.append(_alert("daily_budget", "today (UTC)", burn, daily))

    session = budgets["session_burn_usd"]
    if session > 0:
        top = assemble_top_sessions(db, from_ts=day_start, by="usd", limit=1)
        rows = top.get("rows") or []
        if rows:
            burn = float(rows[0]["usd"]["known"] or 0.0)
            if burn > session:
                alerts.append(
                    _alert("session_burn", str(rows[0].get("session_id")), burn, session)
                )

    case = budgets["case_total_usd"]
    if case > 0:
        explorer = assemble_explorer(
            db, dimension="case", granularity="", from_ts=day_start, limit=5000
        )
        for s in explorer.get("series") or []:
            if s.get("dim") in (None, "", "standalone"):
                continue
            value = float(s["usd"]["known"] or 0.0)
            if value > case:
                alerts.append(_alert("case_total", str(s.get("dim")), value, case))

    return {
        "ok": True,
        "enabled": any(v > 0 for v in budgets.values()),
        "budgets": budgets,
        "alerts": alerts,
    }
