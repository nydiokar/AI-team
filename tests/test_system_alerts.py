"""system_alerts (2026-08-21 incident follow-up) — DB layer + control API.

The table is written by ~/scripts/aiteam-healthcheck.sh, a process OUTSIDE the
gateway (it must be able to record an outage even when the gateway itself is
unresponsive), so these tests exercise it the same way: raw sqlite3 inserts,
never MeshDB write helpers (the gateway has none — it's read-only here).
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.control.db import MeshDB


TOKEN = "test-alerts-token"


@pytest.fixture
def db(tmp_path):
    d = MeshDB(str(tmp_path / "alerts.db"))
    yield d
    d.close()


def _insert(db_path, kind, message, detail, opened_at, resolved_at=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO system_alerts (source, kind, message, detail, opened_at, resolved_at)"
        " VALUES ('healthcheck', ?, ?, ?, ?, ?)",
        (kind, message, detail, opened_at, resolved_at),
    )
    conn.commit()
    conn.close()


def test_migration_creates_table(db):
    # The migration must have run — an external writer's own defensive
    # CREATE TABLE IF NOT EXISTS is a safety net, not the primary path.
    row = db._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='system_alerts'"
    ).fetchone()
    assert row is not None


def test_list_recent_system_alerts_orders_newest_first(db):
    path = str(db._path)
    _insert(path, "unresponsive", "gateway /health http=000", "stack...", "2026-08-21T12:50:00Z")
    _insert(path, "process_down", "pm2 ai-team-gateway status=stopped", "traceback...", "2026-08-21T13:00:00Z")

    alerts = db.list_recent_system_alerts(limit=20)
    assert [a["kind"] for a in alerts] == ["process_down", "unresponsive"]
    assert alerts[0]["resolved_at"] is None
    assert alerts[1]["detail"] == "stack..."


def test_list_recent_system_alerts_empty_ok(db):
    assert db.list_recent_system_alerts() == []


def test_list_recent_system_alerts_respects_limit(db):
    path = str(db._path)
    for i in range(5):
        _insert(path, "degraded", f"m{i}", "", f"2026-08-21T12:0{i}:00Z")
    assert len(db.list_recent_system_alerts(limit=2)) == 2


# ---------------------------------------------------------------------------
# Control API
# ---------------------------------------------------------------------------

class _StubOrchestrator:
    pass


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    test_db = MeshDB(str(tmp_path / "api_alerts.db"))
    monkeypatch.setattr(control_api, "_db", lambda: test_db)
    c = TestClient(control_api.build_control_api(_StubOrchestrator()))
    c._test_db = test_db  # type: ignore[attr-defined]
    yield c
    test_db.close()


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_system_alerts_requires_auth(client):
    r = client.get("/api/system-alerts")
    assert r.status_code in (401, 403)


def test_system_alerts_empty(client):
    r = client.get("/api/system-alerts", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "alerts": []}


def test_system_alerts_returns_rows(client):
    _insert(
        str(client._test_db._path), "unresponsive",
        "gateway /health http=000", "main-thread stack: prune_snapshots(...)",
        "2026-08-21T12:50:00Z",
    )
    r = client.get("/api/system-alerts", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["kind"] == "unresponsive"
    assert body["alerts"][0]["resolved_at"] is None
