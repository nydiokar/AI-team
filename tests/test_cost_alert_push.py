"""A65 P3 F1: budget alerts reach the operator through the existing Web Push seam.

The push rides ``NotificationService.notify_task_outcome`` (the terminal-outcome
seam both completion paths share). It is detached, throttled, deduped per
(rule, scope, UTC day), runs the SQL check off the event loop, and never raises
into or delays task completion. PushService transport is stubbed — no network,
no paid backend (TEST COST GUARD).
"""
import asyncio
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from src.services import cost_alerts
from src.services import notification_service as ns
from src.services.push_service import PushService

from test_cost_read_model import _seed

DAY = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)
ALERT = {"rule": "daily_budget", "scope": "today (UTC)", "value_usd": 12.5,
         "budget_usd": 10.0, "pct": 125.0}


class _Orch:
    telegram_interface = None


def _result():
    return types.SimpleNamespace(success=True, files_modified=[])


@pytest.fixture
def pushes(monkeypatch):
    """Push 'available', fan-out captured; the outcome push itself is muted so
    only budget-alert payloads are observed."""
    sent: list = []

    async def _fanout(self, payload):
        sent.append(payload)

    monkeypatch.setattr(PushService, "available", lambda self: (True, None))
    monkeypatch.setattr(PushService, "fanout", _fanout)
    monkeypatch.setattr(ns.NotificationService, "_maybe_push_outcome", lambda self, *a, **k: None)
    monkeypatch.setattr(ns, "_utc_now", lambda: DAY, raising=False)
    return sent


@pytest.fixture
def calls(monkeypatch):
    """Stub the read model: count calls, return one crossed alert."""
    seen: list = []

    def _check(db, *, now=None):
        seen.append(now)
        return {"ok": True, "enabled": True, "budgets": {}, "alerts": [dict(ALERT)]}

    monkeypatch.setattr(cost_alerts, "check_cost_alerts", _check)
    return seen


def _knob(monkeypatch, value="10"):
    for name in ("COST_ALERT_DAILY_BUDGET_USD", "COST_ALERT_SESSION_BURN_USD",
                 "COST_ALERT_CASE_TOTAL_USD"):
        monkeypatch.delenv(name, raising=False)
    if value is not None:
        monkeypatch.setenv("COST_ALERT_DAILY_BUDGET_USD", value)


async def _drain():
    me = asyncio.current_task()
    while True:
        pending = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _outcomes(svc, n, *, between=None):
    async def _go():
        for i in range(n):
            await svc.notify_task_outcome(f"t{i}", _result())
            await _drain()
            if between:
                between(i)
    asyncio.run(_go())


def _svc(interval=0.0):
    svc = ns.NotificationService(orchestrator=_Orch())
    svc._COST_ALERT_CHECK_INTERVAL_SEC = interval
    return svc


def test_crossed_budget_pushes_once_per_rule_scope_day(monkeypatch, pushes, calls):
    _knob(monkeypatch)
    svc = _svc()
    _outcomes(svc, 5)
    assert len(calls) == 5                      # checked every time (no throttle)
    assert len(pushes) == 1                     # but pushed exactly once
    p = pushes[0]
    assert p["title"] == "💸 Budget alert"
    assert "Daily spend" in p["body"] and "$12.50" in p["body"] and "$10.00" in p["body"]
    assert p["url"] == "/cost"


def test_distinct_scopes_each_push_once(monkeypatch, pushes):
    _knob(monkeypatch)
    alerts = [dict(ALERT, rule="case_total", scope="case-aaaaaaaa"),
              dict(ALERT, rule="case_total", scope="case-bbbbbbbb")]
    monkeypatch.setattr(cost_alerts, "check_cost_alerts",
                        lambda db, *, now=None: {"alerts": alerts})
    _outcomes(_svc(), 3)
    assert len(pushes) == 2


def test_utc_day_rollover_resets_dedupe(monkeypatch, pushes, calls):
    _knob(monkeypatch)
    svc = _svc()
    days = iter([DAY, DAY, DAY + timedelta(days=1), DAY + timedelta(days=1)])
    monkeypatch.setattr(ns, "_utc_now", lambda: next(days), raising=False)
    _outcomes(svc, 4)
    assert len(pushes) == 2
    assert calls[2].date() == (DAY + timedelta(days=1)).date()


@pytest.mark.parametrize("value", [None, "", "0", "-5", "garbage"])
def test_unset_or_invalid_knob_means_no_check_no_push(monkeypatch, pushes, calls, value):
    _knob(monkeypatch, value)
    _outcomes(_svc(), 3)
    assert calls == [] and pushes == []


def test_push_unavailable_means_no_check(monkeypatch, pushes, calls):
    _knob(monkeypatch)
    monkeypatch.setattr(PushService, "available", lambda self: (False, "vapid_not_configured"))
    _outcomes(_svc(), 2)
    assert calls == [] and pushes == []


def test_failing_check_or_fanout_never_raises_into_outcome(monkeypatch, pushes):
    _knob(monkeypatch)

    def _boom_check(db, *, now=None):
        raise RuntimeError("read model down")

    async def _boom_fanout(self, payload):
        raise RuntimeError("push relay down")

    monkeypatch.setattr(cost_alerts, "check_cost_alerts", _boom_check)
    svc = _svc()
    _outcomes(svc, 2)                            # must not raise
    assert svc._cost_alert_inflight is False     # guard released after failure

    monkeypatch.setattr(cost_alerts, "check_cost_alerts",
                        lambda db, *, now=None: {"alerts": [dict(ALERT)]})
    monkeypatch.setattr(PushService, "fanout", _boom_fanout)
    _outcomes(svc, 2)                            # must not raise either


def test_available_raising_never_raises_into_outcome(monkeypatch, pushes, calls):
    _knob(monkeypatch)

    def _boom(self):
        raise RuntimeError("config broken")

    monkeypatch.setattr(PushService, "available", _boom)
    _outcomes(_svc(), 1)
    assert calls == []


def test_check_runs_off_the_event_loop(monkeypatch, pushes):
    """The SQL check runs in a worker thread, and a slow read model does not
    delay notify_task_outcome (the A84 consumer awaits it under a 4 s wait_for)."""
    _knob(monkeypatch)
    threads: list = []

    def _slow_check(db, *, now=None):
        threads.append(threading.get_ident())
        time.sleep(0.5)
        return {"alerts": [dict(ALERT)]}

    monkeypatch.setattr(cost_alerts, "check_cost_alerts", _slow_check)
    to_thread_funcs: list = []
    real_to_thread = asyncio.to_thread

    async def _spy(func, *a, **k):
        to_thread_funcs.append(func)
        return await real_to_thread(func, *a, **k)

    monkeypatch.setattr(asyncio, "to_thread", _spy)
    svc = _svc()

    async def _go():
        loop_thread = threading.get_ident()
        t0 = time.monotonic()
        await asyncio.wait_for(svc.notify_task_outcome("t1", _result()), timeout=4)
        elapsed = time.monotonic() - t0
        await _drain()
        return loop_thread, elapsed

    loop_thread, elapsed = asyncio.run(_go())
    assert elapsed < 0.2                          # completion not delayed by the check
    assert to_thread_funcs and to_thread_funcs[0] is _slow_check
    assert threads and threads[0] != loop_thread
    assert len(pushes) == 1


def test_throttle_bounds_read_model_calls(monkeypatch, pushes, calls):
    _knob(monkeypatch)
    svc = _svc(interval=60.0)
    _outcomes(svc, 10)                            # a completion burst
    assert len(calls) == 1
    assert len(pushes) == 1


def test_inflight_check_is_not_doubled(monkeypatch, pushes):
    """A burst arriving while the (slow) check is still running starts no second one."""
    _knob(monkeypatch)
    gate = threading.Event()
    n: list = []

    def _blocking(db, *, now=None):
        n.append(1)
        gate.wait(2)
        return {"alerts": []}

    monkeypatch.setattr(cost_alerts, "check_cost_alerts", _blocking)
    svc = _svc()

    async def _go():
        for i in range(5):
            await svc.notify_task_outcome(f"t{i}", _result())
            await asyncio.sleep(0.01)
        gate.set()
        await _drain()

    asyncio.run(_go())
    assert len(n) == 1


def test_real_read_model_crossing_pushes_once(monkeypatch, pushes):
    """Cross-layer (offline): env knob → real check_cost_alerts over the seeded
    isolated MeshDB → notify_task_outcome → PushService.fanout payload."""
    from src.control.db import get_db

    _seed(get_db())
    _knob(monkeypatch, "1")
    _outcomes(_svc(), 3)
    assert [p["title"] for p in pushes] == ["💸 Budget alert"]
    assert "Daily spend" in pushes[0]["body"]
