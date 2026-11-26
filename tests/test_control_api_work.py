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
        self.close_calls = []

    def close_case(self, flow_run_id, **kwargs):
        self.close_calls.append((flow_run_id, kwargs))
        return {"ok": True, "closed": True, "reason": None}


@pytest.fixture
def db(tmp_path):
    return MeshDB(str(tmp_path / "mesh.db"))


@pytest.fixture
def orch():
    return _StubOrchestrator()


@pytest.fixture
def client(monkeypatch, db, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    monkeypatch.setattr(control_api, "_db", lambda: db)
    return TestClient(control_api.build_control_api(orch))


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


def test_operator_close_waives_completion_criteria(client, db, orch):
    fid = db.open_case("obj", "sess-1", completion_criteria='["tests green", "deployed"]')

    r = client.post(
        f"/api/cases/{fid}/operator-close",
        headers=_auth(),
        json={"reason": "stale blocked cleanup"},
    )

    assert r.status_code == 200
    assert r.json() == {"ok": True, "closed": True, "reason": None}
    assert orch.close_calls == [
        (
            fid,
            {
                "actor": "operator",
                "criteria_reconciliation": [
                    {
                        "criterion": "tests green",
                        "status": "waived",
                        "reason": "stale blocked cleanup",
                    },
                    {
                        "criterion": "deployed",
                        "status": "waived",
                        "reason": "stale blocked cleanup",
                    },
                ],
            },
        )
    ]


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


# --- [Cockpit] roster endpoint ---------------------------------------------

def test_work_roster_requires_token(client):
    assert client.get("/api/work/x/roster").status_code in (401, 403)


def test_work_roster_unknown_is_404(client):
    assert client.get("/api/work/nope/roster", headers=_auth()).status_code == 404


def test_work_roster_joins_sessions_and_jobs(client, db):
    # A case with a manager session link and a claude -p job owned by that session.
    case = db.create_flow_run("t-roster", "execution")
    db.create_flow_link(case, "session", "mgr_sess", "manager")
    # A non-session link must NOT leak into the roster's session join.
    db.create_flow_link(case, "task", "task_1", "root_task")
    db.register_job("job_x", "gateway-host", "IGNITION_1", session_id="mgr_sess",
                    command="claude -p --model opus 'do x'")

    r = client.get(f"/api/work/{case}/roster", headers=_auth())
    assert r.status_code == 200
    roster = r.json()
    assert roster["flow_run_id"] == case
    # Exactly the session link (not the task link) is in the roster.
    assert [s["session_id"] for s in roster["sessions"]] == ["mgr_sess"]
    assert roster["sessions"][0]["role"] == "manager"
    # No session row exists ⇒ honest present=false + zero tokens (not dropped).
    assert roster["sessions"][0]["present"] is False
    assert roster["sessions"][0]["tokens"]["total"] == 0
    # The claude -p job surfaces, flagged as an agent-spawn and running.
    assert roster["counts"]["jobs"] == 1 and roster["counts"]["running_jobs"] == 1
    job = roster["jobs"][0]
    assert job["job_id"] == "job_x" and job["is_agent_spawn"] is True
    assert job["status"] == "running" and job["session_id"] == "mgr_sess"


def test_work_roster_empty_case_is_clean(client, db):
    case = db.create_flow_run("t-empty", "intent")
    r = client.get(f"/api/work/{case}/roster", headers=_auth())
    assert r.status_code == 200
    roster = r.json()
    assert roster["counts"] == {"sessions": 0, "jobs": 0, "running_jobs": 0}
    assert roster["token_totals"]["total"] == 0


@pytest.mark.parametrize("members", [199, 200, 201, 2000])
def test_work_case_links_remain_reachable_and_summaries_complete(client, db, members):
    case = db.create_flow_run("t-large-case", "execution")
    db._conn().executemany(
        """INSERT INTO flow_links (flow_run_id, entity_type, entity_id, role, created_at)
           VALUES (?, 'session', ?, 'worker', 'now')""",
        ((case, f"worker-{i:04}") for i in range(members)),
    )
    last = f"worker-{members - 1:04}"
    db.register_job("last-worker-job", "gateway-host", "work", session_id=last)
    base = f"/api/work/{case}"
    detail = client.get(base, headers=_auth()).json()
    roster = client.get(f"{base}/roster", headers=_auth()).json()
    assert detail["counts"]["links"] == members
    assert roster["counts"] == {"sessions": members, "jobs": 1, "running_jobs": 1}
    assert [job["session_id"] for job in roster["jobs"]] == [last]
    for suffix, field, items in (
        ("", "ledger", lambda data: data["ledger"]["sessions"]),
        ("/timeline", "evidence", lambda data: data["evidence"]),
        ("/roster", "sessions", lambda data: data["sessions"]),
    ):
        seen = []
        url = base + suffix
        for _ in range(12):
            response = client.get(url, headers=_auth())
            assert response.status_code == 200
            payload = response.json()
            page = items(payload)
            assert len(page) <= 200
            seen.extend(row.get("entity_id") or row.get("session_id") for row in page)
            cursor = payload["next_" + ("session" if field == "sessions" else "link") + "_cursor"]
            if cursor is None:
                break
            url = base + suffix + f"?{'session' if field == 'sessions' else 'link'}_after={cursor}"
        assert seen == [f"worker-{i:04}" for i in range(members)]


def test_work_roster_jobs_page_with_stable_ties_and_historical_roles(client, db):
    case = db.create_flow_run("roster", "execution")
    db.create_flow_link(case, "session", "member", "worker")
    db.create_flow_link(case, "session", "member", "reviewer")
    db._conn().executemany(
        """INSERT INTO jobs (id, session_id, node_id, label, status, started_at, created_at, updated_at)
           VALUES (?, 'member', 'node', 'work', 'running', 'now', 'now', 'now')""",
        ((f"job-{i:04}",) for i in range(201)),
    )
    url = f"/api/work/{case}/roster"
    first = client.get(url, headers=_auth()).json()
    assert first["counts"] == {"sessions": 1, "jobs": 201, "running_jobs": 201}
    assert [member["role"] for member in first["sessions"]] == ["worker"]
    assert [job["job_id"] for job in first["jobs"]] == [f"job-{i:04}" for i in range(200, 0, -1)]
    last = client.get(url, headers=_auth(), params={"job_before": first["next_job_cursor"]}).json()
    assert [job["job_id"] for job in last["jobs"]] == ["job-0000"]
    assert last["next_job_cursor"] is None
    assert last["counts"] == first["counts"]
    assert client.get(url, headers=_auth(), params={"job_before": "invalid"}).status_code == 422
    assert client.get(url, headers=_auth(), params={"session_after": 2**63}).status_code == 422


def test_work_case_capacity_and_unavailable_db(client, db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    cid = db.create_flow_run("bounded", "execution")
    url = f"/api/work/{cid}/roster"
    entered, release = Barrier(5), Event()
    read_page = db.case_roster_session_page

    def blocked_page(*args):
        entered.wait(timeout=10)
        assert release.wait(timeout=10)
        return read_page(*args)

    monkeypatch.setattr(db, "case_roster_session_page", blocked_page)
    with ThreadPoolExecutor(max_workers=4) as pool:
        held = [pool.submit(client.get, url, headers=_auth()) for _ in range(4)]
        try:
            entered.wait(timeout=10)
            rejected = client.get(url, headers=_auth())
            assert rejected.status_code == 503
            assert rejected.json()["detail"] == "case_read_busy"
        finally:
            release.set()
        assert all(response.result().status_code == 200 for response in held)

    monkeypatch.setattr(control_api, "_db", lambda: None)
    unavailable = client.get(url, headers=_auth())
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "case_read_unavailable"
