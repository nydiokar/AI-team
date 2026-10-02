"""A84 (A82 Stage-8 prerequisite, final-gate F1) — managed-completion effects.

A managed (protocol-1) completion commits in the TASK-SERVER process
(``/tasks/{id}/result-managed`` → ``MeshDB.complete_turn``); the legacy
post-commit effects (notification, telemetry reconcile, transcript enrichment,
session summary/history, Case ``task.finished``) must still run exactly once, in
the GATEWAY, discovered through the DB — never through an in-process hint.

Separate-process shape: the task server and the gateway each hold their OWN
``MeshDB`` connection on the same file-backed SQLite DB; the completion goes
through the real task-server route (TestClient on ``task_server.app``) and the
consumer is the real gateway ``TaskOrchestrator`` drain. No CLI is spawned.

E01 Telegram-source turn: one notify (chat id), persisted usage/files/reply,
    driver state, task_history + summary, telemetry reconcile; idempotent re-drain
E02 web turn (no chat id): one notify (web push channel), effects done
E03 crash after the notify, before its mark ⇒ never re-sent (outcome unknown)
E04 crash before the notify fence ⇒ sent exactly once on the next pass
E05 failing notifier ⇒ bounded retries, visible ``failed``, other effects done
E06 failing idempotent effect ⇒ retried, notify still exactly once
E07 legacy (protocol-0) result route ⇒ no effects row, consumer does nothing
E08 never-started (withdrawn) turn ⇒ no effects; recovery resolution ⇒ effects
E09 Case child ⇒ exactly one ``task.finished`` (flow drive ON) across re-drains
E10 the pending-effects read is served by the partial index (no table scan)
E11 the consumer loop discovers a cross-process completion with no hint
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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
TOKEN: str = "a84-admin"
AUTH: Dict[str, str] = {"Authorization": f"Bearer {TOKEN}"}
WAUTH: Dict[str, str] = {"Authorization": "Bearer wtok"}
SID: str = "sess-a84"
CHAT: int = 4242


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


class _Notifier:
    """Records every ``notify_task_outcome`` call (the real dispatcher's seam)."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.fail: int = 0  # raise on the next N calls

    async def notify_task_outcome(self, task_id: str, result: Any, *, session: Any = None,
                                  chat_id: Optional[int] = None, prefix: str = "") -> None:
        self.calls.append({"task_id": task_id, "success": bool(result.success),
                           "output": result.output, "chat_id": chat_id,
                           "session_id": getattr(session, "session_id", None)})
        if self.fail:
            self.fail -= 1
            raise RuntimeError("notifier backend down")


class _Env:
    def __init__(self, gw: MeshDB, ts_db: MeshDB, orch: TaskOrchestrator,
                 api: TestClient, carrier: TestClient, notifier: _Notifier,
                 reconciled: List[str], summaries: List[str]) -> None:
        self.gw = gw
        self.ts_db = ts_db
        self.orch = orch
        self.api = api
        self.carrier = carrier
        self.notifier = notifier
        self.reconciled = reconciled
        self.summaries = summaries

    def create(self, body: str, op: str) -> str:
        r = self.api.post(f"/api/sessions/{SID}/turn-requests", headers=AUTH,
                          json={"body": body, "operation_id": op})
        assert r.status_code == 202, r.text
        return str(r.json()["turn_id"])

    def schedule(self) -> None:
        res = asyncio.run(sched.run_scheduler_pass(
            self.gw, self.orch._prepare_managed_turn, allowance=ta.SharedWaitingAllowance(),
        ))
        assert res.activated == 1

    def claim_start(self, tid: str) -> str:
        tok = self.carrier.post(f"/tasks/{tid}/claim-managed", headers=WAUTH,
                                json={"node_id": "worker-a", "incarnation_id": "inc-1"}).json()["claim_token"]
        assert self.carrier.post(f"/tasks/{tid}/start-managed", headers=WAUTH, json={
            "node_id": "worker-a", "claim_token": tok, "incarnation_id": "inc-1",
        }).status_code == 200
        return str(tok)

    def run_turn(self, body: str, op: str, **result: Any) -> str:
        tid = self.create(body, op)
        self.schedule()
        tok = self.claim_start(tid)
        envelope: Dict[str, Any] = {"node_id": "worker-a", "claim_token": tok, "success": True,
                                    "output": f"reply to {body}"}
        envelope.update(result)
        r = self.carrier.post(f"/tasks/{tid}/result-managed", headers=WAUTH, json=envelope)
        assert r.status_code == 200, r.text
        return tid

    def drain(self) -> int:
        return asyncio.run(self.orch._drain_managed_turn_effects_once(self.gw))

    def row(self, tid: str) -> Dict[str, Any]:
        return dict(self.gw._conn().execute("SELECT * FROM mesh_tasks WHERE id = ?", (tid,)).fetchone())

    def session_row(self) -> Dict[str, Any]:
        return dict(self.gw._conn().execute(
            "SELECT * FROM sessions WHERE session_id = ?", (SID,)).fetchone())


def _make_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, chat_id: Optional[int]) -> _Env:
    import src.control.node_registry as nr_mod
    import src.control.task_server as ts
    from src.control import telemetry_store
    from src.services.session_service import SessionService
    from src.services.session_store import SessionStore

    path = str(tmp_path / "mesh.db")
    gw: MeshDB = MeshDB(path)      # the gateway process's connection
    ts_db: MeshDB = MeshDB(path)   # the task-server process's connection
    monkeypatch.setattr(db_mod, "get_db", lambda: gw)
    monkeypatch.setattr(control_api, "_db", lambda: gw)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    monkeypatch.setattr(ts, "get_db", lambda: ts_db)
    monkeypatch.setattr(ts, "_worker_token", lambda: "wtok")
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    gw.upsert_session(Session(
        session_id=SID, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a",
        telegram_chat_id=chat_id,
    ))
    gw.enroll_session(SID)
    o: TaskOrchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
    o.session_store = SessionStore()
    o.session_service = SessionService(o.session_store, repo_path_validator=lambda _p: None)
    o.task_queue = SessionTaskQueue(50, lambda _t: "")
    o.active_tasks = {}
    o._compact_injected_ids = set()
    o._backends = {"claude": object()}
    o._emit_event = lambda name, task, data=None: None
    o._emit_turn_telemetry = lambda name, task, data=None, **k: None
    notifier = _Notifier()
    o.notifier = notifier
    summaries: List[str] = []
    o._write_session_summary = lambda session, result: summaries.append(session.session_id)
    o._append_session_event = lambda sid, tid, result: None
    reconciled: List[str] = []
    real_reconcile = telemetry_store.TelemetryStore.reconcile

    def _spy(self: Any, *, turn_id: Optional[str] = None, since_hours: float = 1.0) -> Dict[str, Any]:
        reconciled.append(str(turn_id))
        return real_reconcile(self, turn_id=turn_id, since_hours=since_hours)

    monkeypatch.setattr(telemetry_store.TelemetryStore, "reconcile", _spy)
    carrier = TestClient(ts.app)
    assert carrier.post("/nodes/register", headers=WAUTH, json={
        "node_id": "worker-a", "tailscale_ip": "127.0.0.1", "api_port": 0, "incarnation_id": "inc-1",
        "capabilities": {"backends": ["claude"], "queue_protocols": [0, 1], "managed_backends": ["claude"]},
    }).status_code == 200
    return _Env(gw, ts_db, o, TestClient(control_api.build_control_api(o)), carrier,
                notifier, reconciled, summaries)


@pytest.fixture()
def tg(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> _Env:
    return _make_env(tmp_path, monkeypatch, chat_id=CHAT)


@pytest.fixture()
def web(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> _Env:
    return _make_env(tmp_path, monkeypatch, chat_id=None)


# E01 ----------------------------------------------------------------------- #
def test_E01_telegram_turn_runs_every_effect_exactly_once(tg: _Env) -> None:
    usage: Dict[str, Any] = {"input_tokens": 11, "output_tokens": 7}
    tid = tg.run_turn(
        "hello", "op-1", files_modified=["a.py", "b.py"], usage=usage,
        telemetry_invocation_id="inv-1", driver_type="sdk", driver_status="live",
        cache_health="healthy", cache_unhealthy_count=0,
        previous_backend_session_ids=["old-1"], backend_session_id="native-1",
        execution_time=1.5, return_code=0,
    )
    row = tg.row(tid)
    # Persisted durably in the completion commit (before any consumer ran).
    assert row["effects_state"] == "pending"
    assert json.loads(row["usage_json"]) == usage
    assert json.loads(row["files_modified_json"]) == ["a.py", "b.py"]
    assert row["reply_text"] == "reply to hello"
    res = json.loads(row["result"])
    assert res["telemetry_invocation_id"] == "inv-1"
    assert res["previous_backend_session_ids"] == ["old-1"]
    assert res["cache_health"] == "healthy" and res["driver_status"] == "live"
    srow = tg.session_row()
    assert srow["driver_status"] == "live" and srow["cache_health"] == "healthy"
    assert json.loads(srow["previous_backend_session_ids"]) == ["old-1"]
    assert tg.gw._conn().execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0] == 1
    assert tg.notifier.calls == []  # nothing in the task-server process

    assert tg.drain() == 1
    assert tg.notifier.calls == [{"task_id": tid, "success": True, "output": "reply to hello",
                                  "chat_id": CHAT, "session_id": SID}]
    assert tg.reconciled == [tid]
    assert tg.summaries == [SID]
    srow = tg.session_row()
    history = json.loads(srow["task_history"])
    assert [h["task_id"] for h in history] == [tid]
    assert history[0]["result_summary"] == "reply to hello"
    assert history[0]["user_message"] == "hello"
    assert history[0]["files_modified"] == ["a.py", "b.py"]
    assert srow["last_result_summary"] == "reply to hello"
    assert json.loads(srow["last_files_modified"]) == ["a.py", "b.py"]
    assert tg.row(tid)["effects_state"] == "done"
    # Re-drain (another tick / a restart): nothing is repeated.
    assert tg.drain() == 0
    assert len(tg.notifier.calls) == 1 and tg.reconciled == [tid]
    assert len(json.loads(tg.session_row()["task_history"])) == 1


# E02 ----------------------------------------------------------------------- #
def test_E02_web_turn_notifies_once_without_a_chat(web: _Env) -> None:
    tid = web.run_turn("from the web", "op-w")
    assert web.drain() == 1
    assert [(c["task_id"], c["chat_id"], c["session_id"]) for c in web.notifier.calls] == [(tid, None, SID)]
    assert web.row(tid)["effects_state"] == "done"
    assert web.drain() == 0 and len(web.notifier.calls) == 1


# E03 ----------------------------------------------------------------------- #
def test_E03_crash_after_notify_before_mark_never_resends(tg: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = tg.run_turn("crashy", "op-c")
    real = tg.gw.transition_turn_effects

    def _crash(task_id: str, from_state: str, to_state: str, **kw: Any) -> bool:
        if from_state == "notifying":
            raise KeyboardInterrupt("gateway killed after the send")
        return real(task_id, from_state, to_state, **kw)

    monkeypatch.setattr(tg.gw, "transition_turn_effects", _crash)
    with pytest.raises(KeyboardInterrupt):
        tg.drain()
    assert len(tg.notifier.calls) == 1
    assert tg.row(tid)["effects_state"] == "notifying"
    monkeypatch.setattr(tg.gw, "transition_turn_effects", real)  # "restart"
    assert tg.drain() == 1
    assert tg.drain() == 0
    assert len(tg.notifier.calls) == 1  # the user never gets the reply twice
    row = tg.row(tid)
    assert row["effects_state"] == "done"
    assert "notify_outcome_unknown" in (row["effects_error"] or "")


# E04 ----------------------------------------------------------------------- #
def test_E04_crash_before_the_fence_sends_exactly_once(tg: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = tg.run_turn("early crash", "op-e")
    real = tg.gw.transition_turn_effects

    def _crash(task_id: str, from_state: str, to_state: str, **kw: Any) -> bool:
        if to_state == "notifying":
            raise KeyboardInterrupt("gateway killed before the send")
        return real(task_id, from_state, to_state, **kw)

    monkeypatch.setattr(tg.gw, "transition_turn_effects", _crash)
    with pytest.raises(KeyboardInterrupt):
        tg.drain()
    assert tg.notifier.calls == []
    monkeypatch.setattr(tg.gw, "transition_turn_effects", real)
    assert tg.drain() == 1 and tg.drain() == 0
    assert len(tg.notifier.calls) == 1
    assert len(json.loads(tg.session_row()["task_history"])) == 1  # idempotent re-run
    assert tg.row(tid)["effects_state"] == "done"


# E05 ----------------------------------------------------------------------- #
def test_E05_failing_notifier_bounded_retry_visible_failure(tg: _Env) -> None:
    from src.orchestrator import MANAGED_EFFECTS_MAX_ATTEMPTS

    tid = tg.run_turn("notifier down", "op-n")
    tg.notifier.fail = 10_000
    for _ in range(MANAGED_EFFECTS_MAX_ATTEMPTS + 3):
        tg.drain()
    assert len(tg.notifier.calls) == MANAGED_EFFECTS_MAX_ATTEMPTS
    row = tg.row(tid)
    assert row["effects_state"] == "failed"
    assert "notify_failed" in (row["effects_error"] or "")
    assert int(row["effects_attempts"]) == MANAGED_EFFECTS_MAX_ATTEMPTS
    # Every other effect still ran (once).
    assert [h["task_id"] for h in json.loads(tg.session_row()["task_history"])] == [tid]
    assert tid in tg.reconciled


def test_E05b_transient_notifier_failure_retries_then_delivers(tg: _Env) -> None:
    tid = tg.run_turn("blip", "op-b")
    tg.notifier.fail = 1
    tg.drain()
    assert tg.row(tid)["effects_state"] == "pending"
    tg.drain()
    assert len(tg.notifier.calls) == 2  # one failed raise, one delivery
    assert tg.row(tid)["effects_state"] == "done"
    assert tg.drain() == 0 and len(tg.notifier.calls) == 2


def test_E05c_hung_notifier_times_out_and_is_never_resent(
    tg: _Env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "MANAGED_EFFECTS_NOTIFY_TIMEOUT_SEC", 0.05)
    tid = tg.run_turn("hang", "op-h")
    real = tg.notifier.notify_task_outcome

    async def _hang(*a: Any, **k: Any) -> None:
        await real(*a, **k)
        await asyncio.sleep(30)

    tg.notifier.notify_task_outcome = _hang  # type: ignore[method-assign]
    assert tg.drain() == 1
    assert tg.drain() == 0
    assert len(tg.notifier.calls) == 1  # outcome unknown ⇒ never re-sent
    row = tg.row(tid)
    assert row["effects_state"] == "done"
    assert "notify_timeout" in (row["effects_error"] or "")


# E06 ----------------------------------------------------------------------- #
def test_E06_failing_idempotent_effect_retries_without_renotifying(
    tg: _Env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = tg.run_turn("db blip", "op-d")
    real = tg.gw.project_turn_session
    fails: List[int] = [1]

    def _flaky(*a: Any, **k: Any) -> bool:
        if fails[0]:
            fails[0] -= 1
            raise RuntimeError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(tg.gw, "project_turn_session", _flaky)
    tg.drain()
    assert len(tg.notifier.calls) == 1
    assert tg.row(tid)["effects_state"] == "notified"
    tg.drain()
    assert len(tg.notifier.calls) == 1
    assert tg.row(tid)["effects_state"] == "done"
    assert [h["task_id"] for h in json.loads(tg.session_row()["task_history"])] == [tid]


# E07 ----------------------------------------------------------------------- #
def test_E07_legacy_completion_is_untouched(tg: _Env) -> None:
    tg.gw.enqueue_task("legacy-1", None, None, "claude", "run_oneoff", {"prompt": "x"})
    tg.carrier.post("/tasks/legacy-1/claim", headers=WAUTH, json={"node_id": "worker-a"})
    r = tg.carrier.post("/tasks/legacy-1/result", headers=WAUTH,
                        json={"node_id": "worker-a", "success": True, "output": "legacy ok"})
    assert r.status_code == 200, r.text
    row = tg.row("legacy-1")
    assert row["status"] == "completed" and row["effects_state"] is None
    assert tg.drain() == 0 and tg.notifier.calls == []


# E08 ----------------------------------------------------------------------- #
def test_E08_never_started_turn_has_no_effects(tg: _Env) -> None:
    tid = tg.create("withdraw me", "op-x")
    rev = int(tg.row(tid)["revision"])
    tg.gw.withdraw_turn(tid, rev, actor="operator")
    assert tg.row(tid)["effects_state"] is None
    assert tg.drain() == 0 and tg.notifier.calls == []


def test_E08b_recovery_resolution_runs_effects_once(tg: _Env) -> None:
    tid = tg.create("stuck", "op-r")
    tg.schedule()
    tok = tg.claim_start(tid)
    tg.ts_db.enter_recovery(task_id=tid, claim_token=tok, reason="carrier lost")
    tg.ts_db.resolve_recovery(tid, tok, {"quiescent": True, "terminal": True, "task_id": tid},
                              resolved_status="failed")
    assert tg.row(tid)["effects_state"] == "pending"
    assert tg.drain() == 1 and tg.drain() == 0
    assert [(c["task_id"], c["success"]) for c in tg.notifier.calls] == [(tid, False)]
    assert tg.row(tid)["effects_state"] == "done"


# E09 ----------------------------------------------------------------------- #
def test_E09_case_child_gets_exactly_one_task_finished(tg: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    case_id = tg.gw.create_flow_run("a84-root", "queued")
    tid = tg.run_turn("case work", "op-k")
    tg.gw._conn().execute("UPDATE mesh_tasks SET flow_run_id = ? WHERE id = ?", (case_id, tid))
    tg.gw._conn().commit()
    tg.drain()
    tg.gw._conn().execute("UPDATE mesh_tasks SET effects_state = 'pending' WHERE id = ?", (tid,))
    tg.gw._conn().commit()
    tg.drain()  # a forced re-run must not duplicate the Case signal
    finished = [e for e in tg.gw.list_flow_events(case_id) if e["event_type"] == "task.finished"]
    assert [(e["entity_id"], json.loads(e["payload_json"])["outcome"]) for e in finished] == [(tid, "success")]


# E10 ----------------------------------------------------------------------- #
def test_E10_pending_effects_read_uses_the_partial_index(tg: _Env) -> None:
    plan = " ".join(str(r[-1]) for r in tg.gw._conn().execute(
        "EXPLAIN QUERY PLAN " + tg.gw._PENDING_EFFECTS_SQL, (25,)).fetchall())
    # A walk of the PARTIAL index only (it holds just the outstanding rows;
    # legacy/finished rows are never visited) and no sort step.
    assert "USING INDEX idx_mesh_tasks_turn_effects" in plan
    assert plan.count(" mesh_tasks ") == plan.count("USING INDEX idx_mesh_tasks_turn_effects")
    assert "TEMP B-TREE" not in plan


# E11 ----------------------------------------------------------------------- #
def test_E11_consumer_loop_discovers_cross_process_completion(
    tg: _Env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = tg.run_turn("looped", "op-l")

    async def _run() -> None:
        tg.orch.running = True
        task = asyncio.create_task(tg.orch._managed_effects_loop(0.02))
        for _ in range(200):
            if tg.notifier.calls:
                break
            await asyncio.sleep(0.01)
        tg.orch.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert [c["task_id"] for c in tg.notifier.calls] == [tid]
    assert tg.row(tid)["effects_state"] == "done"
