"""U1 — embedded Control API tests (no network, no paid backend).

The Control API is built from a live orchestrator (build_control_api), so its
read handlers call orchestrator.session_service directly instead of side-reading
files. These tests use a minimal stand-in orchestrator wrapping the real
SessionService over an isolated store (conftest), plus the real DB for
tasks/nodes — mirroring test_dashboard.py's coverage on the new in-process surface.
"""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


TOKEN = "test-control-token"


class _StubOrchestrator:
    """Minimal orchestrator: only what the read handlers touch (session_service)."""

    def __init__(self) -> None:
        self.session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)


@pytest.fixture
def orch():
    return _StubOrchestrator()


@pytest.fixture
def client(monkeypatch, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(orch))


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- auth -------------------------------------------------------------------

def test_health_is_open(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_api_requires_token(client):
    assert client.get("/api/sessions").status_code in (401, 403)


def test_api_rejects_bad_token(client):
    assert client.get("/api/sessions", headers=_auth("wrong")).status_code == 401


def test_missing_server_token_is_500(monkeypatch, orch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "")
    c = TestClient(control_api.build_control_api(orch))
    assert c.get("/api/sessions", headers=_auth("anything")).status_code == 500


# --- read-model endpoints (fed by the live SessionService) ------------------

def test_sessions_endpoint_reflects_service(client, orch, tmp_path):
    from src.core.interfaces import SessionOrigin

    res = orch.session_service.create_session(
        backend="claude", repo_path=str(tmp_path), chat_id=1,
        origin=SessionOrigin("web", "user"),
    )
    assert res.ok

    r = client.get("/api/sessions", headers=_auth())
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    mine = next(s for s in sessions if s["session_id"] == res.session.session_id)
    assert mine["backend"] == "claude"
    assert mine["is_active"] is True
    assert mine["origin_channel"] == "web"


def test_sessions_limit_validation_and_bound(client, orch, tmp_path):
    for _ in range(5):
        orch.session_service.create_session(backend="claude", repo_path=str(tmp_path))
    assert client.get("/api/sessions?limit=0", headers=_auth()).status_code == 422
    assert client.get("/api/sessions?limit=99999", headers=_auth()).status_code == 422
    r = client.get("/api/sessions?limit=2", headers=_auth())
    assert r.status_code == 200 and len(r.json()["sessions"]) == 2


def test_tasks_and_nodes_endpoints_return_lists(client):
    rt = client.get("/api/tasks", headers=_auth())
    rn = client.get("/api/nodes", headers=_auth())
    assert rt.status_code == 200 and isinstance(rt.json()["tasks"], list)
    assert rn.status_code == 200 and isinstance(rn.json()["nodes"], list)


def test_tasks_limit_validation(client):
    assert client.get("/api/tasks?limit=0", headers=_auth()).status_code == 422
    assert client.get("/api/tasks?limit=99999", headers=_auth()).status_code == 422


def test_turn_graph_diagnostics_and_timeline_endpoints(client):
    from datetime import timedelta
    from src.control.db import get_db
    from src.control.telemetry_store import TelemetryStore
    from src.core.telemetry import build_event, utc_now

    db = get_db()
    assert db is not None
    store = TelemetryStore(db)
    start = utc_now()
    common = {
        "turn_id": "turn_control_api",
        "node_id": "gateway",
        "emitter_process_instance_id": "gateway_proc",
        "source": "gateway",
        "backend": "codex",
    }
    store.insert_events(
        [
            build_event("turn.started", event_time=start, observed_time=start, **common),
            build_event(
                "invocation.created",
                event_time=start,
                observed_time=start,
                invocation_id="inv_control_api",
                attributes={
                    "attempt": 1,
                    "spawn_reason": "initial",
                    "action": "run_oneoff",
                },
                **common,
            ),
            build_event(
                "turn.completed",
                event_time=start + timedelta(seconds=1),
                observed_time=start + timedelta(seconds=1),
                invocation_id="inv_control_api",
                attributes={
                    "status": "success",
                    "timeout_status": "none",
                    "exit_code": 0,
                },
                **common,
            ),
            build_event(
                "telemetry.coverage",
                event_time=start + timedelta(seconds=1),
                observed_time=start + timedelta(seconds=1),
                attributes={
                    "area": "usage",
                    "coverage": "aggregate_only",
                    "reason_code": "codex_turn_total_only",
                },
                **common,
            ),
        ]
    )

    turns = client.get("/api/turns", headers=_auth())
    detail = client.get("/api/turns/turn_control_api", headers=_auth())
    diagnostics = client.get(
        "/api/turns/turn_control_api/diagnostics", headers=_auth()
    )
    graph = client.get("/api/turns/turn_control_api/graph", headers=_auth())
    timeline = client.get("/api/turns/turn_control_api/events", headers=_auth())

    assert turns.status_code == 200
    assert any(turn["turn_id"] == "turn_control_api" for turn in turns.json()["turns"])
    assert detail.json()["final_status"] == "success"
    assert detail.json()["coverage"]["usage"]["coverage"] == "aggregate_only"
    assert len(diagnostics.json()["invocations"]) == 1
    assert diagnostics.json()["turn"]["metrics"]["model_request_count"] is None
    assert graph.json()["nodes"][0]["kind"] == "turn"
    assert len(timeline.json()["events"]) == 4


# --- Move G′: sectioned /api/tasks + session-status overlay ------------------

def test_tasks_sectioned_returns_five_buckets(client):
    r = client.get("/api/tasks?sectioned=true", headers=_auth())
    assert r.status_code == 200
    sections = r.json()["sections"]
    assert set(sections) == {"attention", "running", "queued", "failed", "recent"}
    assert all(isinstance(v, list) for v in sections.values())


def test_tasks_flat_shape_unchanged_by_default(client):
    # Backward-compat: no ?sectioned → the UI-2 flat shape, byte-for-byte.
    body = client.get("/api/tasks", headers=_auth()).json()
    assert "tasks" in body and "sections" not in body


def test_tasks_sectioned_overlays_session_status(client, orch, tmp_path):
    """An in-flight task whose session AWAITING_INPUT lands in `attention` as
    waiting_for_input — the bucket the flat mesh status alone can't reach."""
    from src.core.interfaces import SessionStatus
    from src.control.db import get_db

    res = orch.session_service.create_session(
        backend="claude", repo_path=str(tmp_path), chat_id=7,
    )
    assert res.ok
    sid = res.session.session_id
    # Drive the session to AWAITING_INPUT via the store (the overlay source).
    sess = orch.session_service.store.get(sid)
    sess.status = SessionStatus.AWAITING_INPUT
    orch.session_service.store.save(sess)
    # An active (pending) task pointing at that session.
    get_db().enqueue_task(
        task_id="task_overlay", session_id=sid, machine_id=None,
        backend="claude", action="run_oneoff", payload={"prompt": "x"},
    )

    sections = client.get("/api/tasks?sectioned=true", headers=_auth()).json()["sections"]
    found = next((t for t in sections["attention"] if t["id"] == "task_overlay"), None)
    assert found is not None, "overlaid task should be in attention"
    assert found["ui_state"] == "waiting_for_input"
    assert found["section"] == "attention"


# --- Move H: approvals (durable gate) ---------------------------------------

def test_approvals_list_empty_by_default(client):
    r = client.get("/api/approvals", headers=_auth())
    assert r.status_code == 200 and r.json()["approvals"] == []


def test_approval_request_then_pending_then_resolve(client):
    # request → pending shows it
    rq = client.post("/api/approvals", headers=_auth(),
                     json={"action": "deploy to prod", "session_id": "s1", "risk": "high"})
    assert rq.status_code == 200
    appr = rq.json()["approval"]
    assert appr["status"] == "pending" and appr["action"] == "deploy to prod"
    appr_id = appr["id"]

    pend = client.get("/api/approvals?status=pending", headers=_auth()).json()["approvals"]
    assert any(a["id"] == appr_id for a in pend)

    # resolve approved → leaves the queue
    rr = client.post(f"/api/approvals/{appr_id}/resolve", headers=_auth(),
                     json={"decision": "approved", "resolved_by": "me"})
    assert rr.status_code == 200 and rr.json()["approval"]["status"] == "approved"
    pend2 = client.get("/api/approvals?status=pending", headers=_auth()).json()["approvals"]
    assert not any(a["id"] == appr_id for a in pend2)


def test_resolve_twice_is_409(client):
    appr_id = client.post("/api/approvals", headers=_auth(),
                          json={"action": "x", "session_id": "s1"}).json()["approval"]["id"]
    assert client.post(f"/api/approvals/{appr_id}/resolve", headers=_auth(),
                       json={"decision": "approved"}).status_code == 200
    again = client.post(f"/api/approvals/{appr_id}/resolve", headers=_auth(),
                        json={"decision": "rejected"})
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "already_resolved"


def test_resolve_missing_is_404(client):
    r = client.post("/api/approvals/nope/resolve", headers=_auth(),
                    json={"decision": "approved"})
    assert r.status_code == 404 and r.json()["detail"]["reason"] == "not_found"


def test_request_missing_action_is_400(client):
    r = client.post("/api/approvals", headers=_auth(), json={"action": "", "session_id": "s1"})
    assert r.status_code == 400 and r.json()["detail"]["reason"] == "missing_action"


# --- UI-4: artifacts / files ------------------------------------------------

def _seed_artifact(tmp_path, task_id, **fields):
    import json
    body = {"task_id": task_id, "success": True, "timestamp": "2026-06-24T00:00:00"}
    body.update(fields)
    (tmp_path / f"{task_id}.json").write_text(json.dumps(body), encoding="utf-8")


def test_artifacts_list_and_detail(client, monkeypatch, tmp_path):
    monkeypatch.setattr(control_api, "_results_dir", lambda: tmp_path)
    _seed_artifact(tmp_path, "task_a4", files_modified=["x.py", "y.py"])

    r = client.get("/api/artifacts", headers=_auth())
    assert r.status_code == 200
    rows = r.json()["artifacts"]
    assert any(a["task_id"] == "task_a4" and a["file_count"] == 2 for a in rows)

    rd = client.get("/api/artifacts/task_a4", headers=_auth())
    assert rd.status_code == 200
    body = rd.json()
    assert body["artifact"]["task_id"] == "task_a4"
    assert [f["path"] for f in body["files"]] == ["x.py", "y.py"]
    assert all(f["change"] == "modified" for f in body["files"])


def test_artifact_missing_is_404(client, monkeypatch, tmp_path):
    monkeypatch.setattr(control_api, "_results_dir", lambda: tmp_path)
    assert client.get("/api/artifacts/task_nope", headers=_auth()).status_code == 404
    # Handler-level path confinement (a ``..`` task_id resolves to None rather than
    # escaping results_dir) is covered directly by
    # test_artifacts::test_get_rejects_traversal — the HTTP router normalizes a
    # cross-segment ``..`` before it ever reaches this handler, so it can't be
    # exercised through TestClient here.


def test_artifacts_requires_auth(client):
    assert client.get("/api/artifacts").status_code in (401, 403)


# --- nodes: DB fallback annotates liveness when the registry is empty -------

def test_nodes_fallback_annotates_live_when_registry_empty(client):
    from src.control.db import get_db
    d = get_db()
    d.upsert_node(node_id="N1", tailscale_ip="127.0.0.1", api_port=9001,
                  backends=["claude"], max_concurrent=1)  # last_heartbeat=now
    r = client.get("/api/nodes", headers=_auth())
    assert r.status_code == 200
    n1 = next(n for n in r.json()["nodes"] if n["node_id"] == "N1")
    assert n1["live"] is True
    assert "heartbeat_age_sec" in n1


# --- events poll (same reader as the dashboard) -----------------------------

def test_events_endpoint(client, monkeypatch, tmp_path):
    from src.core import observability
    monkeypatch.setattr(observability, "_LOGS_DIR", tmp_path)
    observability.emit_event("dispatch", task_id="tX")
    r = client.get("/api/events", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert any(e["event"] == "dispatch" for e in body["events"])
    r2 = client.get(f"/api/events?since={body['offset']}", headers=_auth())
    assert r2.json()["events"] == []
