"""A65 P3 cost alerts: fire on a billable-USD threshold, stay quiet below it,
enforcement stays OFF by default and only surfaces the existing governor seam.
Pure SQL/math over the isolated MeshDB (conftest) — no paid backend, so it is
cheap and safe under the TEST COST GUARD."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.control.db import get_db, runtime_flag_enabled
from src.services.cost_alerts import check_cost_alerts

from test_cost_read_model import _auth, _seed, _StubOrchestrator, TOKEN

NOW = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded_db():
    db = get_db()
    assert db is not None
    _seed(db)
    return db


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(_StubOrchestrator()))


def _knobs(monkeypatch, daily=None, session=None, case=None):
    for name, val in (
        ("COST_ALERT_DAILY_BUDGET_USD", daily),
        ("COST_ALERT_SESSION_BURN_USD", session),
        ("COST_ALERT_CASE_TOTAL_USD", case),
    ):
        if val is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, str(val))


def _rules(alerts):
    return {a["rule"] for a in alerts}


# --- pure check ---------------------------------------------------------------

def test_daily_budget_fires_when_crossed(seeded_db, monkeypatch):
    _knobs(monkeypatch, daily=1)
    out = check_cost_alerts(seeded_db, now=NOW)
    assert out["enabled"] is True
    assert "daily_budget" in _rules(out["alerts"])
    daily = next(a for a in out["alerts"] if a["rule"] == "daily_budget")
    assert daily["scope"] == "today (UTC)"
    assert daily["value_usd"] > daily["budget_usd"]
    assert daily["pct"] > 100


def test_daily_budget_silent_below_threshold(seeded_db, monkeypatch):
    _knobs(monkeypatch, daily=1_000_000)
    out = check_cost_alerts(seeded_db, now=NOW)
    assert out["enabled"] is True
    assert "daily_budget" not in _rules(out["alerts"])


def test_session_burn_fires_for_top_spender(seeded_db, monkeypatch):
    _knobs(monkeypatch, session=1)
    out = check_cost_alerts(seeded_db, now=NOW)
    alerts = [a for a in out["alerts"] if a["rule"] == "session_burn"]
    assert len(alerts) == 1
    assert alerts[0]["scope"] == "wk-1"  # the opus worker is the top spender
    assert alerts[0]["pct"] > 100


def test_case_total_fires_for_the_case(seeded_db, monkeypatch):
    _knobs(monkeypatch, case=1)
    out = check_cost_alerts(seeded_db, now=NOW)
    alerts = [a for a in out["alerts"] if a["rule"] == "case_total"]
    assert len(alerts) == 1
    assert alerts[0]["scope"] == "case-0000000000000001"
    assert alerts[0]["value_usd"] == pytest.approx(10.51, abs=0.01)


def test_no_knobs_means_disabled_and_quiet(seeded_db, monkeypatch):
    _knobs(monkeypatch)
    out = check_cost_alerts(seeded_db, now=NOW)
    assert out["enabled"] is False
    assert out["alerts"] == []


def test_malformed_knob_treated_as_off(seeded_db, monkeypatch):
    monkeypatch.setenv("COST_ALERT_DAILY_BUDGET_USD", "not-a-number")
    out = check_cost_alerts(seeded_db, now=NOW)
    assert out["budgets"]["daily_budget_usd"] == 0.0
    assert out["enabled"] is False


def test_enforcement_flag_is_off_by_default():
    assert runtime_flag_enabled("COST_ALERT_ENFORCE_ENABLED") is False


# --- Control API surface ------------------------------------------------------

def test_alerts_endpoint_requires_auth(client):
    assert client.get("/api/cost/alerts").status_code in (401, 403)


def test_alerts_endpoint_fires_and_surfaces_enforcement(client, seeded_db, monkeypatch):
    _knobs(monkeypatch, daily=1)
    r = client.get("/api/cost/alerts", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["enabled"] is True
    assert "daily_budget" in _rules(body["alerts"])
    # Enforcement is OFF by default and only surfaces the existing governor lever.
    assert body["enforcement"]["enabled"] is False
    assert body["enforcement"]["mechanism"] == "sdk_max_budget_usd"


def test_alerts_endpoint_enforcement_flag_turns_on(client, seeded_db, monkeypatch):
    _knobs(monkeypatch, daily=1)
    monkeypatch.setenv("COST_ALERT_ENFORCE_ENABLED", "1")
    body = client.get("/api/cost/alerts", headers=_auth()).json()
    assert body["enforcement"]["enabled"] is True


def test_alerts_endpoint_quiet_when_within_budget(client, seeded_db, monkeypatch):
    _knobs(monkeypatch, daily=1_000_000)
    body = client.get("/api/cost/alerts", headers=_auth()).json()
    assert body["alerts"] == []
    assert body["enabled"] is True
