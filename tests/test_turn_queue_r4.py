"""A82 Stage 3 rework 6 — A87 round-4 review: clean CLI exit (normal EOF) is a
dead session with live exits; operator resolution is a real exit; probe
refusal classes; not-submitted attestation; /proc parsing; late-capture
session guard. Offline only."""
import asyncio
import os
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import src.control.task_server as ts
import src.worker.agent as agent_mod
from src.backends.claude_driver import ClaudeSDKClientDriver, _SDKSession
from tests.test_turn_queue_carrier_integration import (  # noqa: F401  (fixtures)
    NODE, TOKEN, _ClientHTTP, _FakeBackend, _row, _run_one, _seed_session_turn, _seed_turn,
    _spooled, _worker, db, real_claude,
)
from tests.test_turn_queue_carrier_recovery import _claims, admin  # noqa: F401
from tests.test_turn_queue_r2 import _managed_in_thread, _wait_query
from tests.test_turn_queue_sdk_ownership import (
    _FakeClient, _assistant, _emit_autonomous, _result, _start_fake_session,
)

_END = object()


class _EOFClient(_FakeClient):
    """A CLI that exits 0: the SDK stream simply ends (no error frame)."""

    async def receive_messages(self):
        while True:
            m = await self.q.get()
            if m is _END:
                return
            yield m


def _driver_with(sess) -> ClaudeSDKClientDriver:
    drv = ClaudeSDKClientDriver()
    drv._sessions = {"s": sess}
    return drv


# =========================================================================== #
# MAJOR-1 — clean CLI exit
# =========================================================================== #
def test_R4_clean_exit_after_deadline_is_quiescent_dead_session():
    """Adopted probe (abandoned variant): after a clean CLI exit the session is
    dead ⇒ no pending, quiescent at driver and session level (no live backend
    work), so recovery can resolve and the pool will replace it."""
    fake = _EOFClient()
    fake.defer_echo = True
    sess = _start_fake_session(fake)
    sess._turn_timeout_sec = lambda: 0.3
    sess._on_proactive = lambda k, o: None
    try:
        out = _managed_in_thread(sess, "p")
        _wait_query(fake)
        out["t"].join(3)
        assert "e" in out  # deadline -> recovery
        sess._loop.call_soon_threadsafe(fake.q.put_nowait, _END)
        time.sleep(0.5)
        assert len(sess._pending) == 0 and sess._reader_ended is True
        assert _driver_with(sess).is_session_quiescent("s") is True
        assert sess.is_quiescent() is True
    finally:
        sess.close()


def test_R4_clean_exit_mid_turn_fails_caller_and_frees_session():
    """Adopted probe (in-flight variant): the managed caller gets a terminal
    stream-ended error (not a hang) and the session is quiescent (dead)."""
    from src.backends.claude_driver import SDKStreamEndedError

    fake = _EOFClient()
    fake.defer_echo = True
    sess = _start_fake_session(fake)
    sess._turn_timeout_sec = lambda: 5
    try:
        out = _managed_in_thread(sess, "p")
        _wait_query(fake)
        sess._loop.call_soon_threadsafe(fake.q.put_nowait, _END)
        out["t"].join(3)
        assert isinstance(out.get("e"), SDKStreamEndedError)
        assert sess.is_quiescent() is True
        assert _driver_with(sess).is_session_quiescent("s") is True
    finally:
        sess.close()


def test_R4_next_managed_turn_replaces_the_dead_session(monkeypatch):
    """After a clean exit, `_get_or_create` evicts the dead managed session and
    the next managed turn succeeds on a FRESH session."""
    monkeypatch.setenv("WORKER_MANAGED_TURNS", "1")
    fresh_fake = _FakeClient()
    fresh_fake.replies["second"] = [_assistant("a"), _result("SECOND OK")]
    started = []

    def fake_start(self):  # boot a fake-client loop instead of spawning a CLI
        booted = _start_fake_session(fresh_fake)
        self.__dict__.update({k: v for k, v in booted.__dict__.items() if k not in ("session_key",)})
        started.append(self)

    monkeypatch.setattr(_SDKSession, "start", fake_start)
    monkeypatch.setattr(ClaudeSDKClientDriver, "_role_boot", lambda self, s: (None, None))
    dead_fake = _EOFClient()
    dead = _start_fake_session(dead_fake)
    dead._loop.call_soon_threadsafe(dead_fake.q.put_nowait, _END)
    time.sleep(0.3)
    assert dead._reader_ended and not dead._closed
    drv = ClaudeSDKClientDriver()
    drv._sessions = {"sess-1": dead}
    session = SimpleNamespace(session_id="sess-1", repo_path="/tmp", backend_session_id="n", effort=None)
    try:
        new = drv._get_or_create(session, None, None, {})
        assert new is not dead and dead._closed is True and started == [new]
        assert new.send_managed("second").output == "SECOND OK"
    finally:
        for s in started:
            s.close()
        dead.close()


def test_R4_flag_off_session_with_ended_reader_is_not_evicted(monkeypatch):
    """Legacy byte-identical: with the managed flag OFF (no replay) a session
    whose reader ended is handled exactly as before (no new eviction)."""
    monkeypatch.delenv("WORKER_MANAGED_TURNS", raising=False)
    dead_fake = _EOFClient()
    dead = _start_fake_session(dead_fake)
    dead._replay_user_messages = False
    dead._loop.call_soon_threadsafe(dead_fake.q.put_nowait, _END)
    time.sleep(0.3)
    drv = ClaudeSDKClientDriver()
    drv._sessions = {"sess-1": dead}
    session = SimpleNamespace(session_id="sess-1", repo_path="/tmp", backend_session_id="n", effort=None)
    try:
        assert drv._get_or_create(session, None, None, {}) is dead and dead._closed is False
    finally:
        dead.close()


# =========================================================================== #
# MINOR-1 — operator resolve is a real exit (CLI alive, prompt never echoed)
# =========================================================================== #
def test_R4_operator_resolution_frees_session_for_next_managed_turn(db, tmp_path, real_claude, admin):
    real_claude.fake.defer_echo = True          # the CLI never begins our turn
    real_claude.sess._turn_timeout_sec = lambda: 0.3
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._backends = {"claude": real_claude.backend}
    _seed_session_turn(db, "t-1", "sess-1", "stuck prompt")
    real_claude.sess.session_key = "sess-1"
    _run_one(w, "t-1")
    assert _row(db, "t-1")["status"] == "recovery_required"
    assert real_claude.sess.is_quiescent() is False  # still owed by the (alive) CLI
    api, ah = admin
    assert api.post("/api/turn-requests/t-1/resolve-recovery",
                    json={"decision": "failed", "acknowledge_uncertain": True}, headers=ah).status_code == 200
    asyncio.run(w._reconcile_managed_claims())       # learns the row is terminal (409)
    assert _claims(w) == []
    assert real_claude.sess.is_quiescent() is True, "operator resolution did not free the session"
    # Next managed turn on the same session succeeds.
    real_claude.fake.defer_echo = False
    real_claude.fake.replies["next prompt"] = [_assistant("n", sid="n2"), _result("NEXT OK", sid="n2")]
    real_claude.sess._turn_timeout_sec = lambda: 5
    _seed_turn_for_session(db, "t-2", "sess-1", "next prompt")
    _run_one(w, "t-2")
    assert _row(db, "t-2")["status"] == "completed"


def _seed_turn_for_session(db, task_id, sid, prompt):
    db.enqueue_turn(
        task_id=task_id, session_id=sid, backend="claude", action="resume_session",
        payload={"task_id": task_id, "prompt": prompt,
                 "session": {"session_id": sid, "backend": "claude", "repo_path": "",
                             "backend_session_id": "native-prev"}},
        turn_source="human", turn_kind="instruction", machine_id=NODE,
    )
    db.activate_turn(task_id)


# =========================================================================== #
# MINOR-2 — only 409/404 are definitive for the held/recovery probe
# =========================================================================== #
@pytest.mark.parametrize("code,dropped", [(401, False), (403, False), (400, False), (404, True), (409, True)])
def test_R4_held_probe_refusal_classes(db, tmp_path, code, dropped):
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._claim_record("h-1", claim_token="tok-h", session_id="s", invoked=True, incarnation_id="old",
                    backend="claude", recovery_acked=True, backend_identity={"pid": os.getpid()})
    http.faults["/tasks/h-1/enter-recovery"] = [code]
    asyncio.run(w._reconcile_managed_claims())
    assert ("h-1" not in _claims(w)) is dropped


# =========================================================================== #
# MINOR-3 — deadline before submission ⇒ released as not-invoked (pending)
# =========================================================================== #
def test_R4_not_submitted_attestation_returns_turn_to_pending(db, tmp_path, real_claude, monkeypatch):
    sess = real_claude.sess
    real_orig = sess._submit_turn

    async def slow_submit(*a, **k):  # the loop only gets to it after the deadline
        await asyncio.sleep(0.6)
        return await real_orig(*a, **k)

    monkeypatch.setattr(sess, "_submit_turn", slow_submit)
    sess._turn_timeout_sec = lambda: 0.2
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._backends = {"claude": real_claude.backend}
    _seed_session_turn(db, "t-ns", "sess-ns", "keep me")
    _run_one(w, "t-ns")
    time.sleep(0.8)
    row = _row(db, "t-ns")
    assert row["status"] == "pending" and row["claim_token"] is None, row["status"]
    assert real_claude.fake.queries_sent == []
    assert _claims(w) == []


# =========================================================================== #
# MINOR-4 — /proc parsing with ')' in the name; late-capture session guard
# =========================================================================== #
def test_R4_proc_stat_parse_survives_paren_in_process_name(tmp_path):
    from src.core.process_utils import process_gone_proof, process_identity

    link = tmp_path / "evil) name"
    os.symlink("/bin/sleep", link)
    proc = subprocess.Popen([str(link), "30"])
    try:
        time.sleep(0.2)
        raw = open(f"/proc/{proc.pid}/stat").read()
        assert "evil) name" in raw
        expected = int(raw.rsplit(")", 1)[1].split()[19])
        ident = process_identity(proc.pid)
        assert ident.get("starttime_ticks") == expected
        assert process_gone_proof(ident) is None  # alive ⇒ no proof
    finally:
        proc.kill()
        proc.wait()
    assert process_gone_proof(ident) == {"pid": proc.pid, "observed": "absent"}


def test_R4_late_capture_refuses_session_mismatch(db, tmp_path):
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    w._claim_record("t-x", claim_token="tok-x", session_id="sess-A", invoked=True,
                    incarnation_id="i", backend="claude", turn_uuid="u-x")
    outcome = SimpleNamespace(late_managed=True, managed_turn_uuid="u-x", output="o", is_error=False,
                              error_text="", error_class="", backend_session_id="", raw_ndjson="")
    assert w._capture_late_managed_result("sess-B", outcome) is False
    assert _spooled(w) == []
    assert w._capture_late_managed_result("sess-A", outcome) is True
    assert _spooled(w) == ["t-x"]
