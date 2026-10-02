"""A82 Stage 6 — UI/API truth and compatibility (packet §10, design §9).

Real control API (FastAPI TestClient) over a real bound ``TaskOrchestrator``
(bare ``__new__`` instance, real ``submit_instruction`` → real admission) and a
real file-backed ``MeshDB``. Only the backend/CLI is absent (spawn guard).

S6-01 create: 202 receipt (turn/task id, status, revision, sequence, position,
      acceptance time); idempotent replay returns the same id and CURRENT state
S6-02 list: bounded cursor pages, FIFO (active first), previews only, typed keys
S6-03 detail + edit: full body once; stale If-Match / consumed turn ⇒ 409 with a
      safe current summary; an activated prompt is never overwritten; sequence kept
S6-04 withdraw: only the selected queued item; stale/consumed ⇒ 409
S6-05 stop: persistent operator pause + cancels only the active turn; a later
      admission does not resume; resume releases the stop hold
S6-06 resume never clears recovery / provider-deadline holds
S6-07 recovery resolution (incl. an oversize-artifact hold) reachable via the API
S6-08 compat /api/instructions: canonical id + truthful session (queued ≠ busy,
      active managed turn visible, a later rejected admission changes nothing)
S6-09 post-commit SSE events for every queue mutation; none on a refused one
S6-10 scheduler activation + carrier transitions emit post-commit events
S6-11 transcript never shows a waiting/withdrawn prompt; carries status
S6-12 status consumers: task truth + task lifecycle + stale-BUSY scan
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient

import src.control.db as db_mod
from src.control import control_api
from src.control import turn_admission as ta
from src.control import turn_scheduler as sched
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from src.core.session_task_queue import SessionTaskQueue
from src.orchestrator import TaskOrchestrator

NOW: str = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc).isoformat()
TOKEN: str = "s6-admin"
AUTH: Dict[str, str] = {"Authorization": f"Bearer {TOKEN}"}
SID: str = "sess-1"


@pytest.fixture(autouse=True)
def _no_cli_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.backends import claude_driver

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("real CLI spawn attempted in an offline test")

    monkeypatch.setattr(claude_driver._SDKSession, "start", _boom, raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    monkeypatch.delenv("HARNESS_FLOW_DRIVE", raising=False)
    monkeypatch.delenv("HARNESS_LEVEL3_GUARD", raising=False)
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


class _Env:
    def __init__(self, db: MeshDB, orch: TaskOrchestrator, client: TestClient,
                 events: List[Tuple[str, Dict[str, Any]]]) -> None:
        self.db = db
        self.orch = orch
        self.client = client
        self.events = events

    def create(self, body: str, op: str) -> Any:
        return self.client.post(f"/api/sessions/{SID}/turn-requests", headers=AUTH,
                                json={"body": body, "operation_id": op})

    def schedule(self) -> sched.SchedulerPassResult:
        return asyncio.run(sched.run_scheduler_pass(
            self.db, self.orch._prepare_managed_turn, allowance=ta.SharedWaitingAllowance(),
        ))

    def queue_events(self) -> List[Dict[str, Any]]:
        return [f for name, f in self.events if name == "turn_queue_changed"]


@pytest.fixture()
def env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> _Env:
    from src.core import observability
    from src.services.session_service import SessionService
    from src.services.session_store import SessionStore

    db: MeshDB = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    monkeypatch.setattr(control_api, "_db", lambda: db)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    db.upsert_session(Session(
        session_id=SID, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a",
    ))
    db.enroll_session(SID)
    db.upsert_node(node_id="worker-a", tailscale_ip="", api_port=9001, backends=["claude"],
                   max_concurrent=2, managed_backends=["claude"], incarnation_id="inc-1")
    o: TaskOrchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
    o.session_store = SessionStore()
    o.session_service = SessionService(o.session_store, repo_path_validator=lambda _p: None)
    o.task_queue = SessionTaskQueue(50, lambda _t: "")
    o.active_tasks = {}
    o._compact_injected_ids = set()
    o._backends = {"claude": object()}
    o._emit_event = lambda name, task, data=None: None
    o._emit_turn_telemetry = lambda name, task, data=None, **k: None
    events: List[Tuple[str, Dict[str, Any]]] = []
    real_emit = observability.emit_event

    def _capture(name: str, **fields: Any) -> None:
        events.append((name, fields))
        real_emit(name, **fields)

    monkeypatch.setattr(observability, "emit_event", _capture)
    return _Env(db, o, TestClient(control_api.build_control_api(o)), events)


def _claim_start(db: MeshDB, tid: str) -> str:
    tok = db.claim_turn(tid, "worker-a", "worker_daemon", "inc-1")
    db.start_turn(tid, tok, incarnation_id="inc-1")
    return str(tok)


SUMMARY_KEYS = {
    "id", "turn_id", "session_id", "status", "revision", "queue_sequence", "queue_position",
    "turn_source", "turn_kind", "sender_session_id", "blocked_reason", "created_at",
    "activated_at", "started_at", "preview",
}


# S6-01 --------------------------------------------------------------------- #
def test_S6_01_create_202_receipt_and_replay_returns_current_state(env: _Env) -> None:
    r = env.create("first", "op-1")
    assert r.status_code == 202, r.text
    receipt: Dict[str, Any] = r.json()
    tid: str = receipt["turn_id"]
    assert receipt["task_id"] == tid
    assert receipt["status"] == "queued" and receipt["revision"] == 1
    assert receipt["queue_sequence"] == 1 and receipt["queue_position"] == 1
    assert receipt["accepted_at"] and receipt["idempotent_replay"] is False
    assert env.db.get_task(tid)["status"] == "queued"  # committed before the ack
    # Edit, then replay the ORIGINAL create: same id, CURRENT revision/body.
    assert env.client.patch(f"/api/turn-requests/{tid}", headers={**AUTH, "If-Match": "1"},
                            json={"body": "revised"}).status_code == 200
    replay = env.create("first", "op-1")
    assert replay.status_code == 202
    assert replay.json()["turn_id"] == tid and replay.json()["idempotent_replay"] is True
    assert replay.json()["revision"] == 2
    assert env.db.get_turn_request(tid)["body"] == "revised"
    # Same key, different original input ⇒ 409, nothing new admitted.
    assert env.create("different", "op-1").status_code == 409
    assert env.client.get(f"/api/sessions/{SID}/turn-requests", headers=AUTH).json()["count"] == 1


# S6-02 --------------------------------------------------------------------- #
def test_S6_02_list_is_bounded_fifo_previews_only(env: _Env) -> None:
    big: str = "é" * 6000  # 12 000 UTF-8 bytes (< 16 KiB), > 2 KiB preview
    ids: List[str] = [env.create(f"{i}-{big}", f"op-{i}").json()["turn_id"] for i in range(3)]
    assert env.schedule().activated == 1  # the head becomes the active slot holder
    page1 = env.client.get(f"/api/sessions/{SID}/turn-requests?limit=2", headers=AUTH)
    assert page1.status_code == 200
    body: Dict[str, Any] = page1.json()
    assert [t["id"] for t in body["turns"]] == ids[:2]
    assert body["count"] == 3 and body["queued"] == 2
    assert body["active_turn_id"] == ids[0] and body["active_status"] == "pending"
    assert body["enrolled"] is True and body["paused"] is False and body["hold"] is None
    assert body["next_cursor"] is not None
    for i, turn in enumerate(body["turns"]):
        assert set(turn) == SUMMARY_KEYS, set(turn) ^ SUMMARY_KEYS
        assert turn["queue_position"] == i + 1
        assert len(turn["preview"].encode("utf-8")) <= 2048
        assert turn["preview"] != f"{i}-{big}"  # never the full prompt in a list
    assert body["turns"][0]["status"] == "pending" and body["turns"][1]["status"] == "queued"
    page2 = env.client.get(
        f"/api/sessions/{SID}/turn-requests?limit=2&cursor={body['next_cursor']}", headers=AUTH,
    ).json()
    assert [t["id"] for t in page2["turns"]] == ids[2:]
    assert page2["turns"][0]["queue_position"] == 3 and page2["next_cursor"] is None
    assert env.client.get(f"/api/sessions/{SID}/turn-requests?limit=101",
                          headers=AUTH).status_code == 422
    assert env.client.get("/api/sessions/nope/turn-requests", headers=AUTH).status_code == 404
    assert env.client.get(f"/api/sessions/{SID}/turn-requests").status_code == 401


# S6-03 --------------------------------------------------------------------- #
def test_S6_03_detail_and_edit_conflicts_never_overwrite_consumed_prompt(env: _Env) -> None:
    a: str = env.create("alpha", "op-a").json()["turn_id"]
    b: str = env.create("bravo", "op-b").json()["turn_id"]
    detail = env.client.get(f"/api/turn-requests/{b}", headers=AUTH).json()
    assert detail["body"] == "bravo" and detail["queue_position"] == 2
    assert "claim_token" not in detail and "payload" not in detail
    ok = env.client.patch(f"/api/turn-requests/{b}", headers={**AUTH, "If-Match": "1"},
                          json={"body": "bravo v2"})
    assert ok.status_code == 200 and ok.json()["revision"] == 2 and ok.json()["body"] == "bravo v2"
    assert ok.json()["queue_sequence"] == 2  # edit preserves sequence
    stale = env.client.patch(f"/api/turn-requests/{b}", headers={**AUTH, "If-Match": "1"},
                             json={"body": "lost update"})
    assert stale.status_code == 409
    current: Dict[str, Any] = stale.json()["detail"]["current"]
    assert current["id"] == b and current["revision"] == 2 and current["status"] == "queued"
    # The head is consumed (activated): an edit is refused and the prompt kept.
    assert env.schedule().activated == 1
    late = env.client.patch(f"/api/turn-requests/{a}", headers={**AUTH, "If-Match": "1"},
                            json={"body": "too late"})
    assert late.status_code == 409 and late.json()["detail"]["current"]["status"] == "pending"
    assert env.db.get_turn_request(a)["body"] == "alpha"
    assert env.client.patch(f"/api/turn-requests/{b}", headers=AUTH,
                            json={"body": "x"}).status_code == 422  # If-Match required
    assert env.client.get("/api/turn-requests/missing", headers=AUTH).status_code == 404


# S6-04 --------------------------------------------------------------------- #
def test_S6_04_withdraw_only_the_selected_queued_item(env: _Env) -> None:
    a, b, c = (env.create(t, f"op-{t}").json()["turn_id"] for t in ("a", "b", "c"))
    r = env.client.post(f"/api/turn-requests/{b}/withdraw", headers={**AUTH, "If-Match": "1"})
    assert r.status_code == 200 and r.json()["status"] == "withdrawn" and r.json()["revision"] == 2
    assert [env.db.get_task(t)["status"] for t in (a, b, c)] == ["queued", "withdrawn", "queued"]
    listing = env.client.get(f"/api/sessions/{SID}/turn-requests", headers=AUTH).json()
    assert [t["id"] for t in listing["turns"]] == [a, c]
    assert [t["queue_position"] for t in listing["turns"]] == [1, 2]
    stale = env.client.post(f"/api/turn-requests/{c}/withdraw", headers={**AUTH, "If-Match": "7"})
    assert stale.status_code == 409 and env.db.get_task(c)["status"] == "queued"
    assert env.schedule().activated == 1  # a is consumed
    consumed = env.client.post(f"/api/turn-requests/{a}/withdraw", headers={**AUTH, "If-Match": "1"})
    assert consumed.status_code == 409 and env.db.get_task(a)["status"] == "pending"


# S6-05 --------------------------------------------------------------------- #
def test_S6_05_stop_is_persistent_pause_and_cancels_only_the_active_turn(env: _Env) -> None:
    a: str = env.create("a", "op-a").json()["turn_id"]
    b: str = env.create("b", "op-b").json()["turn_id"]
    assert env.schedule().activated == 1
    stop = env.client.post(f"/api/sessions/{SID}/stop", headers=AUTH)
    assert stop.status_code == 200 and stop.json()["task_id"] == a
    assert env.db.get_task(a)["status"] == "cancelled"
    assert env.db.get_task(b)["status"] == "queued"  # waiting work untouched
    page = env.client.get(f"/api/sessions/{SID}/turn-requests", headers=AUTH).json()
    assert page["paused"] is True
    assert env.schedule().activated == 0, "stop launched the next queued instruction"
    # A later operator admission does NOT silently resume the paused queue.
    c: str = env.create("c", "op-c").json()["turn_id"]
    assert env.schedule().activated == 0
    resumed = env.client.post(f"/api/sessions/{SID}/turn-requests/resume", headers=AUTH)
    assert resumed.status_code == 200 and resumed.json()["paused"] is False
    assert resumed.json()["hold"] is None
    assert env.db.get_session(SID)["status"] != "cancelled"
    assert env.schedule().activated == 1
    assert env.db.get_task(b)["status"] == "pending" and env.db.get_task(c)["status"] == "queued"


def test_S6_05b_pause_without_active_turn_holds_and_resume_releases(env: _Env) -> None:
    paused = env.client.post(f"/api/sessions/{SID}/turn-requests/pause", headers=AUTH)
    assert paused.status_code == 200 and paused.json()["paused"] is True
    t: str = env.create("x", "op-x").json()["turn_id"]
    assert env.schedule().activated == 0 and env.db.get_task(t)["status"] == "queued"
    assert env.client.post(f"/api/sessions/{SID}/turn-requests/resume",
                           headers=AUTH).json()["paused"] is False
    assert env.schedule().activated == 1


# S6-06 --------------------------------------------------------------------- #
def test_S6_06_resume_does_not_clear_recovery_or_provider_holds(env: _Env) -> None:
    a: str = env.create("a", "op-a").json()["turn_id"]
    b: str = env.create("b", "op-b").json()["turn_id"]
    assert env.schedule().activated == 1
    tok: str = _claim_start(env.db, a)
    assert env.db.enter_recovery(a, tok, reason="carrier lost after start")
    env.client.post(f"/api/sessions/{SID}/turn-requests/pause", headers=AUTH)
    env.client.post(f"/api/sessions/{SID}/turn-requests/resume", headers=AUTH)
    assert env.db.get_task(a)["status"] == "recovery_required"
    assert env.schedule().activated == 0 and env.db.get_task(b)["status"] == "queued"
    # A provider deadline (not_before) on the waiting head survives a resume too.
    env.db._conn().execute("UPDATE mesh_tasks SET not_before = '2999-01-01T00:00:00+00:00' WHERE id = ?", (b,))
    env.db._conn().commit()
    env.client.post(f"/api/sessions/{SID}/turn-requests/resume", headers=AUTH)
    assert env.db.get_task(b)["not_before"].startswith("2999")


# S6-07 --------------------------------------------------------------------- #
def test_S6_07_oversize_recovery_hold_resolved_through_the_api(env: _Env) -> None:
    a: str = env.create("a", "op-a").json()["turn_id"]
    b: str = env.create("b", "op-b").json()["turn_id"]
    assert env.schedule().activated == 1
    tok: str = _claim_start(env.db, a)
    reason: str = "managed_result_oversize: artifact=/spool/oversize/a.json; node=worker-a; output_chars=9"
    assert env.db.enter_recovery(a, tok, reason=reason)
    page = env.client.get(f"/api/sessions/{SID}/turn-requests", headers=AUTH).json()
    assert page["active_status"] == "recovery_required"
    assert page["turns"][0]["status"] == "recovery_required"
    assert "artifact=/spool/oversize/a.json" in page["turns"][0]["blocked_reason"]
    url: str = f"/api/turn-requests/{a}/resolve-recovery"
    no_ack = env.client.post(url, headers=AUTH, json={"decision": "failed"})
    assert no_ack.status_code == 409 and env.db.get_task(a)["status"] == "recovery_required"
    res = env.client.post(url, headers=AUTH, json={"decision": "failed", "acknowledge_uncertain": True,
                                                   "note": "read the artifact"})
    assert res.status_code == 200 and res.json() == {"ok": True, "task_id": a, "status": "failed"}
    assert env.db.get_task(a)["status"] == "failed"
    assert env.schedule().activated == 1 and env.db.get_task(b)["status"] == "pending"


# S6-08 --------------------------------------------------------------------- #
def test_S6_08_compat_instructions_canonical_id_and_truthful_session(
    env: _Env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = env.client.post("/api/instructions", headers={**AUTH, "Idempotency-Key": "k-1"},
                        json={"description": "hello", "session_id": SID})
    assert r.status_code == 200
    body: Dict[str, Any] = r.json()
    assert set(body) == {"ok", "task_id", "session"} and body["ok"] is True
    tid: str = body["task_id"]
    assert env.db.get_task(tid)["queue_protocol"] == 1  # the canonical ledger id
    session: Dict[str, Any] = body["session"]
    assert session["status"] == "idle"  # queued is not BUSY
    assert session["turn_queue"] == {"queued": 1, "active_turn_id": None, "active_status": None,
                                     "paused": False, "hold": None}
    assert env.schedule().activated == 1
    _claim_start(env.db, tid)
    listed = {s["session_id"]: s for s in env.client.get("/api/sessions", headers=AUTH).json()["sessions"]}
    assert listed[SID]["turn_queue"]["active_status"] == "running"
    assert listed[SID]["turn_queue"]["active_turn_id"] == tid
    # A later REJECTED admission must not mark the session idle or drop the active turn.
    from src.control.turn_queue import CapacityError

    async def _refuse(*_a: Any, **_k: Any) -> Any:
        raise CapacityError("full", retry_after=1)

    monkeypatch.setattr(env.orch, "submit_instruction", _refuse)
    refused = env.client.post("/api/instructions", headers=AUTH,
                              json={"description": "more", "session_id": SID})
    assert refused.status_code == 429
    after = {s["session_id"]: s for s in env.client.get("/api/sessions", headers=AUTH).json()["sessions"]}
    assert after[SID]["turn_queue"]["active_status"] == "running"
    assert after[SID]["status"] == listed[SID]["status"]


def test_S6_08b_unenrolled_sessions_carry_no_queue_block(env: _Env) -> None:
    env.db.upsert_session(Session(
        session_id="legacy", backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.BUSY, created_at=NOW, updated_at=NOW, machine_id="worker-a",
    ))
    listed = {s["session_id"]: s for s in env.client.get("/api/sessions", headers=AUTH).json()["sessions"]}
    assert listed["legacy"]["turn_queue"] is None and listed["legacy"]["status"] == "busy"


# S6-09 --------------------------------------------------------------------- #
def test_S6_09_queue_mutations_emit_post_commit_events_only_on_commit(env: _Env) -> None:
    a: str = env.create("a", "op-a").json()["turn_id"]
    b: str = env.create("b", "op-b").json()["turn_id"]
    env.events.clear()
    env.client.patch(f"/api/turn-requests/{b}", headers={**AUTH, "If-Match": "1"}, json={"body": "b2"})
    env.client.patch(f"/api/turn-requests/{b}", headers={**AUTH, "If-Match": "1"}, json={"body": "no"})
    env.client.post(f"/api/turn-requests/{b}/withdraw", headers={**AUTH, "If-Match": "2"})
    env.client.post(f"/api/sessions/{SID}/turn-requests/pause", headers=AUTH)
    env.client.post(f"/api/sessions/{SID}/turn-requests/resume", headers=AUTH)
    got: List[Dict[str, Any]] = env.queue_events()
    assert [(e.get("turn_id"), e.get("change")) for e in got] == [
        (b, "edited"), (b, "withdrawn"), (None, "paused"), (None, "resumed"),
    ]
    assert all(e["session_id"] == SID for e in got)
    assert all("task_id" not in e or e["task_id"] is None for e in got if e["change"] in ("paused", "resumed"))
    env.events.clear()
    assert env.schedule().activated == 1
    env.client.post(f"/api/sessions/{SID}/stop", headers=AUTH)
    changes: List[Tuple[Any, Any]] = [(e.get("turn_id"), e.get("change")) for e in env.queue_events()]
    assert (a, "activated") in changes and (None, "paused") in changes and (a, "cancelled") in changes


# S6-10 --------------------------------------------------------------------- #
def test_S6_10_carrier_transitions_emit_post_commit_events(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.control.node_registry as nr_mod
    import src.control.task_server as ts

    monkeypatch.setattr(ts, "get_db", lambda: env.db)
    monkeypatch.setattr(ts, "_worker_token", lambda: "wtok")
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    c = TestClient(ts.app)
    h: Dict[str, str] = {"Authorization": "Bearer wtok"}
    assert c.post("/nodes/register", headers=h, json={
        "node_id": "worker-a", "tailscale_ip": "127.0.0.1", "api_port": 0, "incarnation_id": "inc-1",
        "capabilities": {"backends": ["claude"], "queue_protocols": [0, 1], "managed_backends": ["claude"]},
    }).status_code == 200
    a: str = env.create("a", "op-a").json()["turn_id"]
    assert env.schedule().activated == 1
    env.events.clear()
    tok: str = c.post(f"/tasks/{a}/claim-managed", headers=h,
                      json={"node_id": "worker-a", "incarnation_id": "inc-1"}).json()["claim_token"]
    assert c.post(f"/tasks/{a}/start-managed", headers=h,
                  json={"node_id": "worker-a", "claim_token": tok, "incarnation_id": "inc-1"}).status_code == 200
    assert c.post(f"/tasks/{a}/result-managed", headers=h,
                  json={"node_id": "worker-a", "claim_token": tok, "success": True,
                        "output": "done"}).status_code == 200
    got: List[Tuple[Any, Any, Any]] = [(e.get("turn_id"), e.get("change"), e.get("status"))
                                       for e in env.queue_events()]
    assert got == [(a, "claimed", "claimed"), (a, "started", "running"), (a, "completed", "completed")]
    assert all(e["session_id"] == SID for e in env.queue_events())


# S6-11 --------------------------------------------------------------------- #
def test_S6_11_transcript_never_shows_waiting_or_withdrawn_prompt(env: _Env) -> None:
    from src.control.transcript import _turns_from_db

    a: str = env.create("running prompt", "op-a").json()["turn_id"]
    b: str = env.create("waiting prompt", "op-b").json()["turn_id"]
    c: str = env.create("withdrawn prompt", "op-c").json()["turn_id"]
    env.client.post(f"/api/turn-requests/{c}/withdraw", headers={**AUTH, "If-Match": "1"})
    assert env.schedule().activated == 1
    _claim_start(env.db, a)
    turns: List[Dict[str, Any]] = _turns_from_db(SID, 50) or []
    assert [t["task_id"] for t in turns] == [a], "a waiting/withdrawn prompt was shown as consumed"
    assert turns[0]["status"] == "running" and turns[0]["result"] == ""
    assert b not in {t["task_id"] for t in turns}


# S6-12 --------------------------------------------------------------------- #
@pytest.mark.parametrize("status,state", [
    ("queued", "queued"),
    ("withdrawn", "withdrawn"),
    ("recovery_required", "recovery_required"),
])
def test_S6_12_task_truth_knows_managed_states(status: str, state: str) -> None:
    from src.core.task_state_truth import derive_task_execution_state

    out = derive_task_execution_state({"id": "t", "status": status, "queue_protocol": 1,
                                       "blocked_reason": "why", "updated_at": NOW})
    assert out.state == state and out.confidence == "high"


def test_S6_12b_running_is_never_a_stale_claim() -> None:
    from src.core.task_state_truth import derive_task_execution_state

    out = derive_task_execution_state(
        {"id": "t", "status": "running", "queue_protocol": 1, "claimed_at": "2026-01-01T00:00:00",
         "claimer_incarnation": "inc-1"},
        node_row={"node_id": "n", "status": "online", "incarnation_id": "inc-1",
                  "last_heartbeat": NOW},
        now=datetime(2026, 10, 2, 12, 0, 5, tzinfo=timezone.utc),
    )
    assert out.state != "stale_claim" and out.state != "worker_unknown"


@pytest.mark.parametrize("status,expected", [
    ("queued", "queued"), ("running", "running"), ("withdrawn", "cancelled"),
    ("recovery_required", "connection_unknown"),
])
def test_S6_12c_task_lifecycle_maps_managed_states(status: str, expected: str) -> None:
    from src.core.task_lifecycle import derive_task_state

    assert derive_task_state(status) == expected
    assert derive_task_state(status, "awaiting_input") == (
        expected if expected == "cancelled" else "waiting_for_input")


def test_S6_12d_stale_busy_scan_counts_running_and_recovery_as_active(env: _Env) -> None:
    a: str = env.create("a", "op-a").json()["turn_id"]
    assert env.schedule().activated == 1
    tok: str = _claim_start(env.db, a)
    env.db._conn().execute("UPDATE sessions SET status = 'busy' WHERE session_id = ?", (SID,))
    env.db._conn().commit()
    assert SID not in {r["session_id"] for r in env.db.list_stale_busy_sessions()}
    env.db.enter_recovery(a, tok, reason="uncertain")
    assert SID not in {r["session_id"] for r in env.db.list_stale_busy_sessions()}


def test_S6_12e_stale_busy_repair_never_errors_a_managed_turn(env: _Env) -> None:
    a: str = env.create("a", "op-a").json()["turn_id"]
    assert env.schedule().activated == 1
    tok: str = _claim_start(env.db, a)
    sess = env.orch.session_store.get(SID)
    sess.status = SessionStatus.BUSY
    sess.last_task_id = a
    env.orch.session_store.save(sess)
    env.orch.running = True
    # running ⇒ not an orphan: untouched.
    assert asyncio.run(env.orch._reconcile_stale_busy_sessions_once()) == 0
    assert env.orch.session_store.get(SID).status == SessionStatus.BUSY
    assert env.db.get_task(a)["status"] == "running"
    env.db.complete_turn(task_id=a, claim_token=tok, result={"success": True, "output": "ok"},
                         status="completed")
    env.db._conn().execute("UPDATE sessions SET status = 'busy', last_task_id = ? WHERE session_id = ?",
                           (a, SID))
    env.db._conn().commit()
    # terminal managed ⇒ stale BUSY repaired to IDLE (never ERROR, no legacy replay).
    assert asyncio.run(env.orch._reconcile_stale_busy_sessions_once()) == 1
    assert env.orch.session_store.get(SID).status == SessionStatus.IDLE


# Stage 6 follow-ups ---------------------------------------------------------- #
# F1: an admission holding a reservation while the pass refreshes the shared
# allowance (idempotent replay / DB-side refusal) must not leave the managed
# cache stale-high while the scheduler sleeps until a hint that never comes.
def test_S6_F1_deferred_allowance_refresh_keeps_a_bounded_wake() -> None:
    a: ta.SharedWaitingAllowance = ta.SharedWaitingAllowance()
    a.register_legacy_probe(lambda: 0)
    assert a.refresh_managed(35, a.snapshot_generation())
    held: List[Any] = []

    class _FakeDB:
        def __init__(self) -> None:
            self.mid_flight: bool = True

        def select_eligible_turn_heads(self, limit: int) -> List[Dict[str, Any]]:
            return []

        def managed_waiting_totals(self) -> Dict[str, int]:
            if self.mid_flight:  # an admission reserves inside this pass's read
                cm = a.reserve(50)
                cm.__enter__()
                held.append(cm)
            return {"count": 0, "bytes": 0, "queued": 0}

    async def _prep(_h: Dict[str, Any], _r: Dict[str, Any]) -> Any:
        return None

    fake: _FakeDB = _FakeDB()
    res: sched.SchedulerPassResult = asyncio.run(sched.run_scheduler_pass(fake, _prep, allowance=a))
    # The in-flight admission then fails inside its DB txn (no row, no hint).
    try:
        held[0].__exit__(RuntimeError, RuntimeError("per-session cap"), None)
    except RuntimeError:
        pass
    assert res.refresh_deferred is True
    timeout = sched._next_timeout(res, 25, sched.SAFETY_NET_SEC, 3.0)
    assert timeout is not None and timeout <= sched.FALLBACK_INTERVAL_SEC
    fake.mid_flight = False
    res2: sched.SchedulerPassResult = asyncio.run(sched.run_scheduler_pass(fake, _prep, allowance=a))
    assert res2.refresh_deferred is False and a.managed_cached() == 0
    assert a.legacy_blocked(15, 50) is False
    assert sched._next_timeout(res2, 25, sched.SAFETY_NET_SEC, 3.0) is None  # idle again


# F3: the expiry withdrawal in the scheduler pass and a managed compaction
# admission are queue changes too — each emits ONE post-commit event.
def test_S6_F3a_expired_automation_withdrawal_emits_post_commit(env: _Env) -> None:
    past: str = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    t = env.db.enqueue_turn(session_id=SID, body="hb", operation_id="op-exp", turn_source="system",
                            expires_at=past, fleet_cap=1000)
    env.events.clear()
    res: sched.SchedulerPassResult = env.schedule()
    assert res.withdrawn == 1 and env.db.get_task(str(t))["status"] == "withdrawn"
    assert [(e.get("turn_id"), e.get("change"), e.get("status")) for e in env.queue_events()] == [
        (str(t), "withdrawn", "withdrawn"),
    ]


def test_S6_F3b_managed_compaction_admission_emits_post_commit(env: _Env) -> None:
    env.db._conn().execute("UPDATE sessions SET backend_session_id = 'b-1' WHERE session_id = ?", (SID,))
    env.db._conn().commit()
    env.events.clear()
    res = asyncio.run(env.orch.compact_session(SID, operation_id="k1"))
    tid: str = res.parsed_output["task_id"]
    assert [(e.get("turn_id"), e.get("change"), e.get("status")) for e in env.queue_events()] == [
        (tid, "admitted", "queued"),
    ]
    env.events.clear()
    asyncio.run(env.orch.compact_session(SID, operation_id="k1"))  # replay: no new queue state
    assert env.queue_events() == []
