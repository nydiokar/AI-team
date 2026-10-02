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


def _age_fence(env: _Env, tid: str, seconds: float = 3600.0) -> None:
    """Simulate time passing since the notify fence was taken."""
    import time

    fence = str(env.row(tid)["effects_fence"] or "")
    token = fence.split(":", 1)[1] if ":" in fence else "x"
    env.gw._conn().execute("UPDATE mesh_tasks SET effects_fence = ? WHERE id = ?",
                           (f"{time.time() - seconds:.3f}:{token}", tid))
    env.gw._conn().commit()


def _driver(env: _Env) -> Tuple[Any, ...]:
    s = env.session_row()
    return (s["driver_type"], s["driver_status"], s["cache_health"],
            s["cache_unhealthy_count"], s["previous_backend_session_ids"])


REAL_DRIVER: Dict[str, Any] = {
    "driver_type": "sdk", "driver_status": "lost", "cache_health": "unhealthy",
    "cache_unhealthy_count": 2, "previous_backend_session_ids": ["old-1"],
    "backend_session_id": "native-1",
}


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
    assert tg.drain() == 0  # a fresh fence may be another consumer in flight
    _age_fence(tg, tid)     # ... until it outlives the notify bound
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
def test_E08_never_started_turn_gets_telemetry_only(tg: _Env) -> None:
    tid = tg.create("withdraw me", "op-x")
    rev = int(tg.row(tid)["revision"])
    tg.gw.withdraw_turn(tid, rev, actor="operator")
    assert tg.row(tid)["effects_state"] == "telemetry"
    assert tg.drain() == 1 and tg.drain() == 0
    assert tg.notifier.calls == [] and tg.summaries == []
    assert tg.reconciled == [tid]
    assert tg.row(tid)["effects_state"] == "done"


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


# ---------------------------------------------------------------------------
# Review round 1 (REWORK) regressions — reviewer probes P1-P3 + mig_probe
# inverted, plus F4-F6 and the telemetry gap.
# ---------------------------------------------------------------------------

def _finish(env: _Env, tid: str, **result: Any) -> None:
    env.schedule()
    tok = env.claim_start(tid)
    envelope: Dict[str, Any] = {"node_id": "worker-a", "claim_token": tok, "success": True,
                                "output": "ok"}
    envelope.update(result)
    r = env.carrier.post(f"/tasks/{tid}/result-managed", headers=WAUTH, json=envelope)
    assert r.status_code == 200, r.text


# F1 (P1) ------------------------------------------------------------------- #
def test_R1_default_envelope_never_wipes_driver_state(tg: _Env) -> None:
    tg.run_turn("a", "op-a", **REAL_DRIVER)
    before = _driver(tg)
    assert before == ("sdk", "lost", "unhealthy", 2, '["old-1"]')
    tg.run_turn("b", "op-b")  # carrier defaults: '', '', 'unknown', 0, []
    assert _driver(tg) == before


def test_R1b_compaction_envelope_never_wipes_driver_state(tg: _Env) -> None:
    tg.run_turn("a", "op-a", **REAL_DRIVER)
    before = _driver(tg)
    asyncio.run(tg.orch.compact_session(SID, operation_id="cmp-1"))
    [cid] = [r[0] for r in tg.gw._conn().execute(
        "SELECT id FROM mesh_tasks WHERE turn_kind = 'compaction'").fetchall()]
    _finish(tg, cid, output="compacted")
    assert tg.row(cid)["action"] == "compact_session"
    assert _driver(tg) == before


# F2 (P2) ------------------------------------------------------------------- #
def test_R2_stop_during_send_lets_the_send_finish_and_marks_it(tg: _Env) -> None:
    tid = tg.run_turn("a", "op-a")
    real = tg.notifier.notify_task_outcome

    async def _slow(*a: Any, **k: Any) -> None:
        await asyncio.sleep(0.3)
        await real(*a, **k)

    tg.notifier.notify_task_outcome = _slow  # type: ignore[method-assign]

    async def _stop() -> None:
        t = asyncio.create_task(tg.orch._run_managed_turn_effects(tg.gw, tid))
        await asyncio.sleep(0.1)
        t.cancel()  # gateway stop() cancels the consumer
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(_stop())
    assert len(tg.notifier.calls) == 1
    assert tg.row(tid)["effects_state"] == "notified"
    tg.notifier.notify_task_outcome = real  # type: ignore[method-assign]
    assert tg.drain() == 1
    assert len(tg.notifier.calls) == 1 and tg.row(tid)["effects_state"] == "done"


def test_R2b_stop_before_the_fence_keeps_the_row_pending(
    tg: _Env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = tg.run_turn("a", "op-a")
    real = tg.orch._apply_managed_turn_projections

    def _slow(*a: Any, **k: Any) -> List[str]:
        import time
        time.sleep(0.3)
        return real(*a, **k)

    monkeypatch.setattr(tg.orch, "_apply_managed_turn_projections", _slow)

    async def _stop() -> None:
        t = asyncio.create_task(tg.orch._run_managed_turn_effects(tg.gw, tid))
        await asyncio.sleep(0.1)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(_stop())
    assert tg.notifier.calls == [] and tg.row(tid)["effects_state"] == "pending"
    monkeypatch.setattr(tg.orch, "_apply_managed_turn_projections", real)
    assert tg.drain() == 1 and len(tg.notifier.calls) == 1


# F3 (P3) ------------------------------------------------------------------- #
def test_R3_second_consumer_never_steals_an_in_flight_fence(tg: _Env) -> None:
    tid = tg.run_turn("a", "op-a")
    real = tg.notifier.notify_task_outcome

    async def _slow(*a: Any, **k: Any) -> None:
        await real(*a, **k)
        await asyncio.sleep(0.5)

    tg.notifier.notify_task_outcome = _slow  # type: ignore[method-assign]

    async def _race() -> Tuple[bool, bool]:
        a = asyncio.create_task(tg.orch._run_managed_turn_effects(tg.gw, tid))
        await asyncio.sleep(0.2)
        b = await tg.orch._run_managed_turn_effects(tg.ts_db, tid)  # a 2nd gateway
        return await a, b

    ra, rb = asyncio.run(_race())
    assert (ra, rb) == (True, False)
    row = tg.row(tid)
    assert len(tg.notifier.calls) == 1
    assert row["effects_state"] == "done" and not row["effects_error"]


# F4 ------------------------------------------------------------------------ #
def test_R4_poisoned_rows_fail_visibly_and_never_starve_good_rows(tg: _Env) -> None:
    from src.orchestrator import MANAGED_EFFECTS_BATCH, MANAGED_EFFECTS_MAX_ATTEMPTS

    seed = tg.run_turn("seed", "op-s")
    assert tg.drain() == 1
    base = tg.row(seed)
    conn = tg.gw._conn()
    poisoned: List[str] = []
    for i in range(MANAGED_EFFECTS_BATCH + 1):
        r = dict(base, id=f"poison-{i:02d}", payload="{garbled", effects_state="pending",
                 effects_attempts=0, effects_error=None,
                 completed_at=f"2000-01-01T00:00:{i:02d}+00:00",
                 idempotency_key=f"poison-{i}", queue_sequence=None)
        cols = ", ".join(r)
        conn.execute(f"INSERT INTO mesh_tasks ({cols}) VALUES ({', '.join('?' * len(r))})",
                     list(r.values()))
        poisoned.append(r["id"])
    conn.commit()
    good = tg.run_turn("good", "op-g")
    for _ in range(MANAGED_EFFECTS_MAX_ATTEMPTS + 1):
        tg.drain()
    # The first batch of poisoned rows failed out of the index at the bound,
    # so the good row (completed last) was reached.
    assert [c["task_id"] for c in tg.notifier.calls] == [seed, good]
    for _ in range(MANAGED_EFFECTS_MAX_ATTEMPTS):
        tg.drain()
    assert tg.gw.pending_turn_effects(100) == []
    for pid in poisoned:
        row = tg.row(pid)
        assert row["effects_state"] == "failed", pid
        assert int(row["effects_attempts"]) == MANAGED_EFFECTS_MAX_ATTEMPTS
        assert row["effects_error"]


# F5 ------------------------------------------------------------------------ #
def test_R5_empty_output_notifies_the_stored_reply(tg: _Env) -> None:
    ndjson = json.dumps({"type": "result", "subtype": "success", "result": "the real answer"})
    tid = tg.run_turn("q", "op-q", output="", raw_stdout=ndjson)
    assert tg.row(tid)["reply_text"] == "the real answer"
    tg.drain()
    assert [c["output"] for c in tg.notifier.calls] == ["the real answer"]


# F6 ------------------------------------------------------------------------ #
def test_R6_compaction_never_notifies_nor_touches_history(tg: _Env) -> None:
    first = tg.run_turn("a", "op-a", **REAL_DRIVER)
    tg.drain()
    srow = tg.session_row()
    asyncio.run(tg.orch.compact_session(SID, operation_id="cmp-2"))
    [cid] = [r[0] for r in tg.gw._conn().execute(
        "SELECT id FROM mesh_tasks WHERE turn_kind = 'compaction'").fetchall()]
    _finish(tg, cid, output="compacted")
    assert tg.drain() == 1 and tg.drain() == 0
    assert [c["task_id"] for c in tg.notifier.calls] == [first]
    after = tg.session_row()
    assert [h["task_id"] for h in json.loads(after["task_history"])] == [first]
    assert after["last_result_summary"] == srow["last_result_summary"]
    assert tg.summaries == [SID]
    assert tg.row(cid)["effects_state"] == "done"
    assert cid in tg.reconciled


# Telemetry gap ------------------------------------------------------------- #
def _accept_telemetry(env: _Env, tid: str) -> None:
    from src.control.telemetry_store import TelemetryStore
    from src.core.telemetry import build_event

    TelemetryStore(env.gw).insert_events([build_event(
        "turn.accepted", turn_id=tid, session_id=SID, node_id="gw",
        emitter_process_instance_id="gw-1", source="gateway",
        attributes={"task_id": tid, "source": "web"},
    )])
    assert TelemetryStore(env.gw).get_turn(tid)["final_status"] == "running"


def test_R7_cancelled_managed_turn_closes_its_telemetry_turn(tg: _Env) -> None:
    from src.control.telemetry_store import TelemetryStore

    tid = tg.create("cancel me", "op-cc")
    _accept_telemetry(tg, tid)
    tg.schedule()
    tok = tg.claim_start(tid)
    tg.gw.request_turn_cancel(tid, actor="operator")
    r = tg.carrier.post(f"/tasks/{tid}/result-managed", headers=WAUTH, json={
        "node_id": "worker-a", "claim_token": tok, "success": False, "errors": ["interrupted"],
    })
    assert r.status_code == 200 and tg.row(tid)["status"] == "cancelled"
    tg.drain()
    assert TelemetryStore(tg.gw).get_turn(tid)["final_status"] == "cancelled"


def test_R7b_withdrawn_managed_turn_closes_its_telemetry_turn(tg: _Env) -> None:
    from src.control.telemetry_store import TelemetryStore

    tid = tg.create("withdraw me", "op-wt")
    _accept_telemetry(tg, tid)
    tg.gw.withdraw_turn(tid, int(tg.row(tid)["revision"]), actor="operator")
    tg.drain()
    assert TelemetryStore(tg.gw).get_turn(tid)["final_status"] == "cancelled"
    assert tg.notifier.calls == []


def test_R7c_legacy_cancelled_row_reconcile_semantics_unchanged(tg: _Env) -> None:
    from src.control.telemetry_store import TelemetryStore

    tg.gw.enqueue_task("legacy-c", None, None, "claude", "run_oneoff", {"prompt": "x"})
    _accept_telemetry(tg, "legacy-c")
    assert tg.gw.cancel_task("legacy-c", "test") is True
    out = TelemetryStore(tg.gw).reconcile(turn_id="legacy-c", since_hours=0)
    assert out["reconciled"] == []
    assert TelemetryStore(tg.gw).get_turn("legacy-c")["final_status"] == "running"


# mig_probe ----------------------------------------------------------------- #
def test_R8_migration_42_is_cheap_on_a_large_table_and_reads_stay_indexed(tmp_path: Any) -> None:
    import sqlite3
    import time

    from src.control.db import _get_migrations

    [sql] = [m for v, m in _get_migrations() if v == 42]
    c = sqlite3.connect(str(tmp_path / "big.db"))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE mesh_tasks(id TEXT PRIMARY KEY, status TEXT, completed_at TEXT, "
              "result TEXT, reply_text TEXT, payload TEXT)")
    blob = "x" * 1100
    c.executemany("INSERT INTO mesh_tasks VALUES (?, ?, ?, ?, ?, ?)", (
        (f"t{i}", "completed", f"2026-{i:09d}", blob, blob[:500], blob[:300]) for i in range(60000)))
    c.commit()
    t0 = time.monotonic()
    c.execute("BEGIN IMMEDIATE")
    for stmt in [x for x in sql.split(";") if x.strip()]:
        c.execute(stmt)
    c.execute("COMMIT")
    assert time.monotonic() - t0 < 5.0  # additive ALTERs + an empty partial index
    q = MeshDB._PENDING_EFFECTS_SQL
    plan = " ".join(str(r[-1]) for r in c.execute("EXPLAIN QUERY PLAN " + q, (25,)))
    assert "USING INDEX idx_mesh_tasks_turn_effects" in plan and "TEMP B-TREE" not in plan
    t1 = time.monotonic()
    assert c.execute(q, (25,)).fetchall() == []
    assert time.monotonic() - t1 < 0.05


# Telemetry reconcile cost ---------------------------------------------------- #
def test_R9_turn_scoped_reconcile_never_aggregates_all_llm_events(tg: _Env) -> None:
    """The consumer reconciles once per managed outcome (incl. never-ran
    turns): a turn-scoped reconcile must not materialise MAX(received_at)
    over the whole llm_events table."""
    from src.control.telemetry_store import TelemetryStore

    seen: List[str] = []
    conn = tg.gw._conn()
    conn.set_trace_callback(seen.append)
    try:
        TelemetryStore(tg.gw).reconcile(turn_id="t-x", since_hours=0)
    finally:
        conn.set_trace_callback(None)
    [sql] = [q for q in seen if "FROM llm_turns t" in q]
    plan = " ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
    assert "SCAN llm_events" not in plan, plan
