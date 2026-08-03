"""A56 / M4 — spec authoring → scored review gate → decomposer-as-task-DAG.

HERMETIC (no network, no paid CLI): the db layer is exercised on a real temp
MeshDB (like test_case_closure); the MCP tool surface is imported directly (like
test_mcp_manager); the Control API routes run over a real temp MeshDB + the real
orchestrator seams via the FastAPI TestClient (like test_review_emitter).

Proves the acceptance contract:
  1. a feature intent → a spec artifact linked to the Case + a review.* score;
     a below-threshold OR critical-zero score BLOCKS decomposition (asserted),
     an accepted score allows it.
  2. decomposition yields ONE Case with N task_attached links carrying dependency
     edges on metadata_json — NO orphan flow_runs, DAG edges queryable.
  3. publish_artifact → artifact link + event (durable evidence).
  4. flag OFF ⇒ byte-identical (routes 404, db writes nothing).
  5. a cyclic / malformed DAG is refused.

Run: `pytest tests/test_spec_authoring_decompose.py -q`
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.control import control_api
import src.control.db as db_mod
from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator
from src.services.session_store import SessionStore
from src.services.session_service import SessionService

os.environ["AI_TEAM_ENV_FILE"] = "/nonexistent/mcp_manager_test.env"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
mcp_manager = importlib.import_module("mcp_manager")

TOKEN = "test-control-token"

# A well-formed passing score-card (10/12, no critical zero).
GOOD_SCORES = {
    "objective_clarity": 2,
    "scope_boundaries": 2,
    "decomposability": 2,
    "acceptance_testability": 1,
    "dependency_correctness": 2,
    "risks_and_assumptions": 1,
}
# Below threshold (6/12) — total gate fails.
LOW_SCORES = {d: 1 for d in db_mod.SPEC_REVIEW_DIMENSIONS}
# Above threshold (10/12) but a hard zero on a CRITICAL dimension — critical gate fails.
CRITICAL_ZERO_SCORES = {
    "objective_clarity": 2,
    "scope_boundaries": 2,
    "decomposability": 0,  # critical zero
    "acceptance_testability": 2,
    "dependency_correctness": 2,
    "risks_and_assumptions": 2,
}


# ---------------------------------------------------------------------------
# db-layer helpers
# ---------------------------------------------------------------------------

def _enabled(monkeypatch):
    monkeypatch.setenv("SPEC_AUTHORING_ENABLED", "1")


@pytest.fixture
def db(tmp_path):
    return MeshDB(str(tmp_path / "mesh.db"))


def _count_flow_runs(db):
    return db._conn().execute("SELECT COUNT(*) AS n FROM flow_runs").fetchone()["n"]


# ---------------------------------------------------------------------------
# (0) MCP tool registration + input validation
# ---------------------------------------------------------------------------

def test_m4_tools_registered():
    names = {t["name"] for t in mcp_manager._TOOLS}
    for n in ("publish_spec", "publish_artifact", "record_spec_review", "decompose_case"):
        assert n in names
        assert n in mcp_manager._TOOL_IMPLS


def test_record_spec_review_rejects_missing_scores():
    out = mcp_manager._record_spec_review({"case_id": "c", "spec_id": "s", "scores": {}})
    assert "scores" in out.lower()


def test_decompose_rejects_empty_tasks():
    out = mcp_manager._decompose_case({"case_id": "c", "spec_id": "s", "tasks": []})
    assert "non-empty" in out.lower()


# ---------------------------------------------------------------------------
# (1) pure rubric scoring — the R1 gate is COMPUTED, not trusted
# ---------------------------------------------------------------------------

def test_score_passes_at_threshold_no_critical_zero():
    g = db_mod._score_spec_review(GOOD_SCORES)
    assert g["passed"] is True and g["verdict"] == "accepted" and g["total"] == 10


def test_score_blocked_below_threshold():
    g = db_mod._score_spec_review(LOW_SCORES)
    assert g["passed"] is False and g["verdict"] == "rework_requested" and g["total"] == 6


def test_score_blocked_by_critical_zero_even_above_threshold():
    g = db_mod._score_spec_review(CRITICAL_ZERO_SCORES)
    assert g["total"] == 10 and g["passed"] is False
    assert "decomposability" in g["critical_zero"]


def test_score_blocked_when_malformed():
    g = db_mod._score_spec_review({"objective_clarity": 5})  # missing + out of range
    assert g["passed"] is False
    assert g["missing"] and g["out_of_range"]


# ---------------------------------------------------------------------------
# (2) db layer — publish_spec, review, decompose: ONE Case + N task_attached links
# ---------------------------------------------------------------------------

def test_publish_spec_links_artifact_and_events(monkeypatch, db):
    _enabled(monkeypatch)
    cid = db.open_case("build feature X", "sess-1")
    r = db.publish_spec(cid, "spec-x", "real_objective: ...", title="Feature X")
    assert r["ok"] is True
    links = db.list_flow_links(flow_run_id=cid, entity_type="artifact")
    assert len(links) == 1 and links[0]["entity_id"] == "spec-x"
    assert json.loads(links[0]["metadata_json"])["kind"] == "spec"
    ev_types = [e["event_type"] for e in db.list_flow_events(cid)]
    assert "artifact.published" in ev_types and "spec.authored" in ev_types


def test_decompose_blocked_without_passing_review(monkeypatch, db):
    _enabled(monkeypatch)
    cid = db.open_case("obj", "sess-1")
    db.publish_spec(cid, "spec-x", "body")
    # No review yet ⇒ blocked.
    r = db.decompose_case(cid, "spec-x", [{"task_key": "t1", "objective": "do t1"}])
    assert r["ok"] is False and r["reason"] == "spec_not_approved"
    # A LOW score ⇒ still blocked (the gate is a real block, not a warning).
    db.record_spec_review(cid, "spec-x", LOW_SCORES)
    r = db.decompose_case(cid, "spec-x", [{"task_key": "t1", "objective": "do t1"}])
    assert r["ok"] is False and r["reason"] == "spec_not_approved"
    # A critical-zero score ⇒ still blocked.
    db.record_spec_review(cid, "spec-x", CRITICAL_ZERO_SCORES)
    r = db.decompose_case(cid, "spec-x", [{"task_key": "t1", "objective": "do t1"}])
    assert r["ok"] is False and r["reason"] == "spec_not_approved"
    # NOTHING was decomposed while blocked.
    assert db.list_flow_links(flow_run_id=cid, role="task_attached") == []


def test_decompose_after_pass_makes_one_case_dag_no_orphan_flow_runs(monkeypatch, db):
    _enabled(monkeypatch)
    cid = db.open_case("build feature X", "sess-1")
    db.publish_spec(cid, "spec-x", "body")
    review = db.record_spec_review(cid, "spec-x", GOOD_SCORES, reviewer="cheap-reviewer")
    assert review["passed"] is True

    flow_runs_before = _count_flow_runs(db)
    tasks = [
        {"task_key": "t1", "objective": "scaffold", "depends_on": []},
        {"task_key": "t2", "objective": "impl", "depends_on": ["t1"]},
        {"task_key": "t3", "objective": "tests", "depends_on": ["t1"]},
        {"task_key": "t4", "objective": "wire", "depends_on": ["t2", "t3"]},
    ]
    r = db.decompose_case(cid, "spec-x", tasks)
    assert r["ok"] is True
    assert set(r["task_keys"]) == {"t1", "t2", "t3", "t4"}

    # CORRECTNESS BAR 1: zero new flow_runs were created by decomposition.
    assert _count_flow_runs(db) == flow_runs_before

    # CORRECTNESS BAR 2: N task_attached links on the ONE Case, edges queryable.
    dag = db.list_dag_tasks(cid)
    assert len(dag) == 4
    by_key = {t["task_key"]: t for t in dag}
    assert by_key["t2"]["depends_on"] == ["t1"]
    assert by_key["t4"]["depends_on"] == ["t2", "t3"]

    # Topological order respects the edges (prereqs before dependents).
    order = r["order"]
    assert order.index("t1") < order.index("t2")
    assert order.index("t1") < order.index("t3")
    assert order.index("t2") < order.index("t4")
    assert order.index("t3") < order.index("t4")

    # case.decomposed event recorded.
    assert any(e["event_type"] == "case.decomposed" for e in db.list_flow_events(cid))


def test_decompose_refuses_cycle(monkeypatch, db):
    _enabled(monkeypatch)
    cid = db.open_case("obj", "sess-1")
    db.publish_spec(cid, "spec-x", "body")
    db.record_spec_review(cid, "spec-x", GOOD_SCORES)
    tasks = [
        {"task_key": "a", "objective": "a", "depends_on": ["b"]},
        {"task_key": "b", "objective": "b", "depends_on": ["a"]},
    ]
    r = db.decompose_case(cid, "spec-x", tasks)
    assert r["ok"] is False and r["reason"] == "cyclic_dependencies"
    assert db.list_flow_links(flow_run_id=cid, role="task_attached") == []


def test_decompose_refuses_unknown_dependency(monkeypatch, db):
    _enabled(monkeypatch)
    cid = db.open_case("obj", "sess-1")
    db.publish_spec(cid, "spec-x", "body")
    db.record_spec_review(cid, "spec-x", GOOD_SCORES)
    r = db.decompose_case(cid, "spec-x", [{"task_key": "a", "depends_on": ["ghost"]}])
    assert r["ok"] is False and r["reason"] == "unknown_dependency"
    assert db.list_flow_links(flow_run_id=cid, role="task_attached") == []


def test_decompose_refuses_duplicate_keys(monkeypatch, db):
    _enabled(monkeypatch)
    cid = db.open_case("obj", "sess-1")
    db.publish_spec(cid, "spec-x", "body")
    db.record_spec_review(cid, "spec-x", GOOD_SCORES)
    r = db.decompose_case(cid, "spec-x", [{"task_key": "a"}, {"task_key": "a"}])
    assert r["ok"] is False and r["reason"] == "invalid_task_keys"


def test_review_seat_is_separate_from_author(monkeypatch, db):
    """The scored-review actor is the reviewer seat, not the manager author."""
    _enabled(monkeypatch)
    cid = db.open_case("obj", "sess-1")
    db.publish_spec(cid, "spec-x", "body", actor="manager")
    db.record_spec_review(cid, "spec-x", GOOD_SCORES, reviewer="cheap-reviewer")
    authored = [e for e in db.list_flow_events(cid) if e["event_type"] == "spec.authored"]
    scored = [e for e in db.list_flow_events(cid) if e["event_type"] == "spec.review_scored"]
    assert authored[0]["actor"] == "manager"
    assert scored[0]["actor"] == "cheap-reviewer"
    assert authored[0]["actor"] != scored[0]["actor"]


# ---------------------------------------------------------------------------
# (3) publish_artifact — generic durable evidence
# ---------------------------------------------------------------------------

def test_publish_artifact(monkeypatch, db):
    _enabled(monkeypatch)
    cid = db.open_case("obj", "sess-1")
    r = db.publish_artifact(cid, "art-1", kind="report", title="R", uri="path/to/r")
    assert r["ok"] is True
    links = db.list_flow_links(flow_run_id=cid, entity_type="artifact")
    assert len(links) == 1 and links[0]["role"] == "report"
    assert any(e["event_type"] == "artifact.published" for e in db.list_flow_events(cid))


# ---------------------------------------------------------------------------
# (4) flag OFF ⇒ byte-identical (db writes nothing)
# ---------------------------------------------------------------------------

def test_db_writes_nothing_when_flag_off(monkeypatch, db):
    monkeypatch.delenv("SPEC_AUTHORING_ENABLED", raising=False)
    cid = db.open_case("obj", "sess-1")
    events_before = len(db.list_flow_events(cid))
    assert db.publish_spec(cid, "s", "b")["reason"] == "spec_authoring_disabled"
    assert db.publish_artifact(cid, "a")["reason"] == "spec_authoring_disabled"
    assert db.record_spec_review(cid, "s", GOOD_SCORES)["reason"] == "spec_authoring_disabled"
    assert db.decompose_case(cid, "s", [{"task_key": "t"}])["reason"] == "spec_authoring_disabled"
    # No new events, no links.
    assert len(db.list_flow_events(cid)) == events_before
    assert db.list_flow_links(flow_run_id=cid, entity_type="artifact") == []
    assert db.list_flow_links(flow_run_id=cid, role="task_attached") == []


# ---------------------------------------------------------------------------
# Control API fixtures (real orchestrator seams over a real temp db)
# ---------------------------------------------------------------------------

class _StubOrchestrator:
    def __init__(self) -> None:
        self.session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)

    publish_artifact = TaskOrchestrator.publish_artifact
    publish_spec = TaskOrchestrator.publish_spec
    record_spec_review = TaskOrchestrator.record_spec_review
    decompose_case = TaskOrchestrator.decompose_case


@pytest.fixture
def client(monkeypatch, db):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    monkeypatch.setattr(control_api, "_db", lambda: db)
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    return TestClient(control_api.build_control_api(_StubOrchestrator()))


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_e2e_route_flow_end_to_end(monkeypatch, client, db):
    """Full path over the API: author → score(low⇒block) → score(pass) → decompose."""
    _enabled(monkeypatch)
    cid = db.open_case("build feature X", "sess-1")

    # author
    r = client.post(f"/api/cases/{cid}/spec",
                    json={"spec_id": "spec-x", "body": "the spec", "title": "X"}, headers=_auth())
    assert r.status_code == 200 and r.json()["ok"] is True

    # score LOW ⇒ decompose blocked (422)
    r = client.post(f"/api/cases/{cid}/spec-review",
                    json={"spec_id": "spec-x", "scores": LOW_SCORES}, headers=_auth())
    assert r.status_code == 200 and r.json()["passed"] is False
    r = client.post(f"/api/cases/{cid}/decompose",
                    json={"spec_id": "spec-x", "tasks": [{"task_key": "t1"}]}, headers=_auth())
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "spec_not_approved"
    assert db.list_flow_links(flow_run_id=cid, role="task_attached") == []

    # score PASS ⇒ decompose allowed (200)
    r = client.post(f"/api/cases/{cid}/spec-review",
                    json={"spec_id": "spec-x", "scores": GOOD_SCORES, "reviewer": "cheap-reviewer"},
                    headers=_auth())
    assert r.status_code == 200 and r.json()["passed"] is True
    frbefore = _count_flow_runs(db)
    r = client.post(f"/api/cases/{cid}/decompose",
                    json={"spec_id": "spec-x", "tasks": [
                        {"task_key": "t1", "depends_on": []},
                        {"task_key": "t2", "depends_on": ["t1"]},
                    ]}, headers=_auth())
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _count_flow_runs(db) == frbefore  # no orphan flow_runs
    assert len(db.list_dag_tasks(cid)) == 2


def test_e2e_routes_404_when_flag_off(monkeypatch, client, db):
    monkeypatch.delenv("SPEC_AUTHORING_ENABLED", raising=False)
    cid = db.open_case("obj", "sess-1")
    for path, body in [
        (f"/api/cases/{cid}/spec", {"spec_id": "s", "body": "b"}),
        (f"/api/cases/{cid}/artifacts", {"artifact_id": "a"}),
        (f"/api/cases/{cid}/spec-review", {"spec_id": "s", "scores": GOOD_SCORES}),
        (f"/api/cases/{cid}/decompose", {"spec_id": "s", "tasks": [{"task_key": "t"}]}),
    ]:
        assert client.post(path, json=body, headers=_auth()).status_code == 404
    assert db.list_flow_events(cid) == [e for e in db.list_flow_events(cid)
                                        if not e["event_type"].startswith(("spec.", "artifact.", "case.decomposed"))]
