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

if sys.argv[2:3] == ["generate-json-schema"]:
    # Offline capability probe (no model, no stdin): the real CLI writes the
    # app-server protocol schema bundle; this fake writes the two facts read.
    out = sys.argv[sys.argv.index("--out") + 1]
    c = ctl()
    os.makedirs(os.path.join(out, "v2"), exist_ok=True)
    methods = ["initialize", "thread/start", "turn/start"] + (
        [] if c.get("schema_no_thread_read") else ["thread/read"])
    with open(os.path.join(out, "ClientRequest.json"), "w") as fh:
        json.dump({"oneOf": [{"properties": {"method": {"enum": [m]}}} for m in methods]}, fh)
    props = {"threadId": {"type": "string"}}
    if not c.get("schema_no_client_id"):
        props["clientUserMessageId"] = {"type": ["string", "null"]}
    with open(os.path.join(out, "v2", "TurnStartParams.json"), "w") as fh:
        json.dump({"properties": props}, fh)
    sys.exit(0)

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
        beat = c.get("progress_every")  # held turn keeps streaming native events
        next_beat = time.monotonic()
        while not os.path.exists(hold):
            if threads[tid]["active"] != turn_id:
                return
            if beat and time.monotonic() >= next_beat:
                next_beat = time.monotonic() + beat
                note(tid, "item/started", turnId=turn_id,
                     item={"id": "r-%f" % next_beat, "type": "reasoning"})
            time.sleep(0.02)
    if c.get("die_mid_turn"):
        time.sleep(0.3)  # the turn/start response is consumed first
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
        if c.get("attach_delay"):
            time.sleep(c["attach_delay"])
        with lock:
            th = threads.setdefault(tid, {"active": None, "status": "idle"})
            status = c.get("attach_status") or ("active" if th["active"] else "idle")
        emit({"id": rid, "result": {"thread": {"id": tid, "cwd": p["cwd"], "status": {"type": status}}}})
    elif method == "thread/read":
        if c.get("read_delay"):
            time.sleep(c["read_delay"])
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
        if c.get("interrupt_delay"):
            time.sleep(c["interrupt_delay"])
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
    if not (home / "gateway-ownership.sqlite3").exists():
        return []
    conn = sqlite3.connect(home / "gateway-ownership.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM managed_turns")]
    except sqlite3.OperationalError:
        return []  # polled while the backend is still creating its schema
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
    assert h.backend.run_managed_turn(h.session(), "warm", own()).success  # app-server up
    monkeypatch.setattr(native_mod, "MANAGED_STALL_SECONDS", 0.5)
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


def test_working_turn_longer_than_the_stall_window_is_never_cut_off(h, monkeypatch):
    """A managed turn whose app-server keeps streaming events runs to its real
    result; only a turn with NO native event for the window goes to recovery."""
    assert h.backend.run_managed_turn(h.session(), "warm", own()).success  # app-server up
    monkeypatch.setattr(native_mod, "MANAGED_STALL_SECONDS", 0.6)
    h.ctl(hold=h.release_path, output="long answer", progress_every=0.2)
    out: dict = {}
    th = threading.Thread(target=lambda: out.update(
        r=h.backend.run_managed_turn(h.session(), "hello", own(turn_uuid="uuid-long"))), daemon=True)
    th.start()
    time.sleep(1.8)  # 3x the stall window
    assert out == {}, f"a progressing turn was given up on: {out}"
    h.release()
    th.join(10)
    assert out["r"].success is True and out["r"].output == "long answer", out["r"].errors
    assert h.requests("turn/interrupt") == []


def test_forget_drops_late_delivery_but_quiescence_follows_native_truth(h, monkeypatch):
    assert h.backend.run_managed_turn(h.session(), "warm", own()).success  # app-server up
    monkeypatch.setattr(native_mod, "MANAGED_STALL_SECONDS", 0.5)
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
    monkeypatch.setattr(native_mod, "MANAGED_STALL_SECONDS", 0.0)
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


# =========================================================================== #
# [A82 step 4 rework, review round 1] One slow RPC never kills the shared
# app-server (M1); refused-after-write-ahead is re-beginnable (m1); identity-
# less owners have a cutover exit (m2); late delivery keeps the session busy
# (m3); capability probe + systemError policy (m5); the sender token's only
# path is the JSON-RPC thread config (token audit).
# =========================================================================== #
def _held_neighbour(h) -> tuple[threading.Thread, dict, int]:
    """Session A holds a managed turn natively in flight on the shared app-server."""
    h.ctl(hold=h.release_path)
    th_a, box_a = run_bg(h.backend.run_managed_turn, h.session("sess-a"), "a",
                         own("sess-a", "uuid-a", task="t-a"))
    wait_for(lambda: any(r["turn_uuid"] == "uuid-a" and r["native_turn_id"] for r in managed_rows(h.home)))
    return th_a, box_a, h.backend._client.process.pid


def _neighbour_survives(h, th_a: threading.Thread, box_a: dict, pid: int) -> None:
    time.sleep(0.3)
    assert th_a.is_alive(), "A's in-flight turn was killed by a neighbour's slow RPC"
    client = h.backend._client
    assert client is not None and client.process.pid == pid and client.process.poll() is None
    assert client.failure == "", client.failure
    h.release()
    th_a.join(10)
    assert box_a["result"].success is True, box_a["result"].errors
    native_a = {r["turn_uuid"]: r["native_turn_id"] for r in managed_rows(h.home)}["uuid-a"]
    assert all(i["params"]["turnId"] != native_a for i in h.requests("turn/interrupt"))
    assert h.backend._client is client and client.process.pid == pid, "app-server was restarted"


def test_M1_slow_turn_start_of_one_session_never_kills_a_neighbour(h, monkeypatch):
    th_a, box_a, pid = _held_neighbour(h)
    monkeypatch.setattr(app_server_mod, "RPC_TIMEOUT", 0.3)
    h.ctl(hold=h.release_path, start_delay=1.5)
    res_b = h.backend.run_managed_turn(h.session("sess-b"), "b", own("sess-b", "uuid-b", task="t-b"))
    assert res_b.error_class == "recovery_required"
    assert h.backend.is_quiescent(h.session("sess-b")) is False, "B's prompt may still run: held"
    _neighbour_survives(h, th_a, box_a, pid)
    h.ctl()
    # B's hold resolves through its LATE turn/start reply (the native id of the
    # request that carried its clientUserMessageId) + thread/read status.
    wait_for(lambda: h.backend.is_quiescent(h.session("sess-b")))
    row_b = {r["turn_uuid"]: r for r in managed_rows(h.home)}["uuid-b"]
    assert row_b["native_turn_id"].startswith("turn-") and row_b["state"] == "stopped"
    assert owners(h.home) == []


def test_M1_slow_thread_read_in_quiescence_probe_never_kills_a_neighbour(h, monkeypatch):
    assert h.backend.run_managed_turn(h.session("sess-b"), "warm", own("sess-b", task="t-w")).success
    th_a, box_a, pid = _held_neighbour(h)
    monkeypatch.setattr(app_server_mod, "RPC_TIMEOUT", 0.3)
    h.ctl(hold=h.release_path, read_delay=1.0)
    assert h.backend.is_quiescent(h.session("sess-b")) is False, "unknown ⇒ busy, nothing else"
    _neighbour_survives(h, th_a, box_a, pid)
    h.ctl()
    wait_for(lambda: h.backend.is_quiescent(h.session("sess-b")))


def test_M1_slow_interrupt_never_kills_the_shared_app_server(h, monkeypatch):
    th_a, box_a, pid = _held_neighbour(h)
    th_b, box_b = run_bg(h.backend.run_managed_turn, h.session("sess-b"), "b",
                         own("sess-b", "uuid-b", task="t-b"))
    wait_for(lambda: len(managed_rows(h.home)) == 2 and all(r["native_turn_id"] for r in managed_rows(h.home)))
    monkeypatch.setattr(app_server_mod, "INTERRUPT_TIMEOUT", 0.3, raising=False)
    h.ctl(hold=h.release_path, interrupt_delay=5.5)  # beyond the legacy 5 s interrupt deadline
    assert h.backend.cancel_managed_turn(h.session("sess-b"), "uuid-b") is True
    th_b.join(15)
    assert box_b["result"].errors == ["cancelled"], "the interrupt is confirmed natively, late"
    _neighbour_survives(h, th_a, box_a, pid)


def test_M1_legacy_turn_start_deadline_never_kills_a_managed_neighbour(h, monkeypatch):
    th_a, box_a, pid = _held_neighbour(h)
    monkeypatch.setattr(app_server_mod, "RPC_TIMEOUT", 0.3)
    h.ctl(hold=h.release_path, start_delay=1.0)
    legacy = h.backend.resume_session(h.session("sess-legacy"), "x")
    assert legacy.success is False
    assert h.backend.is_quiescent(h.session("sess-legacy")) is False, "held, never blindly released"
    _neighbour_survives(h, th_a, box_a, pid)
    h.ctl()
    wait_for(lambda: h.backend.is_quiescent(h.session("sess-legacy")))
    assert owners(h.home) == []


def test_m1_turn_refused_after_write_ahead_is_rebeginnable(h):
    session = h.session()
    assert h.backend.cancel_managed_turn(session, "uuid-requeued") is True
    first = h.backend.run_managed_turn(session, "x", own(turn_uuid="uuid-requeued"))
    assert first.error_class == "managed_conflict"
    assert {r["turn_uuid"]: r["state"] for r in managed_rows(h.home)} == {"uuid-requeued": "not_submitted"}
    # The carrier requeues the provably-unsent attempt; the next claim runs it.
    again = h.backend.run_managed_turn(session, "x", own(turn_uuid="uuid-requeued"))
    assert again.success, again.errors
    assert len(h.requests("turn/start")) == 1
    assert {r["turn_uuid"]: r["state"] for r in managed_rows(h.home)} == {"uuid-requeued": "completed"}


def test_m2_cutover_sweep_clears_identityless_owner_only_when_its_process_is_gone(h):
    from src.backends.codex_ownership import sweep_legacy_owners

    legacy = CodexOwnership()
    legacy.acquire("sess-1", "thr-legacy", str(Path(h.repo).resolve()))
    managed = CodexOwnership()  # a MANAGED owner (recorded identity): never swept here
    managed.acquire("sess-2", "thr-managed", str(Path(h.repo).resolve()), process={"pid": 1 << 30})
    no_app_server = h.tmp_path / "proc"  # a fake, empty process table (no live app-server)
    no_app_server.mkdir()
    assert sweep_legacy_owners(proc_root=no_app_server) == [], "the owning process (this one) is alive"
    assert h.make().is_quiescent(h.session(native="thr-legacy")) is False
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    conn = sqlite3.connect(h.home / "gateway-ownership.sqlite3")
    with conn:
        conn.execute("UPDATE owners SET pid = ? WHERE owner = ?", (gone.pid, legacy.owner))
    conn.close()
    assert sweep_legacy_owners(proc_root=no_app_server) == [legacy.owner]
    remaining = {owner for _key, owner in owners(h.home)}
    assert legacy.owner not in remaining and remaining, "the managed owner is clear_dead_owners' job"
    assert h.make().is_quiescent(h.session(native="thr-legacy")) is True


def test_m3_session_stays_busy_until_late_reply_delivery_was_attempted(h, monkeypatch):
    assert h.backend.run_managed_turn(h.session(), "warm", own()).success
    monkeypatch.setattr(native_mod, "MANAGED_STALL_SECONDS", 0.5)
    in_sink, release_sink = threading.Event(), threading.Event()
    seen: list[bool] = []

    def sink(_sid: str, _outcome: Any) -> None:
        seen.append(h.backend.is_quiescent(h.session()))
        in_sink.set()
        release_sink.wait(5)

    h.backend.set_proactive_sink(sink)
    h.ctl(hold=h.release_path)
    result = h.backend.run_managed_turn(h.session(), "x", own(turn_uuid="uuid-late"))
    assert result.error_class == "recovery_required"
    h.release()
    assert in_sink.wait(10)
    assert seen == [False], "reconcile could resolve the turn before its late reply was captured"
    assert h.backend.is_quiescent(h.session()) is False
    release_sink.set()
    wait_for(lambda: h.backend.is_quiescent(h.session()))


@pytest.mark.parametrize("missing", ["schema_no_thread_read", "schema_no_client_id"])
def test_m5_capability_probe_refuses_an_app_server_without_the_managed_protocol(h, missing):
    from src.control.turn_queue import ManagedUnsupportedError

    h.ctl(**{missing: True})
    assert h.backend.supports_managed_turns() is False
    with pytest.raises(ManagedUnsupportedError):
        h.backend.run_managed_turn(h.session(), "x", own())
    assert h.requests() == [], "the probe is offline: no app-server protocol traffic"


def test_m5_capability_probe_is_cached_per_binary(h):
    assert h.backend.supports_managed_turns() is True
    h.ctl(schema_no_thread_read=True)  # would fail if probed again
    assert CodexBackend().supports_managed_turns() is True


@pytest.mark.parametrize("where", ["attach", "loaded"])
def test_m5_system_error_thread_is_submittable_on_attach_and_loaded_alike(h, where):
    if where == "loaded":
        warm = h.backend.run_managed_turn(h.session(), "warm", own())
        assert warm.success
        native = warm.backend_session_id
        h.ctl(read_status="systemError")
    else:
        native = ""
        h.ctl(attach_status="systemError")
    result = h.backend.run_managed_turn(h.session(native=native), "hello", own())
    assert result.success, result.errors


def test_token_audit_sender_token_only_travels_in_the_mcp_env_thread_config(h, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    token = "cap-" + uuid.uuid4().hex
    assert h.backend.provision_sender_capability("sess-1", token) is True
    result = h.backend.run_managed_turn(h.session(), "hello", own())
    assert result.success, result.errors
    carrying = []
    for request in h.requests():
        for name, value in (request["params"].get("config") or {}).items():
            if token in json.dumps(value):
                carrying.append((request["method"], name))
        stripped = {k: v for k, v in request["params"].items() if k != "config"}
        assert token not in json.dumps(stripped), f"token leaked into {request['method']} params"
    assert carrying == [("thread/start", "mcp_servers.ai_team_sender.env")]
    assert token not in caplog.text, "token reached a log record"
    assert token not in result.raw_stdout and token not in json.dumps(result.parsed_output)
    spy = Path(h.spy_path).resolve()
    for path in h.tmp_path.rglob("*"):
        if path.is_file() and path.resolve() != spy:
            assert token.encode() not in path.read_bytes(), f"capability persisted in {path}"


# =========================================================================== #
# [A82 pre-cutover backend carries] N2 — a pre-submit RPC deadline is a
# not-submitted conflict (requeue); N1 — a hung app-server never wedges a
# session (bounded late window + recycle; forget drops the hold + late id).
# =========================================================================== #
def test_N2_pre_submit_thread_start_timeout_is_not_submitted_and_requeues(h, monkeypatch):
    monkeypatch.setattr(app_server_mod, "RPC_TIMEOUT", 0.3)
    h.ctl(attach_delay=1.0)
    result = h.backend.run_managed_turn(h.session(), "x", own(turn_uuid="uuid-n2"))
    assert result.error_class == "managed_conflict", result.errors
    assert h.requests("turn/start") == []
    assert owners(h.home) == [], "nothing of ours runs: ownership released"
    client = h.backend._client
    assert client is not None and client.failure == "" and client.process.poll() is None
    h.ctl()
    wait_for(lambda: not client.late)  # the late thread/start reply drained (dropped)
    again = h.backend.run_managed_turn(h.session(), "x", own(turn_uuid="uuid-n2"))
    assert again.success, again.errors
    assert len(h.requests("turn/start")) == 1


def test_N1_hung_app_server_forget_drops_hold_and_late_id_and_recycle_unwedges(h, monkeypatch):
    """Inverts the reviewer probe ``test_probe_codex_hung.py``: ``turn/start``
    is never answered while the app-server stays alive."""
    monkeypatch.setattr(app_server_mod, "RPC_TIMEOUT", 0.3)
    monkeypatch.setattr(native_mod, "UNRESPONSIVE_AFTER_SEC", 1.0)
    h.ctl(start_delay=30)  # the fake's request loop is wedged: nothing is answered
    result = h.backend.run_managed_turn(h.session(), "x", own(turn_uuid="uuid-h"))
    assert result.error_class == "recovery_required"
    client = h.backend._client
    ident = process_identity(client.process.pid)
    assert h.backend.forget_managed_turn(h.session(), "uuid-h") is True
    assert h.backend._held == {}, "operator forget must drop the hold"
    assert client.late == {}, "operator forget must drop the late id's route"
    assert h.backend.is_quiescent(h.session()) is False, "the unanswered prompt may still be accepted"
    time.sleep(1.1)  # past the late-reply window: the app-server is unresponsive
    assert h.backend.is_quiescent(h.session()) is True, "hung app-server recycled; session not wedged"
    assert process_gone_proof(ident) is not None, "recycle = provable process death"
    assert owners(h.home) == []
    h.ctl()
    after = h.backend.run_managed_turn(h.session(), "y", own(turn_uuid="uuid-next"))
    assert after.success, after.errors
    assert h.backend._client is not client


def test_N1_held_turn_on_a_hung_app_server_resolves_by_recycle_without_forget(h, monkeypatch):
    monkeypatch.setattr(app_server_mod, "RPC_TIMEOUT", 0.3)
    monkeypatch.setattr(native_mod, "UNRESPONSIVE_AFTER_SEC", 1.0)
    h.ctl(start_delay=30)
    assert h.backend.run_managed_turn(h.session(), "x", own(turn_uuid="uuid-h2")).error_class \
        == "recovery_required"
    assert h.backend.is_quiescent(h.session()) is False
    time.sleep(1.1)
    assert h.backend.is_quiescent(h.session()) is True
    assert h.backend._held == {}
    assert {r["turn_uuid"]: r["state"] for r in managed_rows(h.home)}["uuid-h2"] == "stopped"


def test_N1_unresponsive_app_server_is_never_recycled_under_a_live_neighbour_turn(h, monkeypatch):
    th_a, box_a, pid = _held_neighbour(h)
    monkeypatch.setattr(app_server_mod, "RPC_TIMEOUT", 0.3)
    monkeypatch.setattr(native_mod, "UNRESPONSIVE_AFTER_SEC", 0.5)
    h.ctl(hold=h.release_path, start_delay=2.0)
    res_b = h.backend.run_managed_turn(h.session("sess-b"), "b", own("sess-b", "uuid-b", task="t-b"))
    assert res_b.error_class == "recovery_required"
    time.sleep(0.6)
    assert h.backend._client.unresponsive(0.5)
    assert h.backend.is_quiescent(h.session("sess-b")) is False
    _neighbour_survives(h, th_a, box_a, pid)
    h.ctl()
    wait_for(lambda: h.backend.is_quiescent(h.session("sess-b")))


def test_m2_quiescence_reason_names_an_identityless_legacy_owner(h):
    legacy = CodexOwnership()
    legacy.acquire("sess-1", "thr-legacy", str(Path(h.repo).resolve()))
    successor = h.make()
    session = h.session(native="thr-legacy")
    assert successor.quiescence_reason(session) is None
    assert successor.is_quiescent(session) is False
    assert successor.quiescence_reason(session) == "other_owner_not_provably_gone"
    legacy.release()
    assert successor.is_quiescent(session) is True
    assert successor.quiescence_reason(session) is None


# --------------------------------------------------------------------------- #
# [A82 pre-cutover rework, F1] forget never drops the cross-process fence
# --------------------------------------------------------------------------- #
def _forgotten_unanswered(h, monkeypatch, start_delay: float) -> tuple[CodexBackend, CodexBackend, str]:
    monkeypatch.setattr(app_server_mod, "RPC_TIMEOUT", 0.3)
    h.ctl(start_delay=start_delay)  # A's app-server answers turn/start late (or never)
    a = h.backend
    result = a.run_managed_turn(h.session(), "x", own(turn_uuid="uuid-a"))
    assert result.error_class == "recovery_required"
    tid = CodexOwnership().thread_for("sess-1") or result.backend_session_id
    b = h.make()  # successor incarnation / another carrier on the same CODEX_HOME
    assert b.is_quiescent(h.session(native=tid)) is False
    assert a.forget_managed_turn(h.session(), "uuid-a") is True
    return a, b, tid


def test_F1_forget_keeps_the_fence_while_the_submission_is_unanswered_on_a_live_app_server(h, monkeypatch):
    """Inverts the reviewer probe ``test_probe_n1_forget_fence.py``."""
    a, b, tid = _forgotten_unanswered(h, monkeypatch, start_delay=3)
    assert a._client.process.poll() is None
    assert owners(h.home) != [], "forget must not drop the owner rows of a still-unanswered submission"
    assert b.is_quiescent(h.session(native=tid)) is False
    assert b.quiescence_reason(h.session(native=tid)) == "other_owner_not_provably_gone"
    assert a.is_quiescent(h.session(native=tid)) is False
    # A's app-server answers and its turn completes natively: only then is the fence released.
    wait_for(lambda: a.is_quiescent(h.session(native=tid)), timeout=15)
    assert owners(h.home) == []
    assert b.is_quiescent(h.session(native=tid)) is True
    h.ctl()
    assert b.run_managed_turn(h.session(native=tid), "y", own(turn_uuid="uuid-b", task="t-2")).success
    starts = [(x["pid"], x["params"].get("threadId")) for x in h.requests("turn/start")]
    assert [s for s in starts if s[1] == tid and s[0] == a._client.process.pid] == [(a._client.process.pid, tid)]
    assert len([s for s in starts if s[1] == tid]) == 2, "B ran only after A's turn ended — never concurrently"


def test_F1_forget_keeps_the_fence_while_the_native_turn_runs(h, monkeypatch):
    assert h.backend.run_managed_turn(h.session(), "warm", own()).success
    monkeypatch.setattr(native_mod, "MANAGED_STALL_SECONDS", 0.5)
    h.ctl(hold=h.release_path)
    result = h.backend.run_managed_turn(h.session(), "hello", own(turn_uuid="uuid-run"))
    assert result.error_class == "recovery_required"
    tid = result.backend_session_id
    assert h.backend.forget_managed_turn(h.session(), "uuid-run") is True
    b = h.make()
    assert owners(h.home) != []
    assert b.is_quiescent(h.session(native=tid)) is False, "A's native turn still runs"
    h.release()
    wait_for(lambda: h.backend.is_quiescent(h.session(native=tid)))
    assert owners(h.home) == []
    assert b.is_quiescent(h.session(native=tid)) is True


def test_F1_successor_quiescent_once_the_forgotten_holders_app_server_is_recycled(h, monkeypatch):
    monkeypatch.setattr(native_mod, "UNRESPONSIVE_AFTER_SEC", 1.0)
    a, b, tid = _forgotten_unanswered(h, monkeypatch, start_delay=30)
    assert b.is_quiescent(h.session(native=tid)) is False
    time.sleep(1.1)
    assert a.is_quiescent(h.session(native=tid)) is True  # recycle = provable death
    assert owners(h.home) == []
    assert b.is_quiescent(h.session(native=tid)) is True


def test_F1_successor_quiescent_once_the_forgotten_holders_app_server_dies(h, monkeypatch):
    a, b, tid = _forgotten_unanswered(h, monkeypatch, start_delay=30)
    process = a._client.process
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)
    assert b.is_quiescent(h.session(native=tid)) is True, "process proof clears the dead owner"
    h.ctl()
    assert b.run_managed_turn(h.session(native=tid), "y", own(turn_uuid="uuid-b", task="t-2")).success
