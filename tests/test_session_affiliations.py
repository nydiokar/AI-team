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
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.control import work_read_model as wrm
from src.control.db import MeshDB
from src.services.session_service import SessionService
from src.services.session_store import SessionStore

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


def test_compact_affiliations_preserve_newest_and_missing_case(client, db):
    old = db.create_flow_run("old", "execution", objective_lock="old")
    new = db.create_flow_run("new", "execution", objective_lock="<real_objective>New case</real_objective>")
    db.update_flow_run(new, status="closed")
    db.create_flow_link(old, "session", "shared", "worker")
    db.create_flow_link(new, "session", "shared", "manager")
    db.create_flow_link("ghost-12345678", "session", "orphan", "worker")
    response = client.get("/api/work/affiliations/sessions?format=compact", headers=_auth())
    assert response.status_code == 200
    rows = {row["session_id"]: row for row in response.json()["affiliations"]}
    assert rows["shared"]["flow_run_id"] == new
    assert rows["shared"]["case_status"] == "closed"
    assert rows["shared"]["case_title"] == "New case"
    assert "objective_lock" not in rows["shared"]
    assert rows["orphan"]["flow_run_id"] == "ghost-12345678"


def test_compact_affiliations_unavailable_is_not_empty(client, monkeypatch):
    monkeypatch.setattr(control_api, "_db", lambda: None)
    response = client.get("/api/work/affiliations/sessions?format=compact", headers=_auth())
    assert response.status_code == 503


TITLE_FIXTURES = json.loads((Path(__file__).parents[1] / "web/src/transport/caseTitles.golden.json").read_text())


@pytest.mark.parametrize("fixture", TITLE_FIXTURES, ids=lambda f: f["name"])
def test_case_title_common_golden(fixture):
    assert wrm.case_title(fixture["objective"], fixture["flow_run_id"]) == b"".join(unit.to_bytes(2, "little") for unit in fixture["expected_utf16"]).decode("utf-16-le", "surrogatepass")


def test_compact_complete_mapping_and_streaming(db, client, monkeypatch):
    for i in range(230):
        fid = db.create_flow_run(f"t{i}", "execution", objective_lock=f"Title {i}")
        db.create_flow_link(fid, "session", f"s{i}", " Worker ")
    db.create_flow_link(fid, "session", "shared", "unknown")
    old = db.create_flow_run("obsolete", "execution", objective_lock="MUST NOT READ")
    db.create_flow_link(old, "session", "shared", "reviewer")
    db.create_flow_link(fid, "session", "shared", "worker")
    db.create_flow_link("ghost-12345678", "session", "orphan", "evidence")
    legacy = wrm.build_session_affiliations(db.list_session_case_links())
    calls = []
    original = wrm.case_title
    def title(objective, fid):
        calls.append(objective)
        return original(objective, fid)
    monkeypatch.setattr(wrm, "case_title", title)
    statements = []
    db._conn().set_trace_callback(statements.append)
    with db.compact_session_case_data() as (links, cases):
        assert iter(cases) is cases  # Objectives are consumed one row at a time.
        compact = wrm.build_compact_session_affiliations(links, cases)
    db._conn().set_trace_callback(None)
    expected = [{k: v for k, v in row.items() if k != "objective_lock"} |
                {"case_title": original(row["objective_lock"], row["flow_run_id"])}
                for row in legacy["affiliations"]]
    assert compact == {"affiliations": expected, "total": 232}
    assert len(calls) == 231  # once/case including missing case, no obsolete objective
    assert "MUST NOT READ" not in calls
    reads = [s for s in statements if s.startswith("WITH newest")]
    assert len(reads) == 2
    response = client.get("/api/work/affiliations/sessions?format=compact", headers=_auth())
    assert response.json() == compact


def test_compact_snapshot_and_release_on_failure(db):
    fid = db.create_flow_run("snapshot", "execution", objective_lock="before")
    db.create_flow_link(fid, "session", "s", "worker")
    with db.compact_session_case_data() as (links, cases):
        writer = sqlite3.connect(db._conn().execute("PRAGMA database_list").fetchone()[2])
        writer.execute("UPDATE flow_runs SET objective_lock='after', status='closed' WHERE flow_run_id=?", (fid,))
        writer.execute("INSERT INTO flow_links(flow_run_id,entity_type,entity_id,role,created_at) VALUES (?, 'session','new','worker','now')", (fid,))
        writer.commit()
        writer.close()
        result = wrm.build_compact_session_affiliations(links, cases)
    assert result["total"] == 1
    assert result["affiliations"][0]["case_title"] == "before"
    with pytest.raises(RuntimeError), db.compact_session_case_data():
        raise RuntimeError("consumer failed")
    assert not db._conn().in_transaction
    with db.compact_session_case_data() as (links, cases):
        result = wrm.build_compact_session_affiliations(links, cases)
    assert result["total"] == 2
    assert result["affiliations"][0]["case_title"] == "after"


def test_compact_surrogate_json_and_legacy_detail(client, db):
    objective = "a" * 116 + "😀" + "z" * 5
    fid = db.create_flow_run("unicode", "execution", objective_lock=objective)
    db.create_flow_link(fid, "session", "s", "worker")
    response = client.get("/api/work/affiliations/sessions?format=compact", headers=_auth())
    assert response.status_code == 200
    assert response.json()["affiliations"][0]["case_title"] == "a" * 116 + "\ud83d…"
    assert b"\\ud83d" in response.content
    assert client.get("/api/work/affiliations/sessions", headers=_auth()).json()["affiliations"][0]["objective_lock"] == objective
    assert client.get(f"/api/work/{fid}", headers=_auth()).json()["case"]["objective_lock"] == objective


@pytest.mark.parametrize("value", ["bad", "compact " , "x" * 10000])
def test_compact_invalid_format(client, value):
    assert client.get("/api/work/affiliations/sessions", params={"format": value}, headers=_auth()).status_code == 422


def test_compact_models(client):
    schema = client.app.openapi()
    variants = schema["paths"]["/api/work/affiliations/sessions"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["anyOf"]
    assert len(variants) == 2
    fields = schema["components"]["schemas"]["CompactSessionAffiliation"]["properties"]
    assert "case_title" in fields and "objective_lock" not in fields


@pytest.mark.parametrize("format", ["legacy", "compact"])
def test_compact_gate_includes_serialization_and_releases(client, db, monkeypatch, format):
    from src.control.routes import work
    original = work.json.dumps
    entered = Event()
    release = Event()
    lock = Lock()
    count = 0
    def blocked(value, *args, **kwargs):
        nonlocal count
        if isinstance(value, dict) and "affiliations" in value:
            with lock:
                count += 1
                if count == 4:
                    entered.set()
            assert release.wait(10)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(work.json, "dumps", blocked)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(client.get, f"/api/work/affiliations/sessions?format={format}", headers=_auth()) for _ in range(4)]
        try:
            assert entered.wait(10)
            for _ in range(96):
                assert client.get(f"/api/work/affiliations/sessions?format={format}", headers=_auth()).status_code == 429
            assert count == 4
        finally:
            release.set()
        assert [f.result().status_code for f in futures] == [200] * 4
    if format == "legacy":
        return  # Both serializers were held above; compact-specific failures below.
    def fail(*args, **kwargs):
        raise ValueError("serialization failed")
    # Patch the serializer at its call site via a wrapper to avoid also breaking
    # Starlette's structured HTTP error rendering (same stdlib json module).
    monkeypatch.setattr(work, "json", type("BrokenJSON", (), {"dumps": fail}))
    monkeypatch.setattr(json, "dumps", original)
    for _ in range(5):
        assert client.get(f"/api/work/affiliations/sessions?format={format}", headers=_auth()).status_code == 503
    monkeypatch.setattr(work, "json", json)
    monkeypatch.setattr(db, "compact_session_case_data", fail)
    for _ in range(5):
        assert client.get(f"/api/work/affiliations/sessions?format={format}", headers=_auth()).status_code == 503


@pytest.mark.parametrize("objective", ["<" * 65536, "<real_objective>" * 4096])
def test_compact_malformed_large_markup(objective):
    assert wrm.case_title(objective, "flow_12345678") == (
        "<" * 117 + "…" if objective.startswith("<<") else "case 12345678"
    )


def test_compact_db_acquisition_error_releases_gate(client, monkeypatch):
    def fail():
        raise sqlite3.OperationalError("private path")
    monkeypatch.setattr(control_api, "_db", fail)
    for _ in range(5):
        response = client.get("/api/work/affiliations/sessions?format=compact", headers=_auth())
        assert response.status_code == 503
        assert response.json() == {"detail": "affiliations_unavailable"}


@pytest.mark.parametrize("format", ["legacy", "compact"])
def test_both_formats_report_missing_database_and_release_gate(monkeypatch, format):
    from src.control.routes import work
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    monkeypatch.setattr(control_api, "_db", lambda: None)
    with TestClient(control_api.build_control_api(_StubOrchestrator())) as client:
        for _ in range(6):
            response = client.get(f"/api/work/affiliations/sessions?format={format}", headers={"Authorization": f"Bearer {TOKEN}"})
            assert response.status_code == 503
            assert response.json() == {"detail": "affiliations_unavailable"}
    for _ in range(4):
        assert work._affiliations_gate.acquire(blocking=False)
    for _ in range(4):
        work._affiliations_gate.release()
