"""A82 Stage 3 rework 4 — A87 round-2 adversarial review.

Adopts the round-2 probes (D1/D1b/D2 driver, S1/S2/S3 server/worker) as
permanent tests asserting the CORRECT behavior, plus the echo-correlation,
process-proof, dead-letter/cursor, poll-loop-survival and mutation-guard tests
the review required. Offline only (fake SDK client, TestClient, temp SQLite).
"""
import asyncio
import json
import sys
import threading
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import src.control.task_server as ts
import src.worker.agent as agent_mod
from src.backends.claude_driver import _SDKSession
from tests.test_turn_queue_carrier_integration import (  # noqa: F401  (fixtures)
    NODE, TOKEN, _ClientHTTP, _FakeBackend, _RecordingHTTP, _row, _run_one,
    _seed_turn, _spooled, _worker, _register_node, db,
)
from tests.test_turn_queue_carrier_recovery import fake_psutil
from tests.test_turn_queue_sdk_ownership import (
    _FakeClient, _assistant, _emit_autonomous, _init_frame, _result,
    _start_fake_session, _task_notification, _task_updated,
)

H = {"Authorization": f"Bearer {TOKEN}"}


def _managed_in_thread(sess, msg: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    def run() -> None:
        try:
            out["o"] = sess.send_managed(msg)
        except BaseException as e:  # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    out["t"] = t
    return out


# =========================================================================== #
# Driver — echo correlation (adopted D1 / D1b / D2)
# =========================================================================== #
def test_D1_notification_folded_mid_turn_managed_reply_still_served():
    """A task-notification drained INSIDE the running managed turn (no separate
    autonomous turn) must not divert that turn's own reply."""
    fake = _FakeClient()
    sess = _start_fake_session(fake)
    proactive: List[str] = []
    sess._on_proactive = lambda k, o: proactive.append(o.output)
    sess._turn_timeout_sec = lambda: 0.6
    try:
        out = _managed_in_thread(sess, "run tests in background then report")
        time.sleep(0.1)
        _emit_autonomous(sess, fake, _init_frame(), _task_updated("bg", "running"),
                         _assistant("started bg"), _task_notification("bg", "completed"),
                         _assistant("REAL"), _result("REAL"))
        out["t"].join(2)
        assert "o" in out and out["o"].output == "REAL", (out.get("e"), proactive)
        assert proactive == []
    finally:
        sess.close()


def test_D1b_after_folded_notification_session_becomes_quiescent_again():
    fake = _FakeClient()
    sess = _start_fake_session(fake)
    sess._on_proactive = lambda k, o: None
    sess._turn_timeout_sec = lambda: 0.4
    try:
        out = _managed_in_thread(sess, "p")
        time.sleep(0.1)
        _emit_autonomous(sess, fake, _init_frame(), _task_updated("bg", "running"),
                         _task_notification("bg", "completed"), _assistant("REAL"), _result("REAL"))
        out["t"].join(2)
        time.sleep(1.0)  # CLI is idle; no further frames will ever arrive
        assert sess.is_quiescent() is True, (
            "wedged: pending=%d abandoned=%s" % (len(sess._pending), [p.abandoned for p in sess._pending]))
    finally:
        sess.close()


def test_D2_batched_notifications_one_continuation_next_reply_served():
    """Two background tasks finish together; the CLI runs ONE continuation.
    The following managed turn's own reply must be served to it."""
    fake = _FakeClient()
    sess = _start_fake_session(fake)
    proactive: List[str] = []
    sess._on_proactive = lambda k, o: proactive.append(o.output)
    sess._turn_timeout_sec = lambda: 0.6
    try:
        _emit_autonomous(sess, fake, _task_updated("a", "running"), _task_updated("b", "running"),
                         _task_notification("a"), _task_notification("b"),
                         _init_frame(), _assistant("AUTO"), _result("AUTO"))
        time.sleep(0.2)
        out = _managed_in_thread(sess, "next")
        time.sleep(0.1)
        _emit_autonomous(sess, fake, _init_frame(), _assistant("REAL"), _result("REAL"))
        out["t"].join(2)
        assert "o" in out and out["o"].output == "REAL"
        assert proactive == ["AUTO"]
    finally:
        sess.close()


def test_D3_abandoned_unechoed_prompt_keeps_session_in_flight_until_its_echo():
    """[rework 5, MINOR-1] A managed prompt written to the CLI but not yet
    echoed is still owed by the CLI: after the caller's deadline the session
    stays NOT quiescent — an unrelated result neither pops it nor makes the
    session quiescent. Its own echo + result route the reply as late_managed
    (bound to its turn uuid) and only then is the session quiescent."""
    fake = _FakeClient()
    fake.defer_echo = True
    sess = _start_fake_session(fake)
    got: List[Any] = []
    sess._on_proactive = lambda k, o: got.append(o)
    sess._turn_timeout_sec = lambda: 0.3
    try:
        out = _managed_in_thread(sess, "queued prompt")
        out["t"].join(2)
        assert "e" in out  # RecoveryRequiredError at the deadline
        assert sess.is_quiescent() is False
        _emit_autonomous(sess, fake, _assistant("x"), _result("FOREIGN"))
        time.sleep(0.2)
        assert len(sess._pending) == 1 and sess.is_quiescent() is False, "queued prompt forgotten"
        _emit_autonomous(sess, fake, fake.echo_for(), _assistant("y"), _result("LATE OURS"))
        time.sleep(0.3)
        assert [(o.output, o.late_managed) for o in got] == [("FOREIGN", False), ("LATE OURS", True)]
        assert got[1].managed_turn_uuid == fake.query_uuids[-1] and got[0].managed_turn_uuid == ""
        assert sess.is_quiescent() is True and fake.interrupts == 0
    finally:
        sess.close()


def test_D3b_unechoed_prompt_exit_on_stream_end():
    """Exit bound: stream end (session close / CLI death) fails the pending
    entry, so the session never stays wedged in memory."""
    fake = _FakeClient()
    fake.defer_echo = True
    sess = _start_fake_session(fake)
    sess._turn_timeout_sec = lambda: 0.2
    out = _managed_in_thread(sess, "never echoed")
    out["t"].join(2)
    assert len(sess._pending) == 1
    sess.close()
    time.sleep(0.3)
    assert len(sess._pending) == 0


def test_MAJOR2_foreign_tool_result_user_message_does_not_claim_managed_turn():
    """Mutation guard for `p.turn_uuid == uid`: a foreign turn carrying a
    tool_result UserMessage (different uuid) must not become the managed turn."""
    from claude_agent_sdk import ToolResultBlock, UserMessage

    fake = _FakeClient()
    fake.defer_echo = True
    sess = _start_fake_session(fake)
    proactive: List[str] = []
    sess._on_proactive = lambda k, o: proactive.append(o.output)
    sess._turn_timeout_sec = lambda: 5
    try:
        out = _managed_in_thread(sess, "mine")
        time.sleep(0.1)
        tool_result = UserMessage(
            content=[ToolResultBlock(tool_use_id="tu-1", content="ok", is_error=False)],
            uuid="some-other-uuid",
        )
        _emit_autonomous(sess, fake, _assistant("using a tool"), tool_result, _result("FOREIGN TURN"))
        time.sleep(0.3)
        assert proactive == ["FOREIGN TURN"]
        assert "o" not in out and "e" not in out and out["t"].is_alive(), "managed future resolved by a foreign turn"
        _emit_autonomous(sess, fake, fake.echo_for(), _assistant("r"), _result("MY REPLY"))
        out["t"].join(2)
        assert out["o"].output == "MY REPLY"
    finally:
        sess.close()


def test_D4_managed_send_requires_echo_replay_and_legacy_ignores_echoes(monkeypatch):
    from src.control.turn_queue import ManagedUnsupportedError

    monkeypatch.delenv("WORKER_MANAGED_TURNS", raising=False)
    assert _SDKSession("k", "/tmp", None, {})._replay_user_messages is False
    monkeypatch.setenv("WORKER_MANAGED_TURNS", "1")
    assert _SDKSession("k", "/tmp", None, {})._replay_user_messages is True
    fake = _FakeClient()
    fake.replies["q"] = [_result("legacy reply")]
    sess = _start_fake_session(fake)
    proactive: List[Any] = []
    sess._on_proactive = lambda k, o: proactive.append(o)
    try:
        # Legacy send with replayed echoes in the stream: byte-identical routing.
        assert sess.send("q").output == "legacy reply" and proactive == []
        sess._replay_user_messages = False
        with pytest.raises(ManagedUnsupportedError):
            sess.send_managed("m")
        assert fake.queries_sent == ["q"], "managed prompt submitted without echo correlation"
    finally:
        sess.close()


def test_m5_reply_served_between_timeout_and_abandon_goes_late():
    """m5: if the reply was set on the future after the caller timed out but
    before the abandon ran, it is re-routed as late_managed (never lost)."""
    fake = _FakeClient()
    sess = _start_fake_session(fake)
    got: List[Any] = []
    sess._on_proactive = lambda k, o: got.append(o)
    try:
        fut = asyncio.run_coroutine_threadsafe(sess._submit_turn("m", managed=True), sess._loop)
        time.sleep(0.1)
        _emit_autonomous(sess, fake, _result("SERVED"))  # echo was emitted at query
        assert fut.result(2).output == "SERVED"           # future holds the reply…
        sess._loop.call_soon_threadsafe(sess._abandon_managed_pending)  # …caller timed out
        time.sleep(0.3)
        assert [(o.output, o.late_managed) for o in got] == [("SERVED", True)]
    finally:
        sess.close()


def test_M3a_late_handoff_keeps_session_non_quiescent_until_sink_returns():
    """Mutation guard: removing the `_late_handoffs` conjunct must fail this."""
    fake = _FakeClient()
    fake.defer_echo = True
    sess = _start_fake_session(fake)
    release = threading.Event()
    sess._on_proactive = lambda k, o: release.wait(3)
    sess._turn_timeout_sec = lambda: 0.3
    try:
        out = _managed_in_thread(sess, "slow")
        out["t"].join(2)
        _emit_autonomous(sess, fake, fake.echo_for(), _result("LATE"))
        time.sleep(0.2)
        assert len(sess._pending) == 0
        assert sess.is_quiescent() is False, "quiescent while the late reply is still being handed off"
        release.set()
        time.sleep(0.2)
        assert sess.is_quiescent() is True
    finally:
        release.set()
        sess.close()


# =========================================================================== #
# Server — streamed byte cap (S1), carrier_restarted proof (S2 + B2)
# =========================================================================== #
def _started(c, db, tid, incarnation="i"):
    _register_node(c)
    _seed_turn(db, tid, "sess-" + tid)
    tok = c.post(f"/tasks/{tid}/claim-managed", json={"node_id": NODE, "incarnation_id": incarnation}, headers=H).json()["claim_token"]
    assert c.post(f"/tasks/{tid}/start-managed", json={"node_id": NODE, "claim_token": tok, "incarnation_id": incarnation}, headers=H).status_code == 200
    return tok


def test_S1_chunked_oversize_body_rejected_before_parse(db):
    c = TestClient(ts.app)
    tok = _started(c, db, "t-s1")
    big = json.dumps({"node_id": NODE, "claim_token": tok, "success": True,
                      "output": "x" * (9 * 1024 * 1024)}).encode()

    def gen():
        for i in range(0, len(big), 65536):
            yield big[i:i + 65536]
    r = c.post("/tasks/t-s1/result-managed", content=gen(),
               headers={**H, "content-type": "application/json"})
    assert r.status_code == 413 and r.json()["detail"]["reason"] == "payload_too_large"
    assert _row(db, "t-s1")["status"] == "running"
    # Small managed control routes have a tight cap too.
    r = c.post("/tasks/t-s1/enter-recovery", content=iter([b"{" + b" " * 20000 + b"}"]),
               headers={**H, "content-type": "application/json"})
    assert r.status_code == 413


def test_S1b_operator_route_streamed_cap(monkeypatch):
    from src.control import control_api

    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "admin-tok")
    api = TestClient(control_api.build_control_api(object()))
    body = iter([b'{"decision":"failed","note":"' + b"x" * 20000 + b'"}'])
    r = api.post("/api/turn-requests/x/resolve-recovery", content=body,
                 headers={"Authorization": "Bearer admin-tok", "content-type": "application/json"})
    assert r.status_code == 413


def _register_with_incarnation(c, inc):
    r = c.post("/nodes/register", json={
        "node_id": NODE, "tailscale_ip": "127.0.0.1", "api_port": 0, "incarnation_id": inc,
        "capabilities": {"backends": ["claude"], "queue_protocols": [0, 1], "managed_backends": ["claude"]},
    }, headers=H)
    assert r.status_code == 200


def test_S2_carrier_restarted_refused_for_same_live_incarnation_even_with_proof(db):
    """Mutation guard for `observer_incarnation != claim_inc`: observer ==
    registered == claim incarnation (the live claimer itself) must be refused."""
    c = TestClient(ts.app)
    _seed_turn(db, "t-s2", "sess-t-s2")
    _register_with_incarnation(c, "live")
    tok = c.post("/tasks/t-s2/claim-managed", json={"node_id": NODE, "incarnation_id": "live"}, headers=H).json()["claim_token"]
    assert c.post("/tasks/t-s2/start-managed", json={"node_id": NODE, "claim_token": tok, "incarnation_id": "live"}, headers=H).status_code == 200
    assert db.enter_recovery("t-s2", tok, reason="x")
    body = {"node_id": NODE, "claim_token": tok, "quiescent": True, "terminal": True,
            "terminal_status": "failed", "stop_evidence": "carrier_restarted",
            "observer_incarnation": "live", "process_proof": {"pid": 1, "observed": "absent"}}
    assert c.post("/tasks/t-s2/quiescence", json=body, headers=H).status_code == 409
    assert _row(db, "t-s2")["status"] == "recovery_required"


def test_B2_carrier_restarted_requires_process_proof(db):
    c = TestClient(ts.app)
    _seed_turn(db, "t-p", "sess-t-p")
    _register_with_incarnation(c, "old")
    tok = c.post("/tasks/t-p/claim-managed", json={"node_id": NODE, "incarnation_id": "old"}, headers=H).json()["claim_token"]
    assert c.post("/tasks/t-p/start-managed", json={"node_id": NODE, "claim_token": tok, "incarnation_id": "old"}, headers=H).status_code == 200
    assert db.enter_recovery("t-p", tok, reason="x")
    _register_with_incarnation(c, "new")
    base = {"node_id": NODE, "claim_token": tok, "quiescent": True, "terminal": True,
            "terminal_status": "failed", "stop_evidence": "carrier_restarted", "observer_incarnation": "new"}
    assert c.post("/tasks/t-p/quiescence", json=base, headers=H).status_code == 409
    assert c.post("/tasks/t-p/quiescence", json={**base, "process_proof": {"pid": 5, "observed": "alive"}}, headers=H).status_code == 409
    r = c.post("/tasks/t-p/quiescence", json={**base, "process_proof": {"pid": 5, "observed": "absent"}}, headers=H)
    assert r.status_code == 200 and _row(db, "t-p")["status"] == "failed"
    assert "process_proof" in _row(db, "t-p")["error"]


# =========================================================================== #
# Worker — B2 proof rules (psutil present/absent/denied/alive)
# =========================================================================== #
def _crash_after_invoke(ident):
    async def run(task_row, backends, http=None, telemetry_sink=None, node_id="", ownership=None, on_process=None):
        on_process(ident)
        raise RuntimeError("carrier died")
    return run


def _live_identity():
    import os

    from src.core.process_utils import process_identity
    return process_identity(os.getpid())


@pytest.mark.parametrize("ident_kind,expect", [
    ("dead", "failed"),                       # pid absent (Linux /proc)
    ("reused", "failed"),                     # same pid, different boot-relative start ticks
    ("rebooted", "failed"),                   # different boot_id
    ("alive", "recovery_required"),           # the very process still runs
    ("no-ticks", "recovery_required"),        # identity without boot-relative ticks + no psutil
    ("unrecorded", "recovery_required"),      # backend never reported a pid
], ids=["absent", "pid-reused", "rebooted", "alive", "ambiguous", "unrecorded"])
def test_B2_boot_resolution_only_with_process_gone_proof(db, tmp_path, monkeypatch, ident_kind, expect):
    from tests.test_turn_queue_carrier_recovery import dead_process_identity

    live = _live_identity()
    ident = {
        "dead": dead_process_identity(),
        "reused": {**live, "starttime_ticks": live["starttime_ticks"] + 7},
        "rebooted": {**live, "boot_id": "00000000-dead-beef-0000-000000000000"},
        "alive": live,
        "no-ticks": {"pid": live["pid"], "create_time": 1000.0},
        "unrecorded": {},
    }[ident_kind]
    monkeypatch.setattr(agent_mod, "_execute_task", _crash_after_invoke(ident))
    client = TestClient(ts.app)
    w1 = _worker(tmp_path, _ClientHTTP(client), incarnation="inc-old")
    _seed_turn(db, "t-b2", "sess-b2")
    with pytest.raises(RuntimeError):
        _run_one(w1, "t-b2")
    w2 = _worker(tmp_path, _ClientHTTP(client), incarnation="inc-new")
    asyncio.run(w2._reconcile_managed_claims())
    assert _row(db, "t-b2")["status"] == expect
    held = expect == "recovery_required"
    assert (w2._claim_store.get("t-b2") is not None) is held, "held attempt must stay for the operator"


def test_B2_non_linux_proof_is_wall_clock_tolerant_and_fails_closed(monkeypatch):
    import src.core.process_utils as pu

    monkeypatch.setattr(pu, "_procfs_available", lambda: False)
    rec = {"pid": 77, "create_time": 1000.0, "cmdline": ["claude", "--x"]}
    monkeypatch.setattr(pu, "psutil", None)
    assert pu.process_gone_proof(rec) is None                      # no psutil ⇒ no proof
    monkeypatch.setattr(pu, "psutil", fake_psutil(alive={}))
    assert pu.process_gone_proof(rec) == {"pid": 77, "observed": "absent"}
    monkeypatch.setattr(pu, "psutil", fake_psutil(denied=True))
    assert pu.process_gone_proof(rec) is None                      # access denied ⇒ no proof
    step = fake_psutil(alive={77: 1001.5})                          # NTP step of 1.5 s, same cmdline
    step.Process.cmdline = lambda self: ["claude", "--x"]
    monkeypatch.setattr(pu, "psutil", step)
    assert pu.process_gone_proof(rec) is None, "a wall-clock step was taken as pid reuse"
    reused = fake_psutil(alive={77: 5000.0})
    reused.Process.cmdline = lambda self: ["bash"]
    monkeypatch.setattr(pu, "psutil", reused)
    assert pu.process_gone_proof(rec) == {"pid": 77, "observed": "pid_reused"}
    same_cmd = fake_psutil(alive={77: 5000.0})
    same_cmd.Process.cmdline = lambda self: ["claude", "--x"]
    monkeypatch.setattr(pu, "psutil", same_cmd)
    assert pu.process_gone_proof(rec) is None                      # ambiguous ⇒ fail closed


# =========================================================================== #
# Worker — M1 poll loop survives managed-path failures (adopted S3)
# =========================================================================== #
def _enospc_on_dead(monkeypatch):
    from pathlib import Path

    real_mkdir = Path.mkdir

    def enospc(self, *a, **k):
        if "managed_result_dead" in str(self):
            raise OSError(28, "No space left on device")
        return real_mkdir(self, *a, **k)
    monkeypatch.setattr(Path, "mkdir", enospc)


def test_S3_disk_full_on_dead_letter_does_not_escape(db, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_execute_task", _FakeBackend())
    http = _ClientHTTP(TestClient(ts.app))
    http.faults["/result-managed"] = [503, 422]
    w = _worker(tmp_path, http)
    _seed_turn(db, "m-9", "sm-9")
    _run_one(w, "m-9")  # first POST 503 -> spooled, pending delivery
    _enospc_on_dead(monkeypatch)
    asyncio.run(w._redeliver_spooled_results())  # must not raise
    # Parked (not re-POSTed forever) and the attempt is in recovery; once the
    # recovery was acknowledged the refused envelope left the budget.
    assert _row(db, "m-9")["status"] == "recovery_required"
    assert _spooled(w) == [] and "m-9" not in w._delivery_parked
    n = len(http.calls)
    asyncio.run(w._redeliver_spooled_results())
    assert not any("/result-managed" in c[1] for c in http.calls[n:])


def test_M1_poll_loop_survives_managed_failure_and_schedules_legacy_row(tmp_path):
    w = _worker(tmp_path, _RecordingHTTP(), managed=True)
    scheduled: List[str] = []

    async def scenario():
        calls = {"n": 0}

        async def boom(*a, **k):
            raise OSError(28, "No space left on device")

        async def fetch():
            calls["n"] += 1
            if calls["n"] > 2:
                w._shutdown.set()
                return []
            w._poll_now.set()
            return [{"id": f"legacy-{calls['n']}", "action": "run_oneoff"}]

        async def handle(row):
            scheduled.append(row["id"])

        w._pending_result_delivery.add("stuck")
        w._redeliver_spooled_results = boom
        w._reconcile_managed_claims = boom
        w._fetch_pending = fetch
        w._handle_task = handle
        await w._poll_loop()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert scheduled == ["legacy-1", "legacy-2"]


# =========================================================================== #
# Worker — M2 bounded dead letters + rotating cursor
# =========================================================================== #
def test_M2_dead_letter_cap_parks_without_reposting(db, tmp_path, monkeypatch):
    import src.worker.managed_result_spool as spool_mod

    monkeypatch.setattr(spool_mod, "MAX_DEAD_LETTERS", 0)  # dead-letter dir "full"
    monkeypatch.setattr(agent_mod, "_execute_task", _FakeBackend())
    http = _ClientHTTP(TestClient(ts.app))
    http.faults["/result-managed"] = [422]
    http.faults["/enter-recovery"] = [503]  # recovery not yet acknowledged
    w = _worker(tmp_path, http)
    _seed_turn(db, "d-1", "sd-1")
    _run_one(w, "d-1")
    assert "d-1" in w._delivery_parked and _spooled(w) == ["d-1"]
    n = len(http.calls)
    asyncio.run(w._redeliver_spooled_results())
    assert not any("/result-managed" in c[1] for c in http.calls[n:]), "parked envelope re-POSTed"


def test_M2_rotating_cursor_stuck_head_cannot_starve_later_records(db, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_execute_task", _FakeBackend())
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    for tid in ("r-a", "r-b", "r-c"):
        _seed_turn(db, tid, "s" + tid)
        http.faults["/tasks/%s/result-managed" % tid] = [503]
        _run_one(w, tid)
    assert _spooled(w) == ["r-a", "r-b", "r-c"]
    http.faults["/tasks/r-a/result-managed"] = [503] * 50  # a permanently stuck head
    for _ in range(4):
        asyncio.run(w._redeliver_spooled_results(batch=1))
    assert _spooled(w) == ["r-a"], "later envelopes starved behind a stuck head"


# =========================================================================== #
# Worker — reconciler guards (M3c mutation guard, m1)
# =========================================================================== #
def test_M3c_reconciler_leaves_attempts_owned_by_result_delivery(db, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_execute_task", _FakeBackend())
    http = _ClientHTTP(TestClient(ts.app))
    http.faults["/result-managed"] = [503]
    w = _worker(tmp_path, http)
    _seed_turn(db, "c-1", "sc-1")
    _run_one(w, "c-1")
    assert "c-1" in w._pending_result_delivery
    asyncio.run(w._reconcile_managed_claims())
    assert _row(db, "c-1")["status"] == "running", "reconciler raced the result path"
    assert not any("enter-recovery" in c[1] or "quiescence" in c[1] for c in http.calls)


def test_m1_late_result_captured_during_quiescence_probe_is_not_resolved_failed(db, tmp_path, monkeypatch):
    class _Uncertain(_FakeBackend):
        async def __call__(self, *a, **k):
            return {"success": False, "output": "", "errors": ["deadline"],
                    "error_class": "recovery_required", "execution_time": 0.0}

    monkeypatch.setattr(agent_mod, "_execute_task", _Uncertain())
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    _seed_turn(db, "l-1", "sl-1")
    _run_one(w, "l-1")

    def probe(session):
        w._pending_result_delivery.add("l-1")  # late reply captured meanwhile
        return True
    w._backends["claude"].is_quiescent = probe
    asyncio.run(w._reconcile_managed_claims())
    assert not any("quiescence" in c[1] for c in http.calls)
    assert _row(db, "l-1")["status"] == "recovery_required"


# =========================================================================== #
# Minors m3 / m4
# =========================================================================== #
def test_m3_spool_sized_with_wire_serialization(tmp_path):
    from src.worker.managed_result_spool import ManagedResultSpool, OversizeResultError

    sp = ManagedResultSpool(str(tmp_path), max_envelope_bytes=4000)
    env = {"output": "é" * 1000}  # 2000 B as UTF-8, 6000 B as the ASCII wire form
    assert len(json.dumps(env, ensure_ascii=False).encode()) < 4000 < len(json.dumps(env).encode())
    with pytest.raises(OversizeResultError):
        sp.commit("t", "tok", env)


def test_m4_offline_sweep_does_not_report_managed_rows(db):
    from src.control.node_registry import NodeRegistry

    c = TestClient(ts.app)
    _register_node(c)
    _seed_turn(db, "o-m", "sess-o-m")
    assert c.post("/tasks/o-m/claim-managed", json={"node_id": NODE, "incarnation_id": "i"}, headers=H).status_code == 200
    assert _row(db, "o-m")["status"] == "claimed"
    failed = NodeRegistry()._fail_offline_tasks(NODE)
    assert "o-m" not in failed
    assert _row(db, "o-m")["status"] == "claimed"
