"""
A29 — session→case affiliation index (whole-substrate, no cap, no fanout).

The A28 UI resolved session affiliations by fetching each case's detail and
reading its ledger — capped at the first 100 cases and O(N) requests. A session
linked to a case beyond that window rendered a FALSE "Standalone", violating
milestone authority rule 7 (never infer absence when the link exists).

A29 replaces that with one authoritative JOIN (db.list_session_case_links →
build_session_affiliations) exposed at GET /api/work/affiliations/sessions:
covers every session link in the backlog, resolves multi-case links
deterministically, and never fabricates an affiliation the substrate did not
assert. These tests pin that behaviour end to end.
"""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api, work_read_model as wrm
from src.control.db import MeshDB
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


TOKEN = "test-control-token"


# --- pure builder ----------------------------------------------------------

def test_builder_dedup_role_norm_and_orphan_title():
    rows = [
        {"session_id": "s1", "flow_run_id": "f1", "role": "worker",
         "objective_lock": "<real_objective>Ship it</real_objective>", "status": "active"},
        # duplicate session — the FIRST (oldest link) wins, deterministically.
        {"session_id": "s1", "flow_run_id": "f2", "role": "reviewer",
         "objective_lock": "later", "status": "closed"},
        # unknown role collapses to generic "session"; orphan (no objective) title.
        {"session_id": "s2", "flow_run_id": "f2f2f2f2ff", "role": "custodian",
         "objective_lock": None, "status": None},
        # null session id is skipped, never an empty-string affiliation.
        {"session_id": None, "flow_run_id": "f9", "role": "worker",
         "objective_lock": "x", "status": None},
    ]
    out = wrm.build_session_affiliations(rows)
    assert out["total"] == 2
    by_id = {a["session_id"]: a for a in out["affiliations"]}
    assert by_id["s1"]["flow_run_id"] == "f1"       # first link wins
    assert by_id["s1"]["role"] == "worker"
    # Raw objective_lock rides along; the frontend derives the display title.
    assert by_id["s1"]["objective_lock"] == "<real_objective>Ship it</real_objective>"
    assert by_id["s2"]["role"] == "session"         # normalized
    assert by_id["s2"]["objective_lock"] is None


def test_builder_empty():
    assert wrm.build_session_affiliations([]) == {"affiliations": [], "total": 0}


def test_builder_skips_null_session_id():
    rows = [{"session_id": None, "flow_run_id": "f", "role": "worker",
             "objective_lock": "x", "status": None}]
    assert wrm.build_session_affiliations(rows)["total"] == 0


# --- db JOIN helper --------------------------------------------------------

def test_list_session_case_links_joins_and_covers_whole_substrate(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    # Two cases, each with a worker session + a non-session link that must be
    # excluded by the entity_type='session' filter.
    f1 = db.create_flow_run("t1", "execution", objective_lock="alpha")
    f2 = db.create_flow_run("t2", "intent", objective_lock="beta")
    db.update_flow_run(f2, status="blocked")
    db.create_flow_link(f1, "session", "sess-1", "worker")
    db.create_flow_link(f1, "task", "t1", "root_task")       # excluded
    db.create_flow_link(f2, "session", "sess-2", "worker")

    rows = db.list_session_case_links()
    got = {r["session_id"]: r for r in rows}
    assert set(got) == {"sess-1", "sess-2"}
    assert got["sess-1"]["flow_run_id"] == f1
    assert got["sess-1"]["objective_lock"] == "alpha"        # joined from flow_runs
    assert got["sess-2"]["status"] == "blocked"


def test_multi_case_session_resolves_to_newest(tmp_path):
    # A long-lived session that worked several cases must resolve to its MOST
    # RECENT case (newest link), not the first one it ever touched.
    db = MeshDB(str(tmp_path / "mesh.db"))
    old = db.create_flow_run("t-old", "closure", objective_lock="old case")
    new = db.create_flow_run("t-new", "execution", objective_lock="new case")
    db.create_flow_link(old, "session", "sess-long", "worker")
    db.create_flow_link(new, "session", "sess-long", "worker")  # newer link

    rows = db.list_session_case_links()
    out = wrm.build_session_affiliations(rows)
    aff = next(a for a in out["affiliations"] if a["session_id"] == "sess-long")
    assert aff["flow_run_id"] == new
    assert aff["objective_lock"] == "new case"


def test_list_session_case_links_orphan_link_keeps_row(tmp_path):
    # A session link whose flow_run row is absent must still resolve (LEFT JOIN),
    # not silently vanish — the affiliation is authoritative even if the summary
    # is gone.
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.create_flow_link("ghost-flow", "session", "sess-x", "worker")
    rows = db.list_session_case_links()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-x"
    assert rows[0]["objective_lock"] is None


def test_list_session_case_links_beyond_100_cases(tmp_path):
    # The exact defect the operator flagged: a truly-affiliated session in a
    # >100-case backlog. The reverse index must find it (no cap).
    db = MeshDB(str(tmp_path / "mesh.db"))
    target_flow = None
    for i in range(150):
        f = db.create_flow_run(f"t{i}", "execution")
        if i == 140:  # a case well beyond the old 100-case window
            target_flow = f
            db.create_flow_link(f, "session", "deep-session", "worker")
    rows = db.list_session_case_links()
    hit = [r for r in rows if r["session_id"] == "deep-session"]
    assert len(hit) == 1 and hit[0]["flow_run_id"] == target_flow


# --- endpoint --------------------------------------------------------------

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


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_affiliations_requires_token(client):
    assert client.get("/api/work/affiliations/sessions").status_code in (401, 403)


def test_affiliations_read_only(client):
    assert client.post("/api/work/affiliations/sessions", headers=_auth()).status_code == 405


def test_affiliations_empty_substrate(client):
    r = client.get("/api/work/affiliations/sessions", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"affiliations": [], "total": 0}


def test_affiliations_endpoint_returns_authoritative_index(client, db):
    f1 = db.create_flow_run("t1", "execution", objective_lock="Do the thing")
    db.create_flow_link(f1, "session", "sess-1", "worker")

    r = client.get("/api/work/affiliations/sessions", headers=_auth())
    assert r.status_code == 200
    model = r.json()
    assert model["total"] == 1
    a = model["affiliations"][0]
    assert a["session_id"] == "sess-1"
    assert a["flow_run_id"] == f1
    assert a["role"] == "worker"
    assert a["objective_lock"] == "Do the thing"


def test_affiliations_path_not_captured_as_flow_id(client, db):
    # The literal route must win over /api/work/{flow_run_id}: a flow literally
    # named 'affiliations' must NOT hijack the index route.
    r = client.get("/api/work/affiliations/sessions", headers=_auth())
    assert r.status_code == 200
    assert "affiliations" in r.json()
