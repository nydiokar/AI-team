"""A82 Stage 4b — producer 2 on the CARRIER: operator cancel reaches exactly the
running managed turn, and managed compaction runs on the REAL Claude driver.

Real pieces: file-backed ``MeshDB``, the REAL task-server app (TestClient), the
REAL ``WorkerAgent`` (poll → claim → start → result spool → ``/result-managed``),
the REAL ``ClaudeCodeBackend`` / ``ClaudeSDKClientDriver`` / ``_SDKSession`` driven
by a fake claude_agent_sdk client, and (compaction E2E) the REAL gateway
admission + scheduler + preparation on a bare orchestrator. The base
``_SDKSession.start`` / ``ClaudeSDKClient.connect`` / subprocess spawn stay
guarded (they raise); fresh sessions boot a fake client via a subclass.
"""
import asyncio
import json
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, List

import pytest
from fastapi.testclient import TestClient

import src.control.task_server as ts
from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control import turn_scheduler as tsch
from src.core.interfaces import CodingBackend
from src.orchestrator import TaskOrchestrator
from tests.test_turn_queue_carrier_integration import (  # noqa: F401  (fixtures)
    NODE, _ClientHTTP, _row, _run_one, _seed_session_turn, _sess, _worker, db, real_claude,
)
from tests.test_turn_queue_carrier_recovery import _poll_once, _RecordingHTTP
from tests.test_turn_queue_sdk_ownership import (
    _FakeClient, _assistant, _emit_autonomous, _result,
)


@pytest.fixture(autouse=True)
def _no_cli_spawn(monkeypatch):
    from src.backends import claude_driver

    def _boom(*_a, **_k):
        raise AssertionError("real CLI spawn attempted in an offline test")

    monkeypatch.setattr(claude_driver._SDKSession, "start", _boom, raising=False)
    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk.ClaudeSDKClient, "connect", _boom, raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch):
    """The shared legacy+managed allowance is process-global: isolate it so
    this file's admissions never leak into other suites' capacity."""
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())

def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _interrupt_ends_turn(fake, text="interrupted", sid="n-x"):
    """Emulate the CLI: an interrupt ends the running turn with its own
    terminal (error) ResultMessage."""
    async def interrupt():
        fake.interrupts += 1
        fake.q.put_nowait(_result(text, sid=sid, is_error=True))
    fake.interrupt = interrupt


def _handle_control(w, action="cancel_managed"):
    async def scenario():
        rows = await w._fetch_pending()
        for r in [r for r in rows if r.get("action") == action]:
            await w._handle_task(r)
    asyncio.run(scenario())


def _gateway_cancel(task_id):
    o = TaskOrchestrator.__new__(TaskOrchestrator)
    return o._cancel_managed_turn_if_managed(task_id)


# --------------------------------------------------------------------------- #
# X — operator cancel on the carrier
# --------------------------------------------------------------------------- #
def test_X01_cancel_interrupts_exactly_the_running_managed_turn(db, tmp_path, real_claude):
    fake = real_claude.fake
    fake.replies["long job"] = [_assistant("working", sid="n-x")]  # no result: still running
    _interrupt_ends_turn(fake)
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._backends = {"claude": real_claude.backend}
    _seed_session_turn(db, "t-x", "sess-x", "long job")
    th = threading.Thread(target=_run_one, args=(w, "t-x"), daemon=True)
    th.start()
    assert _wait(lambda: fake.queries_sent and _row(db, "t-x")["status"] == "running")
    assert _gateway_cancel("t-x") is True
    _handle_control(w)
    th.join(5)
    assert not th.is_alive()
    assert fake.interrupts == 1
    row = _row(db, "t-x")
    assert row["status"] == "cancelled"
    [ctl] = [r for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE action='cancel_managed'").fetchall()]
    assert ctl["status"] == "completed" and "interrupt delivered" in (ctl["result"] or "")
    assert db.get_active_turn("sess-x") is None
    assert "t-x" not in w._managed_claims  # ownership released by the result exit
    # The control row is not a conversational turn: no session turn event.
    assert db._conn().execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (ctl["id"],)).fetchone()[0] == 0


def test_X02_cancel_before_the_turn_began_is_armed_for_its_echo(db, tmp_path, real_claude):
    fake = real_claude.fake
    fake.defer_echo = True
    fake.replies["queued in cli"] = []
    _interrupt_ends_turn(fake)
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._backends = {"claude": real_claude.backend}
    _seed_session_turn(db, "t-y", "sess-y", "queued in cli")
    th = threading.Thread(target=_run_one, args=(w, "t-y"), daemon=True)
    th.start()
    assert _wait(lambda: fake.queries_sent and _row(db, "t-y")["status"] == "running")
    assert _gateway_cancel("t-y") is True
    _handle_control(w)
    time.sleep(0.2)
    assert fake.interrupts == 0  # never interrupt a turn that is not ours
    assert _row(db, "t-y")["status"] == "running"
    _emit_autonomous(real_claude.sess, fake, fake.echo_for())  # our turn begins
    th.join(5)
    assert not th.is_alive() and fake.interrupts == 1
    assert _row(db, "t-y")["status"] == "cancelled"


def test_X03_cancel_for_an_attempt_this_carrier_does_not_hold_interrupts_nothing(db, tmp_path, real_claude):
    fake = real_claude.fake
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._backends = {"claude": real_claude.backend}
    _seed_session_turn(db, "t-z", "sess-z", "p")
    tok = db.claim_turn("t-z", NODE, "worker_daemon", "inc-old")
    db.start_turn("t-z", tok, incarnation_id="inc-old")
    assert _gateway_cancel("t-z") is True
    _handle_control(w)
    assert fake.interrupts == 0
    [ctl] = [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE action='cancel_managed'").fetchall()]
    assert ctl["status"] == "completed" and "no live managed attempt" in ctl["result"]
    assert _row(db, "t-z")["status"] == "running"  # the Stage-3 recovery exits own it


def test_X04_cancel_control_row_is_exempt_from_the_turn_capacity_gate(tmp_path):
    w = _worker(tmp_path, _RecordingHTTP(), managed=True)
    w.cfg.max_concurrent = 1
    rows = [{"id": "turn-new", "action": "resume_session"},
            {"id": "cancelm-1", "action": "cancel_managed", "session_id": "s"}]
    assert _poll_once(w, rows) == ["cancelm-1"]


def test_X05_cancel_managed_runs_outside_the_turn_slot(db, tmp_path):
    """Slots full (the running turn holds them): the cancel is still handled."""
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._semaphore = asyncio.Semaphore(0)  # no turn slot available at all
    _seed_session_turn(db, "t-s", "sess-s", "p")
    tok = db.claim_turn("t-s", NODE, "worker_daemon", "inc-1")
    db.start_turn("t-s", tok, incarnation_id="inc-1")
    _gateway_cancel("t-s")

    async def scenario():
        rows = await w._fetch_pending()
        [r] = [r for r in rows if r.get("action") == "cancel_managed"]
        await asyncio.wait_for(w._handle_task(r), 5)
    asyncio.run(scenario())
    [ctl] = [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE action='cancel_managed'").fetchall()]
    assert ctl["status"] == "completed"


# --------------------------------------------------------------------------- #
# K — managed compaction on the real driver
# --------------------------------------------------------------------------- #
class _LocalCmdFake(_FakeClient):
    """Emulates the bundled CLI (2.1.191) for a local slash command: `/compact`
    is NOT echoed back (no caller-uuid UserMessage); only its result arrives."""

    async def query(self, message, session_id: str = "default") -> None:
        if isinstance(message, str):
            text, uid = message, str(uuid.uuid4())
        else:
            text, uid = None, None
            async for m in message:
                text = m["message"]["content"]
                uid = m.get("uuid") or str(uuid.uuid4())
        self.queries_sent.append(text)
        self.query_uuids.append(uid)
        if text != "/compact" and self.echo and not self.defer_echo:
            self.q.put_nowait(__import__("claude_agent_sdk").UserMessage(content=text, uuid=uid))
        for m in self.replies.get(text, []):
            self.q.put_nowait(m)


@pytest.fixture()
def pooled_claude(monkeypatch):
    """A REAL ClaudeCodeBackend on the REAL SDK driver pool: every process the
    driver would spawn is a real `_SDKSession` whose `start` (subclass) boots a
    fake client instead of the CLI."""
    from src.backends import claude_driver as cd
    from src.backends.claude_code import ClaudeCodeBackend
    import src.core.test_guard as tg

    monkeypatch.setenv("WORKER_MANAGED_TURNS", "1")
    monkeypatch.setattr(tg, "assert_live_calls_allowed", lambda name: None)
    script: dict = {}
    booted: List[Any] = []
    base = cd._SDKSession

    class _Boot(base):
        def start(self) -> None:  # the base start stays guarded (raises)
            fake = _LocalCmdFake()
            fake.replies = script
            _interrupt_ends_turn(fake)
            self._client = fake
            self.fake = fake

            def run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop

                async def boot() -> None:
                    self._reader_task = asyncio.create_task(self._reader_loop())
                    self._ready.set()
                    while not self._closed:
                        await asyncio.sleep(0.02)
                    self._reader_task.cancel()
                    try:
                        await self._reader_task
                    except BaseException:  # noqa: BLE001
                        pass
                loop.run_until_complete(boot())
                loop.close()
            threading.Thread(target=run, daemon=True).start()
            assert self._ready.wait(3)
            booted.append(self)

    monkeypatch.setattr(cd, "_SDKSession", _Boot)
    # A mis-attributed turn must fail fast (deadline ⇒ recovery), not hang.
    monkeypatch.setattr(_Boot, "_turn_timeout_sec", lambda self: 3.0)
    backend = ClaudeCodeBackend("sdk")
    assert backend.supports_managed_turns()
    yield SimpleNamespace(backend=backend, script=script, booted=booted)
    for s in booted:
        s._closed = True


def _gateway(db):
    """Bare orchestrator with the REAL admission / scheduler preparation."""
    from src.core.session_task_queue import SessionTaskQueue
    from src.services.session_store import SessionStore

    o = TaskOrchestrator.__new__(TaskOrchestrator)
    o.session_store = SessionStore()
    o.task_queue = SessionTaskQueue(50, lambda _t: "")
    o.active_tasks = {}
    o._compact_injected_ids = set()
    o._backends = {"claude": object()}
    return o


def test_K04_compaction_end_to_end_retires_quiescent_process_and_resumes(db, tmp_path, pooled_claude):
    pooled_claude.script["first"] = [_assistant("hi", sid="native-1"), _result("done", sid="native-1")]
    pooled_claude.script["/compact"] = [_result("", sid="native-2")]
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._backends = {"claude": pooled_claude.backend}
    _seed_session_turn(db, "t-1", "sess-k", "first")
    _run_one(w, "t-1")
    assert _row(db, "t-1")["status"] == "completed"
    assert _sess(db, "sess-k")["backend_session_id"] == "native-1"
    [first] = pooled_claude.booted

    o = _gateway(db)
    acc = asyncio.run(o.compact_session("sess-k"))
    kid = acc.parsed_output["task_id"]
    asyncio.run(tsch.run_scheduler_pass(db, o._prepare_managed_turn,
                                        allowance=ta.SharedWaitingAllowance()))
    assert _row(db, kid)["status"] == "pending"
    _run_one(w, kid)

    assert len(pooled_claude.booted) == 2
    second = pooled_claude.booted[1]
    assert first._closed and first.fake.interrupts == 0  # retired, never interrupted
    assert second.resume == "native-1"  # the fresh process continues the conversation
    assert second.fake.queries_sent == ["/compact"]
    row = _row(db, kid)
    assert row["status"] == "completed", row
    assert _sess(db, "sess-k")["backend_session_id"] == "native-2"
    posted = [c[1] for c in http.calls if c[0] == "POST"]
    assert f"/tasks/{kid}/enter-recovery" not in posted


def test_K04b_compaction_after_carrier_restart_resumes_the_native_session(pooled_claude):
    """No pooled process (carrier restarted): the fresh process compaction runs
    on must RESUME the session's native conversation, not start an empty one."""
    from src.core.interfaces import Session, SessionStatus

    pooled_claude.script["/compact"] = [_result("", sid="native-9")]
    s = Session(session_id="sess-r", backend="claude", repo_path="", status=SessionStatus.IDLE,
                created_at="t", updated_at="t", backend_session_id="native-8")
    own = tq.ManagedTurnOwnership(task_id="k", session_id="sess-r", node_id=NODE,
                                  claim_token="tok", incarnation_id="inc", turn_uuid=str(uuid.uuid4()))
    res = pooled_claude.backend.run_managed_compaction(s, own)
    assert res.success and res.backend_session_id == "native-9"
    [fresh] = pooled_claude.booted
    assert fresh.resume == "native-8" and fresh.fake.queries_sent == ["/compact"]


def test_K05_non_quiescent_process_refuses_compaction_without_interrupt(pooled_claude):
    from src.core.interfaces import Session, SessionStatus

    pooled_claude.script["first"] = [_assistant("hi", sid="native-1"), _result("done", sid="native-1")]
    backend = pooled_claude.backend
    s = Session(session_id="sess-q", backend="claude", repo_path="", status=SessionStatus.IDLE,
                created_at="t", updated_at="t", backend_session_id="native-1")
    own = tq.ManagedTurnOwnership(task_id="a", session_id="sess-q", node_id=NODE,
                                  claim_token="tok", incarnation_id="inc", turn_uuid=str(uuid.uuid4()))
    assert backend.run_managed_turn(s, "first", own).success
    [first] = pooled_claude.booted
    first._bg_task_status["bg-1"] = "running"  # native background work in flight
    own2 = tq.ManagedTurnOwnership(task_id="k", session_id="sess-q", node_id=NODE,
                                   claim_token="tok2", incarnation_id="inc", turn_uuid=str(uuid.uuid4()))
    res = backend.run_managed_compaction(s, own2)
    assert not res.success and res.error_class == "managed_conflict"
    assert not first._closed and first.fake.interrupts == 0
    assert len(pooled_claude.booted) == 1 and "/compact" not in first.fake.queries_sent


def test_K05b_used_process_cannot_take_an_unechoed_local_command(pooled_claude):
    """The fresh-process guard itself: a local command on a process that ran a
    query is refused before anything is written."""
    from src.core.interfaces import Session, SessionStatus

    pooled_claude.script["first"] = [_assistant("hi", sid="native-1"), _result("done", sid="native-1")]
    backend = pooled_claude.backend
    s = Session(session_id="sess-g", backend="claude", repo_path="", status=SessionStatus.IDLE,
                created_at="t", updated_at="t", backend_session_id="native-1")
    own = tq.ManagedTurnOwnership(task_id="a", session_id="sess-g", node_id=NODE,
                                  claim_token="tok", incarnation_id="inc", turn_uuid=str(uuid.uuid4()))
    assert backend.run_managed_turn(s, "first", own).success
    [first] = pooled_claude.booted
    with pytest.raises(tq.OwnershipConflictError):
        first.send_managed("/compact", local_command=True)
    assert first.fake.queries_sent == ["first"]


def test_K06_backends_without_a_managed_compaction_path_fail_closed():
    from src.backends.codex_native import CodexBackend
    from src.backends.opencode import OpenCodeBackend, OpenCodeServerBackend

    for cls in (CodexBackend, OpenCodeBackend, OpenCodeServerBackend):
        assert cls.run_managed_compaction is CodingBackend.run_managed_compaction
        assert cls.cancel_managed_turn is CodingBackend.cancel_managed_turn
    fake = SimpleNamespace()
    with pytest.raises(tq.ManagedUnsupportedError):
        CodingBackend.run_managed_compaction(fake, None, None)
    assert CodingBackend.cancel_managed_turn(fake, None, "u") is False


# --------------------------------------------------------------------------- #
# Stage 4b rework — MAJOR 1: cancel in the boot window is never lost
# --------------------------------------------------------------------------- #
def test_R01_cancel_in_boot_window_is_armed_and_the_turn_never_runs(db, tmp_path, real_claude):
    """Adopted reviewer probe P1 (inverted): the cancel lands after
    /start-managed (row running, turn uuid recorded) but before the driver
    registered the prompt (CLI boot). It is armed by uuid; the prompt is never
    submitted; the attempt is released not-invoked ⇒ `cancelled`."""
    fake = real_claude.fake
    fake.replies["slow boot"] = [_assistant("work", sid="n-p"), _result("real output", sid="n-p")]
    _interrupt_ends_turn(fake, sid="n-p")
    gate = threading.Event()
    inner = real_claude.backend.run_managed_turn

    def gated(*a, **k):
        assert gate.wait(10)
        return inner(*a, **k)

    real_claude.backend.run_managed_turn = gated
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    w._backends = {"claude": real_claude.backend}
    _seed_session_turn(db, "t-p", "sess-p", "slow boot")
    th = threading.Thread(target=_run_one, args=(w, "t-p"), daemon=True)
    th.start()
    assert _wait(lambda: _row(db, "t-p")["status"] == "running"
                 and (w._managed_claims.get("t-p") or {}).get("turn_uuid"))
    assert _gateway_cancel("t-p") is True
    _handle_control(w)
    gate.set()
    th.join(10)
    assert not th.is_alive()
    row = _row(db, "t-p")
    assert row["status"] == "cancelled", row["status"]
    assert "slow boot" not in fake.queries_sent and fake.interrupts == 0  # never ran
    assert db.get_active_turn("sess-p") is None and "t-p" not in w._managed_claims


def test_R02_cancel_before_the_turn_uuid_exists_is_caught_pre_invoke(db, tmp_path, real_claude):
    """The cancel is handled between /start-managed and the carrier recording
    the turn uuid (nothing to arm yet): the attempt carries the cancel and the
    pre-invoke check releases it not-invoked ⇒ `cancelled`, backend never called."""
    fake = real_claude.fake
    fake.replies["p"] = [_result("should not run")]
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    w._backends = {"claude": real_claude.backend}
    _seed_session_turn(db, "t-q", "sess-q", "p")
    real_start = w._claim_and_start_managed

    async def start_then_cancel(task_id):
        out = await real_start(task_id)
        assert not (w._managed_claims.get(task_id) or {}).get("turn_uuid")
        assert _gateway_cancel(task_id) is True
        rows = await w._fetch_pending()
        [ctl] = [r for r in rows if r.get("action") == "cancel_managed"]
        await w._handle_cancel_managed(ctl)
        return out
    w._claim_and_start_managed = start_then_cancel
    _run_one(w, "t-q")
    assert _row(db, "t-q")["status"] == "cancelled"
    assert real_claude.calls["run_managed_turn"] == 0 and fake.queries_sent == []
