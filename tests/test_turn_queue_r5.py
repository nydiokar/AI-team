"""A82 Stage 3 round 5 (ACCEPT with minors): refused managed write ⇒ not
submitted; guards that kill the surviving mutants M1 (dead session with a late
handoff) and M3 (starved loop must not attest not-submitted). Offline only."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import src.control.task_server as ts
from src.backends.claude_driver import ClaudeSDKClientDriver
from tests.test_turn_queue_carrier_integration import (  # noqa: F401  (fixtures)
    _ClientHTTP, _row, _run_one, _seed_session_turn, _worker, db, real_claude,
)
from tests.test_turn_queue_carrier_recovery import _claims
from tests.test_turn_queue_r2 import _managed_in_thread, _wait_query
from tests.test_turn_queue_r4 import _END, _EOFClient
from tests.test_turn_queue_sdk_ownership import (
    _FakeClient, _emit_autonomous, _result, _start_fake_session,
)


class _DeadWriteClient(_FakeClient):
    async def query(self, message, session_id: str = "default") -> None:
        from claude_agent_sdk import CLIConnectionError

        raise CLIConnectionError("Cannot write to terminated process (exit code: 0)")


def test_R5_refused_managed_write_is_not_submitted_at_driver(monkeypatch):
    monkeypatch.setenv("WORKER_MANAGED_TURNS", "1")
    fake = _DeadWriteClient()
    sess = _start_fake_session(fake)
    drv = ClaudeSDKClientDriver()
    monkeypatch.setattr(ClaudeSDKClientDriver, "_get_or_create", lambda self, *a, **k: sess)
    session = SimpleNamespace(session_id="s1", repo_path="", backend_session_id="n", effort=None,
                              driver_type="", driver_status="")
    res = drv._run_turn(session, "m", model=None, effort=None, proc_env={}, _managed=True)
    assert res.success is False and res.error_class == "managed_conflict"
    assert "not_submitted" in res.errors[0]
    legacy = drv._run_turn(session, "m", model=None, effort=None, proc_env={})
    assert legacy.error_class == "transient"  # legacy mapping unchanged


def test_R5_refused_managed_write_returns_turn_to_pending(db, tmp_path, real_claude, monkeypatch):
    from claude_agent_sdk import CLIConnectionError

    async def dead_query(message, session_id="default"):
        raise CLIConnectionError("Cannot write to terminated process (exit code: 0)")

    monkeypatch.setattr(real_claude.fake, "query", dead_query)
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    w._backends = {"claude": real_claude.backend}
    _seed_session_turn(db, "t-w", "sess-w", "keep this prompt")
    _run_one(w, "t-w")
    row = _row(db, "t-w")
    assert row["status"] == "pending" and row["claim_token"] is None and row["error"] is None
    assert _claims(w) == []


def test_R5_M1_dead_session_with_pending_late_handoff_is_not_quiescent():
    """Kills mutant `return not self._late_handoffs` → `return True`."""
    fake = _EOFClient()
    fake.defer_echo = True
    sess = _start_fake_session(fake)
    release = threading.Event()
    sess._on_proactive = lambda k, o: release.wait(5)
    sess._turn_timeout_sec = lambda: 0.3
    try:
        out = _managed_in_thread(sess, "late")
        _wait_query(fake)
        out["t"].join(3)
        _emit_autonomous(sess, fake, fake.echo_for(), _result("LATE"))
        time.sleep(0.2)
        assert sess._late_handoffs == 1
        sess._loop.call_soon_threadsafe(fake.q.put_nowait, _END)  # CLI exits cleanly
        time.sleep(0.3)
        assert sess._reader_ended is True
        assert sess.is_quiescent() is False, "dead session reported quiescent mid late-handoff"
        drv = ClaudeSDKClientDriver()
        drv._sessions = {"s": sess}
        assert drv.is_session_quiescent("s") is False  # the reconciler cannot resolve in this window
        release.set()
        time.sleep(0.3)
        assert sess.is_quiescent() is True
    finally:
        release.set()
        sess.close()


def test_R5_M3_starved_loop_past_deadline_does_not_attest_not_submitted():
    """Kills mutant `if abandoned.wait(...) and pending is None` →
    `if pending is None`: the managed step is queued BEFORE the abandon step
    and the loop stalls past the deadline + abandon wait, so the caller cannot
    know whether the prompt will be sent — it must raise RecoveryRequiredError,
    never the not-submitted attestation (the prompt IS sent once the loop runs)."""
    from src.control.turn_queue import OwnershipConflictError, RecoveryRequiredError

    fake = _FakeClient()
    fake.defer_echo = True
    sess = _start_fake_session(fake)
    sess._turn_timeout_sec = lambda: 0.2
    sess._abandon_wait_sec = 0.3
    real_q = sess.is_quiescent

    def stalled_quiescence_check():
        # Runs INSIDE the managed step on the loop (already dequeued ahead of
        # the abandon callback): the loop stalls past deadline + abandon wait.
        time.sleep(1.2)
        return real_q()

    sess.is_quiescent = stalled_quiescence_check
    try:
        with pytest.raises(OwnershipConflictError) as ei:
            sess.send_managed("m")
        assert ei.value.code == "recovery_required", "attested not-submitted without knowing"
        assert isinstance(ei.value, RecoveryRequiredError)
        _wait_query(fake)  # once the loop resumes, the prompt IS written
        assert fake.queries_sent == ["m"]
    finally:
        sess.is_quiescent = real_q
        sess.close()
