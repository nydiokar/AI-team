"""[A82 step 4a] Codex implements the managed-turn contract (``CodingBackend``).

The REAL ``CodexBackend`` + REAL ``CodexAppServerClient`` drive a fake
``codex app-server`` that speaks the real JSON-RPC protocol over stdio (shapes
verified against codex-cli 0.157.1's generated app-server schema and an
unauthenticated, isolated-CODEX_HOME probe). The fake can delay the
``turn/start`` response, emit foreign-turn events, report non-idle thread
status, die before the response or mid-turn, and hold a turn so a late result
arrives after the managed deadline. No model provider is reachable: the paid
codex CLI is never invoked.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import src.backends.codex_app_server as app_server_mod
import src.backends.codex_native as native_mod
from src.backends.codex_native import CodexBackend
from src.backends.codex_ownership import CodexOwnership
from src.control.turn_queue import ManagedTurnOwnership
from src.core.interfaces import Session, SessionStatus
from src.core.process_utils import process_gone_proof, process_identity

FAKE_APP_SERVER = r'''#!/usr/bin/env python3
"""Fake `codex app-server --stdio` (JSON-RPC lines). Control file is re-read
per request; the spy log records every request (test infra, outside CODEX_HOME)."""
import json, os, sys, threading, time, uuid
HOME = os.environ["CODEX_HOME"]
CTL = os.environ.get("FAKE_CODEX_CTL", "")
SPY = os.environ.get("FAKE_CODEX_SPY", "")
STATE = os.path.join(HOME, "fake_rollouts.json")
lock = threading.RLock()
threads = {}

def ctl():
    try:
        with open(CTL) as fh:
            return json.load(fh)
    except Exception:
        return {}

def spy(record):
    if SPY:
        with lock, open(SPY, "a") as fh:
            fh.write(json.dumps({"pid": os.getpid(), **record}) + "\n")

def emit(value):
    with lock:
        sys.stdout.write(json.dumps(value) + "\n")
        sys.stdout.flush()

def note(tid, method, **params):
    emit({"method": method, "params": {"threadId": tid, **params}})

def save(tid, turn):
    # Native history (rollout analogue): turn records only, never thread config.
    with lock:
        try:
            with open(STATE) as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        turns = data.setdefault(tid, [])
        turns[:] = [t for t in turns if t["id"] != turn["id"]] + [turn]
        with open(STATE, "w") as fh:
            json.dump(data, fh)

def complete(tid, turn_id, status, text=None):
    with lock:
        th = threads[tid]
        if th["active"] != turn_id:
            return
        if text:
            note(tid, "item/completed", turnId=turn_id,
                 item={"id": "msg-" + turn_id, "type": "agentMessage", "text": text})
        th["active"] = None
        th["status"] = "systemError" if status == "failed" else "idle"
        save(tid, {"id": turn_id, "status": status, "clientId": th.get("client")})
        note(tid, "turn/completed", turn={"id": turn_id, "items": [], "status": status,
             "error": {"message": "failed"} if status == "failed" else None})

def run_turn(tid, turn_id, c, text):
    hold = c.get("hold")
    if hold:
        while not os.path.exists(hold):
            if threads[tid]["active"] != turn_id:
                return
            time.sleep(0.02)
    if c.get("die_mid_turn"):
        os._exit(3)
    if c.get("foreign_event"):
        note(tid, "item/completed", turnId="turn-foreign",
             item={"id": "f", "type": "agentMessage", "text": "not yours"})
        return  # our turn stays active natively (never completed by itself)
    complete(tid, turn_id, c.get("final_status", "completed"), text)

for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    rid, method, p = req["id"], req["method"], req.get("params", {})
    c = ctl()
    spy({"method": method, "params": p})
    if method == "initialize":
        emit({"id": rid, "result": {"userAgent": "codex/fake"}})
    elif method in ("thread/start", "thread/resume"):
        tid = p.get("threadId") or ("thr-" + uuid.uuid4().hex[:10])
        with lock:
            th = threads.setdefault(tid, {"active": None, "status": "idle"})
            status = c.get("attach_status") or ("active" if th["active"] else "idle")
        emit({"id": rid, "result": {"thread": {"id": tid, "cwd": p["cwd"], "status": {"type": status}}}})
    elif method == "thread/read":
        if c.get("read_error"):
            emit({"id": rid, "error": {"code": -32000, "message": "read refused"}})
            continue
        with lock:
            th = threads.get(p["threadId"])
            status = c.get("read_status") or (
                "notLoaded" if th is None else ("active" if th["active"] else th["status"]))
        body = {"type": status, **({"activeFlags": []} if status == "active" else {})}
        emit({"id": rid, "result": {"thread": {"id": p["threadId"], "status": body}}})
    elif method == "turn/start":
        tid = p["threadId"]
        turn_id = "turn-" + uuid.uuid4().hex[:10]
        with lock:
            threads[tid]["active"] = turn_id
            threads[tid]["client"] = p.get("clientUserMessageId")
        save(tid, {"id": turn_id, "status": "inProgress", "clientId": p.get("clientUserMessageId")})
        if c.get("die_before_response"):
            os._exit(4)
        if c.get("start_delay"):
            time.sleep(c["start_delay"])
        emit({"id": rid, "result": {"turn": {"id": turn_id, "items": [], "status": "inProgress"}}})
        note(tid, "turn/started", turn={"id": turn_id, "items": [], "status": "inProgress"})
        note(tid, "item/started", turnId=turn_id, item={
            "id": "u-" + turn_id, "type": "userMessage",
            "clientId": c.get("client_id_override", p.get("clientUserMessageId")),
            "content": p["input"]})
        threading.Thread(target=run_turn, args=(tid, turn_id, c, c.get("output", "native answer")),
                         daemon=True).start()
    elif method == "turn/interrupt":
        tid = p["threadId"]
        if threads.get(tid, {}).get("active") != p["turnId"]:
            emit({"id": rid, "error": {"code": -32600, "message": "no such active turn"}})
            continue
        emit({"id": rid, "result": {}})
        complete(tid, p["turnId"], "interrupted")
    elif method == "thread/compact/start":
        tid = p["threadId"]
        turn_id = "cmp-" + uuid.uuid4().hex[:10]
        with lock:
            threads[tid]["active"] = turn_id
            threads[tid]["client"] = None
        emit({"id": rid, "result": {}})
        note(tid, "turn/started", turn={"id": turn_id, "items": [], "status": "inProgress"})
        threading.Thread(target=run_turn, args=(tid, turn_id, c, None), daemon=True).start()
    elif method == "thread/unsubscribe":
        emit({"id": rid, "result": {"status": "unsubscribed"}})
    else:
        emit({"id": rid, "error": {"code": -32601, "message": "unsupported " + method}})
'''


class Harness(SimpleNamespace):
    def ctl(self, **values: Any) -> None:
        Path(self.ctl_path).write_text(json.dumps(values))

    def requests(self, method: str | None = None) -> list[dict]:
        if not Path(self.spy_path).exists():
            return []
        rows = [json.loads(line) for line in Path(self.spy_path).read_text().splitlines() if line]
        return [r for r in rows if method is None or r["method"] == method]

    def release(self) -> None:
        Path(self.release_path).write_text("go")

    def session(self, sid: str = "sess-1", native: str = "") -> Session:
        return Session(session_id=sid, backend="codex", repo_path=self.repo, status=SessionStatus.IDLE,
                       created_at="2026-10-02T00:00:00", updated_at="2026-10-02T00:00:00",
                       backend_session_id=native)


@pytest.fixture
def h(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "codex"
    fake.write_text(FAKE_APP_SERVER.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    fake.chmod(0o700)
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "codexhome"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("FAKE_CODEX_CTL", str(tmp_path / "ctl.json"))
    monkeypatch.setenv("FAKE_CODEX_SPY", str(tmp_path / "spy.jsonl"))
    monkeypatch.setattr("src.core.test_guard.assert_live_calls_allowed", lambda _name: None)
    backends: list[CodexBackend] = []

    def make() -> CodexBackend:
        backend = CodexBackend()
        backends.append(backend)
        return backend

    harness = Harness(tmp_path=tmp_path, repo=str(repo), home=home, ctl_path=str(tmp_path / "ctl.json"),
                      spy_path=str(tmp_path / "spy.jsonl"), release_path=str(tmp_path / "release"),
                      make=make, backend=make())
    harness.ctl()
    yield harness
    harness.release()
    for backend in backends:
        backend.terminate_active_processes()


def own(sid: str = "sess-1", turn_uuid: str | None = None, task: str = "t-1") -> ManagedTurnOwnership:
    return ManagedTurnOwnership(task_id=task, session_id=sid, node_id="Horse", claim_token="tok",
                                incarnation_id="inc-1", turn_uuid=turn_uuid or uuid.uuid4().hex)


def run_bg(fn, *args, **kwargs) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def target() -> None:
        box["result"] = fn(*args, **kwargs)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def managed_rows(home: Path) -> list[dict]:
    conn = sqlite3.connect(home / "gateway-ownership.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM managed_turns")]
    finally:
        conn.close()


def owners(home: Path) -> list[tuple]:
    conn = sqlite3.connect(home / "gateway-ownership.sqlite3")
    try:
        return list(conn.execute("SELECT key, owner FROM owners"))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Capability + happy path: attribution by write-ahead mapping, identity first
# --------------------------------------------------------------------------- #
def test_capability_and_attributed_turn_with_process_identity_before_submit(h):
    assert h.backend.supports_managed_turns() is True
    seen: list[dict] = []

    def on_process(ident: dict) -> None:
        # Recorded BEFORE the prompt reaches the app-server.
        assert h.requests("turn/start") == []
        seen.append(dict(ident))

    ownership = own()
    result = h.backend.run_managed_turn(h.session(), "hello", ownership, on_process=on_process)
    assert result.success, result.errors
    assert result.output == "native answer"
    thread_id = result.backend_session_id
    assert thread_id.startswith("thr-")
    starts = h.requests("turn/start")
    assert len(starts) == 1 and starts[0]["params"]["clientUserMessageId"] == ownership.turn_uuid
    assert len(seen) == 1 and seen[0]["pid"] == starts[0]["pid"], "identity is the app-server's"
    rows = managed_rows(h.home)
    assert len(rows) == 1
    row = rows[0]
    assert (row["turn_uuid"], row["session_key"], row["thread_id"], row["state"]) == (
        ownership.turn_uuid, "sess-1", thread_id, "completed")
    assert row["native_turn_id"].startswith("turn-")
    assert owners(h.home) == [], "terminal outcome releases ownership"
    assert h.backend.is_quiescent(h.session()) is True


def test_unsupported_backend_without_executable_fails_closed(h, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-bin")
    assert CodexBackend().supports_managed_turns() is False


def test_mismatched_ownership_session_refused_before_anything_runs(h):
    from src.control.turn_queue import OwnershipConflictError

    with pytest.raises(OwnershipConflictError):
        h.backend.run_managed_turn(h.session("sess-1"), "hi", own("other"))
    assert h.requests("turn/start") == []


# --------------------------------------------------------------------------- #
# Never interrupt: conflict while a turn is in flight / thread not idle
# --------------------------------------------------------------------------- #
def test_in_flight_turn_refuses_second_managed_turn_without_interrupt(h):
    h.ctl(hold=h.release_path)
    first = own(turn_uuid="uuid-first")
    th, box = run_bg(h.backend.run_managed_turn, h.session(), "first", first)
    wait_for(lambda: len(h.requests("turn/start")) == 1)
    assert h.backend.is_quiescent(h.session()) is False
    second = h.backend.run_managed_turn(h.session(), "second", own(turn_uuid="uuid-second"))
    assert second.success is False and second.error_class == "managed_conflict"
    assert any("not_submitted" in e for e in second.errors)
    assert h.requests("turn/interrupt") == []
    assert len(h.requests("turn/start")) == 1
    h.release()
    th.join(10)
    assert box["result"].success is True
    states = {r["turn_uuid"]: r["state"] for r in managed_rows(h.home)}
    assert states == {"uuid-first": "completed"}, "the refused turn left no write-ahead row"


@pytest.mark.parametrize("where", ["attach", "loaded"])
def test_native_thread_not_idle_is_a_typed_conflict_before_submit(h, where):
    if where == "loaded":
        warm = h.backend.run_managed_turn(h.session(), "warm", own())
        assert warm.success
        native = warm.backend_session_id
        h.ctl(read_status="active")
    else:
        native = ""
        h.ctl(attach_status="active")
    result = h.backend.run_managed_turn(h.session(native=native), "hello", own())
    assert result.error_class == "managed_conflict"
    assert len(h.requests("turn/start")) == (1 if where == "loaded" else 0)
    assert h.requests("turn/interrupt") == []
    assert owners(h.home) == []


# --------------------------------------------------------------------------- #
# Unattributable outcomes ⇒ recovery_required (never interrupt, never success)
# --------------------------------------------------------------------------- #
def test_foreign_turn_event_is_recovery_required_and_never_interrupts(h):
    h.ctl(foreign_event=True)
    result = h.backend.run_managed_turn(h.session(), "hello", own(turn_uuid="uuid-f"))
    assert result.success is False and result.error_class == "recovery_required"
    assert h.requests("turn/interrupt") == []
    pid = h.requests("turn/start")[0]["pid"]
    assert process_gone_proof(process_identity(pid)) is None, "shared app-server not killed"
    # Native work is still active on our live app-server: held, not quiescent.
    assert h.backend.is_quiescent(h.session()) is False
    assert owners(h.home), "uncertainty retains ownership"
    # Once native status reports the thread idle, the hold is provably over.
    h.ctl(read_status="idle")
    assert h.backend.is_quiescent(h.session()) is True
    assert owners(h.home) == []


def test_wrong_client_id_echo_is_recovery_required(h):
    h.ctl(client_id_override="someone-else", hold=h.release_path)
    result = h.backend.run_managed_turn(h.session(), "hello", own(turn_uuid="uuid-mine"))
    assert result.error_class == "recovery_required"
    assert h.requests("turn/interrupt") == []


def test_delayed_turn_start_response_still_attributes_exactly(h):
    h.ctl(start_delay=0.6)
    ownership = own(turn_uuid="uuid-delay")
    result = h.backend.run_managed_turn(h.session(), "hello", ownership)
    assert result.success, result.errors
    row = managed_rows(h.home)[0]
    assert row["state"] == "completed" and row["native_turn_id"].startswith("turn-")


def test_turn_start_response_deadline_is_recovery_required(h, monkeypatch):
    monkeypatch.setattr(app_server_mod, "RPC_TIMEOUT", 0.3)
    h.ctl(start_delay=2)
    result = h.backend.run_managed_turn(h.session(), "hello", own(turn_uuid="uuid-late"))
    assert result.error_class == "recovery_required"
    row = managed_rows(h.home)[0]
    assert row["native_turn_id"] == "", "native id never learned"


def test_crash_between_submit_and_response_is_recovery_and_never_resubmitted(h):
    h.ctl(die_before_response=True)
    idents: list[dict] = []
    ownership = own(turn_uuid="uuid-crash")
    result = h.backend.run_managed_turn(h.session(), "hello", ownership, on_process=idents.append)
    assert result.error_class == "recovery_required"
    assert process_gone_proof(idents[0]) is not None, "app-server death is provable"
    row = managed_rows(h.home)[0]
    assert row["turn_uuid"] == "uuid-crash" and row["native_turn_id"] == ""
    assert row["state"] == "stopped"
    # The process that may have accepted the prompt is gone: quiescent.
    assert h.backend.is_quiescent(h.session()) is True
    # A replay of the SAME attempt is never blindly re-submitted.
    h.ctl()
    again = h.backend.run_managed_turn(h.session(), "hello", ownership)
    assert again.error_class == "recovery_required"
    assert len(h.requests("turn/start")) == 1


def test_app_server_death_mid_turn_is_attributable_failure_with_process_proof(h):
    h.ctl(die_mid_turn=True)
    idents: list[dict] = []
    result = h.backend.run_managed_turn(h.session(), "hello", own(turn_uuid="uuid-die"),
                                        on_process=idents.append)
    assert result.success is False and result.error_class != "recovery_required"
    assert "codex_runtime_lost" in " ".join(result.errors)
    assert process_gone_proof(idents[0]) is not None
    assert managed_rows(h.home)[0]["state"] == "stopped"
    assert h.backend.is_quiescent(h.session()) is True
    assert owners(h.home) == []


# --------------------------------------------------------------------------- #
# Deadline: no interrupt; late reply binds to the turn uuid only
# --------------------------------------------------------------------------- #
def test_deadline_holds_without_interrupt_and_late_result_binds_to_turn_uuid(h, monkeypatch):
    monkeypatch.setattr(native_mod, "MANAGED_TURN_SECONDS", 0.5)
    late: list[tuple[str, Any]] = []
    h.backend.set_proactive_sink(lambda sid, outcome: late.append((sid, outcome)))
    h.ctl(hold=h.release_path, output="late answer")
    result = h.backend.run_managed_turn(h.session(), "hello", own(turn_uuid="uuid-held"))
    assert result.error_class == "recovery_required"
    assert h.requests("turn/interrupt") == []
    assert h.backend.is_quiescent(h.session()) is False
    h.release()
    wait_for(lambda: late)
    sid, outcome = late[0]
    assert sid == "sess-1" and outcome.late_managed is True
    assert outcome.managed_turn_uuid == "uuid-held" and outcome.output == "late answer"
    assert outcome.backend_session_id.startswith("thr-")
    wait_for(lambda: h.backend.is_quiescent(h.session()))


def test_forget_drops_late_delivery_but_quiescence_follows_native_truth(h, monkeypatch):
    monkeypatch.setattr(native_mod, "MANAGED_TURN_SECONDS", 0.5)
    late: list = []
    h.backend.set_proactive_sink(lambda sid, outcome: late.append(outcome))
    h.ctl(hold=h.release_path)
    result = h.backend.run_managed_turn(h.session(), "hello", own(turn_uuid="uuid-forget"))
    assert result.error_class == "recovery_required"
    assert h.backend.forget_managed_turn(h.session(), "uuid-forget") is True
    assert h.backend.is_quiescent(h.session()) is False, "native work still runs"
    h.release()
    wait_for(lambda: h.backend.is_quiescent(h.session()))
    time.sleep(0.2)
    assert late == []
    assert h.backend.forget_managed_turn(h.session(), "uuid-unknown") is False


def test_deadline_before_submission_is_not_submitted(h, monkeypatch):
    monkeypatch.setattr(native_mod, "MANAGED_TURN_SECONDS", 0.0)
    gate = threading.Event()
    real = CodexOwnership.acquire

    def slow_acquire(self, *a, **k):
        gate.wait(5)
        return real(self, *a, **k)

    monkeypatch.setattr(CodexOwnership, "acquire", slow_acquire)
    result = h.backend.run_managed_turn(h.session(), "hello", own(turn_uuid="uuid-pre"))
    assert result.error_class == "managed_conflict"
    gate.set()
    wait_for(lambda: h.backend.is_quiescent(h.session()))
    assert h.requests("turn/start") == []


# --------------------------------------------------------------------------- #
# Cancel exactly one turn (incl. armed before start)
# --------------------------------------------------------------------------- #
def test_cancel_interrupts_exactly_that_turn(h):
    h.ctl(hold=h.release_path)
    a, b = own("sess-a", "uuid-a"), own("sess-b", "uuid-b")
    th_a, box_a = run_bg(h.backend.run_managed_turn, h.session("sess-a"), "a", a)
    th_b, box_b = run_bg(h.backend.run_managed_turn, h.session("sess-b"), "b", b)
    wait_for(lambda: len(h.requests("turn/start")) == 2)
    wait_for(lambda: all(r["native_turn_id"] for r in managed_rows(h.home)))
    native_a = {r["turn_uuid"]: r["native_turn_id"] for r in managed_rows(h.home)}["uuid-a"]
    assert h.backend.cancel_managed_turn(h.session("sess-a"), "uuid-a") is True
    th_a.join(10)
    interrupts = h.requests("turn/interrupt")
    assert [i["params"]["turnId"] for i in interrupts] == [native_a]
    assert box_a["result"].success is False and "cancelled" in box_a["result"].errors
    assert th_b.is_alive(), "the other session's turn is untouched"
    h.release()
    th_b.join(10)
    assert box_b["result"].success is True


def test_cancel_unknown_turn_never_cancels_another(h):
    h.ctl(hold=h.release_path)
    th, box = run_bg(h.backend.run_managed_turn, h.session(), "x", own(turn_uuid="uuid-live"))
    wait_for(lambda: len(h.requests("turn/start")) == 1)
    h.backend.cancel_managed_turn(h.session(), "uuid-other")
    time.sleep(0.3)
    assert h.requests("turn/interrupt") == []
    h.release()
    th.join(10)
    assert box["result"].success is True


def test_cancel_armed_before_start_never_submits(h):
    assert h.backend.cancel_managed_turn(h.session(), "uuid-armed") is True
    result = h.backend.run_managed_turn(h.session(), "x", own(turn_uuid="uuid-armed"))
    assert result.error_class == "managed_conflict"
    assert h.requests("turn/start") == []


def test_cancel_during_delayed_turn_start_interrupts_when_turn_id_known(h):
    h.ctl(start_delay=0.5, hold=h.release_path)
    th, box = run_bg(h.backend.run_managed_turn, h.session(), "x", own(turn_uuid="uuid-d"))
    wait_for(lambda: len(h.requests("turn/start")) == 1)
    assert h.backend.cancel_managed_turn(h.session(), "uuid-d") is True
    th.join(10)
    assert box["result"].errors == ["cancelled"]
    assert len(h.requests("turn/interrupt")) == 1


# --------------------------------------------------------------------------- #
# Managed compaction
# --------------------------------------------------------------------------- #
def test_managed_compaction_same_contract(h):
    warm = h.backend.run_managed_turn(h.session(), "warm", own())
    native = warm.backend_session_id
    ownership = own(turn_uuid="uuid-compact")
    result = h.backend.run_managed_compaction(h.session(native=native), ownership)
    assert result.success, result.errors
    assert result.backend_session_id == native
    row = {r["turn_uuid"]: r for r in managed_rows(h.home)}["uuid-compact"]
    assert row["kind"] == "compaction" and row["native_turn_id"].startswith("cmp-")
    assert row["state"] == "completed"
    h.ctl(read_status="active")
    refused = h.backend.run_managed_compaction(h.session(native=native), own())
    assert refused.error_class == "managed_conflict"
    assert len(h.requests("thread/compact/start")) == 1


# --------------------------------------------------------------------------- #
# Quiescence: unknown ⇒ busy
# --------------------------------------------------------------------------- #
def test_quiescence_unknown_native_status_is_busy(h):
    h.ctl(foreign_event=True)
    result = h.backend.run_managed_turn(h.session(), "hello", own())
    assert result.error_class == "recovery_required"
    h.ctl(read_error=True)
    assert h.backend.is_quiescent(h.session()) is False


# --------------------------------------------------------------------------- #
# Ownership interplay: successor carrier with / without proof
# --------------------------------------------------------------------------- #
def _crashed_owner(h, alive: bool) -> tuple[str, subprocess.Popen | None]:
    """A predecessor carrier's durable state left by a crash mid-turn: owners
    rows + its app-server identity + a started managed row (no TTL)."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    ident = process_identity(proc.pid)
    if not alive:
        proc.kill()
        proc.wait()
    previous = CodexOwnership()
    thread = "thr-prev"
    previous.acquire("sess-1", thread, str(Path(h.repo).resolve()), process=ident)
    previous.begin_managed("uuid-prev", thread, "turn", ident)
    previous.bind_native_turn("uuid-prev", "turn-prev")
    return thread, (proc if alive else None)


def test_successor_never_steals_a_live_owner(h):
    thread, proc = _crashed_owner(h, alive=True)
    try:
        successor = h.make()
        assert successor.is_quiescent(h.session(native=thread)) is False
        result = successor.run_managed_turn(h.session(native=thread), "hi", own())
        assert result.error_class == "managed_conflict"
        assert h.requests("turn/start") == []
        assert owners(h.home), "live owner untouched"
    finally:
        proc.kill()
        proc.wait()


def test_successor_with_process_proof_clears_no_ttl_owner(h):
    thread, _ = _crashed_owner(h, alive=False)
    successor = h.make()
    assert successor.is_quiescent(h.session(native=thread)) is True
    states = {r["turn_uuid"]: r["state"] for r in managed_rows(h.home)}
    assert states["uuid-prev"] == "stopped"
    result = successor.run_managed_turn(h.session(native=thread), "hi", own(turn_uuid="uuid-next"))
    assert result.success, result.errors
    assert result.backend_session_id == thread


def test_legacy_owner_without_identity_is_unknown_and_busy(h):
    legacy = CodexOwnership()
    legacy.acquire("sess-1", "thr-legacy", str(Path(h.repo).resolve()))
    successor = h.make()
    assert successor.is_quiescent(h.session(native="thr-legacy")) is False
    assert successor.run_managed_turn(h.session(native="thr-legacy"), "x", own()).error_class == "managed_conflict"


# --------------------------------------------------------------------------- #
# Sender capability: reaches the thread config, never disk
# --------------------------------------------------------------------------- #
def test_sender_capability_reaches_thread_config_and_never_disk(h):
    token = "cap-" + uuid.uuid4().hex
    assert h.backend.provision_sender_capability("sess-1", token) is True
    result = h.backend.run_managed_turn(h.session(), "hello", own())
    assert result.success, result.errors
    config = h.requests("thread/start")[0]["params"]["config"]
    assert config["mcp_servers.ai_team_sender.env"]["AI_TEAM_SENDER_CAPABILITY"] == token
    for path in h.home.rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), f"capability persisted in {path}"
