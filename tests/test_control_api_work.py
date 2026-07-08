"""
A27 — read-only Work/Case API route tests (no network, no paid backend).

Mirrors test_control_api_flows.py: build the app from a stub orchestrator,
inject an isolated on-disk MeshDB, drive with TestClient. Proves list/detail/
timeline/graph shapes, honest 404s, bucket filtering, and that the Work surface
is READ-ONLY (no mutation routes).
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


# --- auth + read-only ------------------------------------------------------

def test_work_requires_token(client):
    assert client.get("/api/work").status_code in (401, 403)
    assert client.get("/api/work/x").status_code in (401, 403)
    assert client.get("/api/work/x/timeline").status_code in (401, 403)
    assert client.get("/api/work/x/graph").status_code in (401, 403)


def test_work_is_read_only(client):
    # No mutation verbs on the Work surface.
    assert client.post("/api/work", headers=_auth()).status_code == 405
    assert client.delete("/api/work/x", headers=_auth()).status_code == 405


# --- list ------------------------------------------------------------------

def test_work_list_with_buckets(client, db):
    a = db.create_flow_run("t-a", "execution")
    b = db.create_flow_run("t-b", "intent")
    db.update_flow_run(b, status="blocked")

    r = client.get("/api/work", headers=_auth())
    assert r.status_code == 200
    model = r.json()
    ids = {c["flow_run_id"] for c in model["cases"]}
    assert {a, b} <= ids
    assert model["bucket_counts"]["blocked"] >= 1
    a_case = next(c for c in model["cases"] if c["flow_run_id"] == a)
    assert a_case["bucket"] == "active"


def test_work_list_bucket_filter(client, db):
    a = db.create_flow_run("t-a", "execution")            # active
    b = db.create_flow_run("t-b", "intent")
    db.update_flow_run(b, status="blocked")               # blocked

    r = client.get("/api/work?bucket=blocked", headers=_auth())
    assert r.status_code == 200
    ids = [c["flow_run_id"] for c in r.json()["cases"]]
    assert ids == [b]


def test_work_list_limit_validation(client):
    assert client.get("/api/work?limit=0", headers=_auth()).status_code == 422
    assert client.get("/api/work?limit=99999", headers=_auth()).status_code == 422


# --- detail ----------------------------------------------------------------

def test_work_detail_ledger_and_lineage(client, db):
    parent = db.create_flow_run("t-parent", "execution")
    child = db.create_flow_run("t-child", "intent", parent_flow_run_id=parent)
    db.create_flow_link(child, "task", "t-child", "root_task")
    db.create_flow_link(parent, "flow", child, "child_flow")
    db.append_flow_event(child, "flow.created", "system")

    r = client.get(f"/api/work/{child}", headers=_auth())
    assert r.status_code == 200
    model = r.json()
    assert model["case"]["flow_run_id"] == child
    assert model["parent"]["flow_run_id"] == parent
    assert [l["entity_id"] for l in model["ledger"]["tasks"]] == ["t-child"]
    assert model["counts"]["events"] == 1
    assert model["coverage"]["has_links"] is True and model["coverage"]["is_root"] is False

    # Parent detail shows the child in its ledger + children lineage.
    pr = client.get(f"/api/work/{parent}", headers=_auth()).json()
    assert [l["entity_id"] for l in pr["ledger"]["flows"]] == [child]
    assert [c["flow_run_id"] for c in pr["children"]] == [child]


def test_work_detail_unknown_is_404(client):
    r = client.get("/api/work/nope", headers=_auth())
    assert r.status_code == 404 and r.json()["detail"] == "case_not_found"


# --- timeline --------------------------------------------------------------

def test_work_timeline_orders_events(client, db):
    fid = db.create_flow_run("t-a", "intent")
    db.append_flow_event(fid, "flow.created", "system")
    db.append_flow_event(fid, "flow.stage_changed", "system", to_state="execution")

    r = client.get(f"/api/work/{fid}/timeline", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert [e["event_type"] for e in body["events"]] == ["flow.created", "flow.stage_changed"]
    assert body["event_count"] == 2


def test_work_timeline_unknown_is_404(client):
    assert client.get("/api/work/nope/timeline", headers=_auth()).status_code == 404


# --- graph -----------------------------------------------------------------

def test_work_graph_lineage(client, db):
    parent = db.create_flow_run("t-parent", "execution")
    child = db.create_flow_run("t-child", "intent", parent_flow_run_id=parent)

    r = client.get(f"/api/work/{child}/graph", headers=_auth())
    assert r.status_code == 200
    g = r.json()
    rels = {n["flow_run_id"]: n["rel"] for n in g["nodes"]}
    assert rels[child] == "self" and rels[parent] == "parent"
    assert {"from": parent, "to": child, "role": "child_flow"} in g["edges"]


def test_work_graph_unknown_is_404(client):
    assert client.get("/api/work/nope/graph", headers=_auth()).status_code == 404
