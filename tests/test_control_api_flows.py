"""A23 — read-only flow-run API tests (no network, no paid backend).

The Control API's ``/api/flows`` + ``/api/flows/{id}`` routes are thin read-only
projections over ``db.list_flow_runs`` / ``db.get_flow_run`` (A21 §11 schema).
These mirror ``test_control_api.py``'s injected-MeshDB pattern: build the app from
a stub orchestrator, monkeypatch ``control_api._db`` to an isolated on-disk DB,
and drive it with the FastAPI TestClient. No live gateway is touched.

Proves: list returns rows (summary shape); detail returns the full §11 record;
unknown id ⇒ 404; NULL §11 columns serialize as JSON null (never fabricated).
"""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.control.db import MeshDB
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


TOKEN = "test-control-token"


class _StubOrchestrator:
    def __init__(self) -> None:
        self.session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)


@pytest.fixture
def db(tmp_path):
    return MeshDB(str(tmp_path / "mesh.db"))


@pytest.fixture
def client(monkeypatch, db):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    monkeypatch.setattr(control_api, "_db", lambda: db)
    return TestClient(control_api.build_control_api(_StubOrchestrator()))


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- auth (parity with the rest of /api/*) ---------------------------------

def test_flows_requires_token(client):
    assert client.get("/api/flows").status_code in (401, 403)
    assert client.get("/api/flows/anything").status_code in (401, 403)


# --- list ------------------------------------------------------------------

def test_flows_list_returns_rows(client, db):
    fid_a = db.create_flow_run(task_id="task-a", current_stage="dispatched")
    fid_b = db.create_flow_run(task_id="task-b", current_stage="executing")

    r = client.get("/api/flows", headers=_auth())
    assert r.status_code == 200
    flows = r.json()["flows"]
    ids = {f["flow_run_id"] for f in flows}
    assert {fid_a, fid_b} <= ids

    # Summary shape only — exactly the six packet §11 columns, nothing more.
    row = next(f for f in flows if f["flow_run_id"] == fid_a)
    assert set(row) == {
        "flow_run_id", "task_id", "current_stage", "status", "created_at", "updated_at",
    }
    assert row["task_id"] == "task-a"
    assert row["current_stage"] == "dispatched"


def test_flows_list_filters_by_task_id(client, db):
    db.create_flow_run(task_id="task-a", current_stage="dispatched")
    fid_b = db.create_flow_run(task_id="task-b", current_stage="executing")

    r = client.get("/api/flows?task_id=task-b", headers=_auth())
    assert r.status_code == 200
    flows = r.json()["flows"]
    assert [f["flow_run_id"] for f in flows] == [fid_b]


def test_flows_list_empty_when_no_rows(client):
    r = client.get("/api/flows", headers=_auth())
    assert r.status_code == 200 and r.json()["flows"] == []


def test_flows_list_limit_validation(client):
    assert client.get("/api/flows?limit=0", headers=_auth()).status_code == 422
    assert client.get("/api/flows?limit=99999", headers=_auth()).status_code == 422


# --- detail ----------------------------------------------------------------

def test_flow_detail_returns_full_record(client, db):
    fid = db.create_flow_run(
        task_id="task-a",
        current_stage="executing",
        objective_lock="ship it",
        approved_plan="do the thing",
    )
    db.update_flow_run(fid, status="running")

    r = client.get(f"/api/flows/{fid}", headers=_auth())
    assert r.status_code == 200
    flow = r.json()["flow"]

    # Full §11 record — every ADD-COLUMN migration column is present as a key.
    for col in (
        "flow_run_id", "task_id", "current_stage", "objective_lock", "created_at",
        "approved_plan", "plan_review", "burn_down_items", "execution_result",
        "implementation_review", "waived_findings", "closure_summary",
        "role_assignments", "artifact_links", "status", "updated_at",
        "parent_flow_run_id", "dispatched_by", "dispatch_file",
    ):
        assert col in flow, f"missing column {col!r} in detail record"

    assert flow["flow_run_id"] == fid
    assert flow["objective_lock"] == "ship it"
    assert flow["approved_plan"] == "do the thing"
    assert flow["status"] == "running"
    assert flow["updated_at"] is not None  # stamped by update_flow_run


def test_flow_detail_unknown_id_is_404(client):
    r = client.get("/api/flows/does-not-exist", headers=_auth())
    assert r.status_code == 404
    assert r.json()["detail"] == "flow_not_found"


# --- honest nulls ----------------------------------------------------------

def test_null_fields_serialize_as_null(client, db):
    # A freshly created flow leaves the §11 columns (and updated_at) NULL — the
    # API must echo them as JSON null, never a fabricated default.
    fid = db.create_flow_run(task_id="task-a", current_stage="dispatched")

    detail = client.get(f"/api/flows/{fid}", headers=_auth()).json()["flow"]
    for col in (
        "status", "updated_at", "approved_plan", "plan_review", "execution_result",
        "closure_summary", "role_assignments", "artifact_links",
        "parent_flow_run_id", "dispatched_by", "dispatch_file",
    ):
        assert detail[col] is None, f"{col} should be null, got {detail[col]!r}"

    # The summary projection carries the same honest nulls.
    summary = client.get("/api/flows", headers=_auth()).json()["flows"]
    row = next(f for f in summary if f["flow_run_id"] == fid)
    assert row["status"] is None
    assert row["updated_at"] is None
