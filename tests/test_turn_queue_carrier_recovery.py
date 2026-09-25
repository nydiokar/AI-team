"""A82 Stage 3 rework 3 — "every managed state has a live, tested exit".

Adopts the A87 adversarial probes (P1-P6b, originally written to FAIL on
`7863895`) as permanent tests asserting the CORRECT behavior, plus the
blocker/major/minor fixes they exposed. Offline only: fake SDK client,
in-process TestClient, file-backed temp SQLite. No real Claude/Codex.
"""
import asyncio
import json
import os
import threading
import time
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import src.control.task_server as ts
import src.worker.agent as agent_mod
from src.control.db import MeshDB
from tests.test_turn_queue_carrier_integration import (  # noqa: F401  (fixtures)
    NODE, NOW, TOKEN, _ClientHTTP, _FakeBackend, _RecordingHTTP, _row, _run_one,
    _seed_session_turn, _seed_turn, _sess, _spooled, _worker, _register_node,
    db, real_claude,
)
from tests.test_turn_queue_sdk_ownership import (
    _FakeClient, _assistant, _emit_autonomous, _result, _start_fake_session,
    _task_notification, _task_updated,
)

H = {"Authorization": f"Bearer {TOKEN}"}


def _claims(w) -> List[str]:
    return [r["task_id"] for r in w._claim_store.list()]


# =========================================================================== #
# P1 / M1 — control rows are never starved; flag OFF poll loop == main
# =========================================================================== #
def _poll_once(w, rows):
    scheduled: List[str] = []

    async def scenario():
        calls = {"n": 0}

        async def fetch():
            calls["n"] += 1
            if calls["n"] > 1:
                w._shutdown.set()
                return []
            w._poll_now.set()
            return rows

        async def handle(row):
            scheduled.append(row["id"])

        blocker = asyncio.get_event_loop().create_future()
        w._active = {"turn-running": blocker, "turn-waiting": blocker}
        w._fetch_pending = fetch
        w._handle_task = handle
        await w._poll_loop()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    return scheduled


def test_P1_flag_off_control_row_scheduled_and_loop_identical_to_main(tmp_path):
    """Flag OFF: no dedup and no capacity gate (main's loop) — the close row AND
    the re-fetched task row are both scheduled, exactly as on main."""
    w = _worker(tmp_path, None, managed=False)
    w.cfg.max_concurrent = 1
    rows = [{"id": "turn-waiting", "action": "resume_session"},
            {"id": "close-1", "action": "close_session", "session_id": "s"},
            {"id": "cancel-1", "action": "cancel_codex"}]
    assert _poll_once(w, rows) == ["turn-waiting", "close-1", "cancel-1"]


def test_P1b_flag_on_capacity_gate_exempts_control_rows(tmp_path):
    w = _worker(tmp_path, _RecordingHTTP(), managed=True)
    w.cfg.max_concurrent = 1
    rows = [{"id": "turn-waiting", "action": "resume_session"},   # dedup
            {"id": "turn-new", "action": "resume_session"},       # capacity-gated
            {"id": "close-1", "action": "close_session", "session_id": "s"},
            {"id": "cancel-1", "action": "cancel_codex"}]
    assert _poll_once(w, rows) == ["close-1", "cancel-1"]


# =========================================================================== #
# P2 / B1 — lost /start-managed response never wedges the turn
# =========================================================================== #
class _LoseStart(_ClientHTTP):
    """Server COMMITS /start-managed but the response is lost `lose` times."""

    def __init__(self, client, lose: int = 1, lose_release: int = 0) -> None:
        super().__init__(client)
        self.lose, self.lose_release = lose, lose_release

    def post(self, path, body=None, timeout=10):
        if path.endswith("/start-managed") and self.lose > 0:
            self.lose -= 1
            super().post(path, body, timeout)
            raise TimeoutError("response lost after server commit")
        if path.endswith("/release-managed") and self.lose_release > 0:
            self.lose_release -= 1
            self.calls.append(("POST", path, body))
            raise TimeoutError("release response lost")
        return super().post(path, body, timeout)


def test_P2_lost_start_response_is_retried_idempotently_and_turn_runs(db, tmp_path, monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    http = _LoseStart(TestClient(ts.app), lose=1)
    w = _worker(tmp_path, http)
    _seed_turn(db, "t-1", "sess-1")
    _run_one(w, "t-1")
    posted = [c[1] for c in http.calls if c[0] == "POST"]
    assert posted.count("/tasks/t-1/start-managed") == 2
    assert "/tasks/t-1/release-managed" not in posted
    assert len(backend.rows) == 1
    assert _row(db, "t-1")["status"] == "completed"
    assert _claims(w) == [] and db.get_active_turn("sess-1") is None


def test_P2b_start_never_confirmed_releases_with_not_invoked_attestation(db, tmp_path, monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    http = _LoseStart(TestClient(ts.app), lose=99)
    w = _worker(tmp_path, http)
    _seed_turn(db, "t-2", "sess-2", prompt="keep me")
    _run_one(w, "t-2")
    row = _row(db, "t-2")
    assert backend.rows == []
    assert row["status"] == "pending", "started-but-never-invoked turn must return to pending"
    assert row["claim_token"] is None and row["claim_incarnation"] is None and row["started_at"] is None
    assert json.loads(row["payload"])["prompt"] == "keep me"
    assert _claims(w) == []


def test_P2c_unreachable_release_keeps_token_durably_then_reconciler_releases(db, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_execute_task", _FakeBackend())
    client = TestClient(ts.app)
    http = _LoseStart(client, lose=99, lose_release=1)
    w = _worker(tmp_path, http)
    _seed_turn(db, "t-3", "sess-3")
    _run_one(w, "t-3")
    assert _row(db, "t-3")["status"] == "running"
    rec = w._claim_store.get("t-3")
    assert rec and rec["status"] == "start_unknown" and rec["invoked"] is False, "token discarded"
    # Next poll pass: the reconciler moves it.
    http.lose = 0
    assert asyncio.run(w._reconcile_managed_claims()) == 1
    assert _row(db, "t-3")["status"] == "pending" and _claims(w) == []


# =========================================================================== #
# B2 — durable claims: crash mid-turn / before invoke always has an exit
# =========================================================================== #
class _Crash(Exception):
    pass


def _crashing_backend():
    async def run(task_row, backends, http=None, telemetry_sink=None, node_id="", ownership=None):
        raise _Crash("worker process died mid-turn")
    return run


def test_B2_crash_mid_turn_resolved_at_boot_by_new_incarnation(db, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_execute_task", _crashing_backend())
    client = TestClient(ts.app)
    w1 = _worker(tmp_path, _ClientHTTP(client), incarnation="inc-old")
    _seed_turn(db, "t-4", "sess-4")
    with pytest.raises(_Crash):
        _run_one(w1, "t-4")
    assert _row(db, "t-4")["status"] == "running"
    rec = w1._claim_store.get("t-4")
    assert rec["invoked"] is True and rec["claim_token"], "token must be durable before invoke"
    # New process, same carrier state dir; registers its new incarnation.
    w2 = _worker(tmp_path, _ClientHTTP(client), incarnation="inc-new")
    assert asyncio.run(w2._reconcile_managed_claims()) == 1
    row = _row(db, "t-4")
    assert row["status"] == "failed"
    assert "carrier_restarted" in (row["error"] or "")
    assert rec["claim_token"] not in (row["error"] or ""), "token leaked into recorded evidence"
    assert db.get_active_turn("sess-4") is None
    assert _claims(w2) == []


def test_B2b_crash_before_invoke_returns_turn_to_pending_at_boot(db, tmp_path, monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    client = TestClient(ts.app)
    w1 = _worker(tmp_path, _ClientHTTP(client), incarnation="inc-old")
    _seed_turn(db, "t-5", "sess-5")

    async def crash_after_start():
        rows = await w1._fetch_pending()
        assert await w1._claim_and_start_managed("t-5") is not None  # started, not invoked
    asyncio.run(crash_after_start())
    assert _row(db, "t-5")["status"] == "running"
    w2 = _worker(tmp_path, _ClientHTTP(client), incarnation="inc-new")
    asyncio.run(w2._reconcile_managed_claims())
    row = _row(db, "t-5")
    assert row["status"] == "pending" and row["claim_token"] is None
    assert backend.rows == []


def test_B2c_enter_recovery_retried_until_acked_then_resolved_by_quiescence(db, tmp_path, monkeypatch):
    class _Uncertain(_FakeBackend):
        async def __call__(self, *a, **k):
            await super().__call__(*a, **k)
            return {"success": False, "output": "", "errors": ["uncorrelated"],
                    "error_class": "recovery_required", "execution_time": 0.0}

    monkeypatch.setattr(agent_mod, "_execute_task", _Uncertain())
    http = _ClientHTTP(TestClient(ts.app))
    http.faults["/enter-recovery"] = [503]
    w = _worker(tmp_path, http)
    _seed_turn(db, "t-6", "sess-6")
    _run_one(w, "t-6")
    assert _row(db, "t-6")["status"] == "running"
    assert w._claim_store.get("t-6")["recovery_acked"] is False
    # Poll pass: recovery acked, backend oracle quiescent ⇒ resolved with evidence.
    assert asyncio.run(w._reconcile_managed_claims()) == 1
    row = _row(db, "t-6")
    assert row["status"] == "failed" and "backend_quiescent" in row["error"]
    assert _claims(w) == [] and db.get_active_turn("sess-6") is None


def test_B2d_recovery_held_while_backend_not_quiescent(db, tmp_path, monkeypatch):
    class _Uncertain(_FakeBackend):
        async def __call__(self, *a, **k):
            return {"success": False, "output": "", "errors": ["deadline"],
                    "error_class": "recovery_required", "execution_time": 0.0}

    monkeypatch.setattr(agent_mod, "_execute_task", _Uncertain())
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    _seed_turn(db, "t-7", "sess-7")
    _run_one(w, "t-7")
    w._backends["claude"].is_quiescent = lambda s: False
    assert asyncio.run(w._reconcile_managed_claims()) == 0
    assert _row(db, "t-7")["status"] == "recovery_required"
    assert _claims(w) == ["t-7"]
    assert w._managed_shutdown_release_ok({"id": "t-7", "queue_protocol": 1, "status": "recovery_required"}) is False


# =========================================================================== #
# B2 operator route — a human can always unwedge (admin auth, no token)
# =========================================================================== #
@pytest.fixture()
def admin(monkeypatch):
    from src.control import control_api

    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "admin-tok")
    return TestClient(control_api.build_control_api(object())), {"Authorization": "Bearer admin-tok"}


def _claim(client, tid, start=True):
    _register_node(client)
    tok = client.post(f"/tasks/{tid}/claim-managed", json={"node_id": NODE, "incarnation_id": "i"}, headers=H).json()["claim_token"]
    if start:
        assert client.post(f"/tasks/{tid}/start-managed", json={"node_id": NODE, "claim_token": tok, "incarnation_id": "i"}, headers=H).status_code == 200
    return tok


def test_B2e_operator_resolve_route_exits_every_held_state(db, admin):
    api, ah = admin
    c = TestClient(ts.app)
    url = "/api/turn-requests/{}/resolve-recovery"
    # No auth ⇒ 401.
    assert api.post(url.format("x"), json={"decision": "failed"}).status_code == 401
    # claimed (never started) ⇒ requeue only.
    _seed_turn(db, "o-1", "so-1"); _claim(c, "o-1", start=False)
    assert api.post(url.format("o-1"), json={"decision": "failed", "acknowledge_uncertain": True}, headers=ah).status_code == 409
    r = api.post(url.format("o-1"), json={"decision": "requeue"}, headers=ah)
    assert r.status_code == 200 and _row(db, "o-1")["status"] == "pending" and _row(db, "o-1")["claim_token"] is None
    # running ⇒ acknowledgement required; never requeue; never completed.
    _seed_turn(db, "o-2", "so-2"); _claim(c, "o-2")
    assert api.post(url.format("o-2"), json={"decision": "failed"}, headers=ah).status_code == 409
    assert api.post(url.format("o-2"), json={"decision": "requeue", "acknowledge_uncertain": True}, headers=ah).status_code == 409
    assert api.post(url.format("o-2"), json={"decision": "completed", "acknowledge_uncertain": True}, headers=ah).status_code == 422
    r = api.post(url.format("o-2"), json={"decision": "failed", "acknowledge_uncertain": True, "note": "box died"}, headers=ah)
    assert r.status_code == 200
    row = _row(db, "o-2")
    assert row["status"] == "failed" and "operator" in row["error"] and "box died" in row["error"]
    assert db.get_active_turn("so-2") is None
    # recovery_required ⇒ cancelled.
    _seed_turn(db, "o-3", "so-3"); tok = _claim(c, "o-3")
    assert db.enter_recovery("o-3", tok, reason="x")
    assert api.post(url.format("o-3"), json={"decision": "cancelled", "acknowledge_uncertain": True}, headers=ah).status_code == 200
    assert _row(db, "o-3")["status"] == "cancelled"
    # Terminal / pending ⇒ nothing to resolve.
    assert api.post(url.format("o-3"), json={"decision": "failed", "acknowledge_uncertain": True}, headers=ah).status_code == 409
    assert "claim_token" not in r.text


def test_B2f_quiescence_evidence_must_be_verifiable(db):
    c = TestClient(ts.app)
    _seed_turn(db, "q-1", "sq-1")
    tok = _claim(c, "q-1")  # claim incarnation "i"; registered incarnation is NODE's register
    assert db.enter_recovery("q-1", tok, reason="x")
    base = {"node_id": NODE, "claim_token": tok, "quiescent": True, "terminal": True, "terminal_status": "failed"}
    # backend_quiescent from an incarnation that is not the claim's ⇒ refused.
    assert c.post("/tasks/q-1/quiescence", json={**base, "stop_evidence": "backend_quiescent", "observer_incarnation": "zzz"}, headers=H).status_code == 409
    # carrier_restarted claimed by the SAME incarnation that holds the claim ⇒ refused.
    assert c.post("/tasks/q-1/quiescence", json={**base, "stop_evidence": "carrier_restarted", "observer_incarnation": "i"}, headers=H).status_code == 409
    assert c.post("/tasks/q-1/quiescence", json={**base, "stop_evidence": "made_up"}, headers=H).status_code == 409
    assert _row(db, "q-1")["status"] == "recovery_required"


# =========================================================================== #
# P3 / M2 — non-quiescent session: released BEFORE start, prompt preserved
# =========================================================================== #
def test_P3_not_quiescent_session_releases_before_start_and_runs_later(db, tmp_path, real_claude):
    real_claude.sess._bg_task_status["bg-1"] = "running"   # previous turn's background job
    real_claude.fake.replies["user prompt"] = [_assistant("x", sid="n-3"), _result("y", sid="n-3")]
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._backends = {"claude": real_claude.backend}
    _seed_session_turn(db, "t-3", "sess-3", "user prompt")
    _run_one(w, "t-3")
    row = _row(db, "t-3")
    assert real_claude.fake.queries_sent == []
    assert row["status"] == "pending" and row["claim_token"] is None and row["error"] is None
    assert "/tasks/t-3/start-managed" not in [c[1] for c in http.calls]
    # Background job finishes ⇒ the same prompt runs.
    real_claude.sess._bg_task_status["bg-1"] = "completed"
    _run_one(w, "t-3")
    assert real_claude.fake.queries_sent == ["user prompt"]
    assert _row(db, "t-3")["status"] == "completed"


def test_P3b_conflict_detected_at_loop_reservation_returns_to_pending(db, tmp_path, monkeypatch):
    class _Conflict(_FakeBackend):
        async def __call__(self, *a, **k):
            await super().__call__(*a, **k)
            return {"success": False, "output": "", "errors": ["not quiescent"],
                    "error_class": "managed_conflict", "execution_time": 0.0}

    monkeypatch.setattr(agent_mod, "_execute_task", _Conflict())
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    _seed_turn(db, "t-8", "sess-8")
    _run_one(w, "t-8")
    row = _row(db, "t-8")
    assert row["status"] == "pending" and row["claim_token"] is None
    assert _claims(w) == []


# =========================================================================== #
# P4 / M3 — managed deadline: the late reply is captured, never dropped
# =========================================================================== #
def test_P4_managed_deadline_late_reply_reaches_sink_as_late_managed():
    from src.control.turn_queue import RecoveryRequiredError

    fake = _FakeClient()
    sess = _start_fake_session(fake)
    got: List[Any] = []
    sess._on_proactive = lambda k, o: got.append(o)
    sess._turn_timeout_sec = lambda: 0.3
    try:
        with pytest.raises(RecoveryRequiredError):
            sess.send_managed("slow prompt")
        assert sess.is_quiescent() is False
        _emit_autonomous(sess, fake, _assistant("LATE REPLY"), _result("LATE REPLY"))
        time.sleep(0.3)
        assert [(o.output, o.late_managed) for o in got] == [("LATE REPLY", True)]
        assert sess.is_quiescent() is True
        assert fake.interrupts == 0
    finally:
        sess.close()


def test_P4b_late_reply_completes_the_held_turn_via_carrier(db, tmp_path, real_claude):
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._backends = {"claude": real_claude.backend}
    real_claude.sess._on_proactive = w._deliver_proactive_turn
    real_claude.sess.session_key = "sess-9"  # production: pool key == session id
    real_claude.sess._turn_timeout_sec = lambda: 0.3
    real_claude.fake.replies["slow"] = []
    _seed_session_turn(db, "t-9", "sess-9", "slow")
    _run_one(w, "t-9")
    assert _row(db, "t-9")["status"] == "recovery_required"
    # Reconciler must NOT resolve while the late reply is still owed.
    assert asyncio.run(w._reconcile_managed_claims()) == 0
    _emit_autonomous(real_claude.sess, real_claude.fake, _assistant("late", sid="native-late"),
                     _result("LATE ANSWER", sid="native-late"))
    time.sleep(0.4)
    assert _spooled(w) == ["t-9"]
    asyncio.run(w._redeliver_spooled_results())
    row = _row(db, "t-9")
    assert row["status"] == "completed"
    assert json.loads(row["result"])["output"] == "LATE ANSWER"
    assert _sess(db, "sess-9")["backend_session_id"] == "native-late"
    assert _claims(w) == [] and db.get_active_turn("sess-9") is None
    assert not any("proactive-turn" in c[1] for c in http.calls)
    assert real_claude.fake.interrupts == 0


# =========================================================================== #
# P5 / M4 — autonomous continuation is never adopted as the managed reply
# =========================================================================== #
def test_P5_autonomous_continuation_not_served_as_managed_reply():
    fake = _FakeClient()
    sess = _start_fake_session(fake)
    proactive: List[str] = []
    sess._on_proactive = lambda k, o: proactive.append(o.output)
    try:
        _emit_autonomous(sess, fake, _task_updated("bg", "running"), _task_notification("bg", "completed"))
        time.sleep(0.1)
        out: Dict[str, Any] = {}

        def run():
            out["o"] = sess.send_managed("user prompt")
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.1)
        # The CLI's autonomous continuation for the finished task streams first…
        _emit_autonomous(sess, fake, _assistant("AUTONOMOUS"), _result("AUTONOMOUS"))
        time.sleep(0.1)
        # …then the real reply to the managed query.
        _emit_autonomous(sess, fake, _assistant("REAL"), _result("REAL"))
        t.join(2)
        assert out["o"].output == "REAL"
        assert proactive == ["AUTONOMOUS"]
        assert fake.interrupts == 0
    finally:
        sess.close()


# =========================================================================== #
# P6 / M5 — legacy routes/helpers never touch protocol-1 rows
# =========================================================================== #
def test_P6_legacy_result_route_refuses_managed_running_row(db):
    c = TestClient(ts.app)
    _seed_turn(db, "t-6", "sess-6")
    tok = _claim(c, "t-6")
    r = c.post("/tasks/t-6/result", json={"node_id": NODE, "success": True, "output": "forged"}, headers=H)
    assert r.status_code == 409
    assert _row(db, "t-6")["status"] == "running"
    assert db.enter_recovery("t-6", tok, reason="x")
    assert c.post("/tasks/t-6/result", json={"node_id": NODE, "success": False}, headers=H).status_code == 409
    assert _row(db, "t-6")["status"] == "recovery_required"
    assert c.post("/tasks/t-6/release", json={"node_id": NODE}, headers=H).status_code == 409


def test_P6b_legacy_claim_route_refuses_managed_pending_row(db):
    c = TestClient(ts.app)
    _seed_turn(db, "t-7", "sess-7")
    r = c.post("/tasks/t-7/claim", json={"node_id": "legacy-node"}, headers=H)
    assert r.status_code == 409
    assert _row(db, "t-7")["status"] == "pending"


def test_P6c_legacy_db_helpers_are_fenced_and_legacy_claim_has_no_token(db):
    c = TestClient(ts.app)
    _seed_turn(db, "t-8", "sess-8")
    tok = _claim(c, "t-8", start=False)
    assert db.release_task("t-8", NODE) is False
    assert db.release_node_claims(NODE) == []
    assert all(r["id"] != "t-8" for r in db.list_stale_claims(lease_sec=-1, live_state_max_age_sec=0, active_task_max_runtime_sec=0))
    db.fail_task("t-8", "reaper")
    assert _row(db, "t-8")["status"] == "claimed" and _row(db, "t-8")["claim_token"] == tok
    # Legacy claim response never carries a claim_token key.
    from src.core.interfaces import Session, SessionStatus
    db.upsert_session(Session(session_id="sl", backend="claude", repo_path="", status=SessionStatus.IDLE,
                              created_at=NOW, updated_at=NOW, machine_id=NODE))
    db.enqueue_task("leg", "sl", NODE, "claude", "run_oneoff", {"prompt": "p"})
    r = c.post("/tasks/leg/claim", json={"node_id": NODE}, headers=H)
    assert r.status_code == 200 and "claim_token" not in r.json()["task"]


# =========================================================================== #
# Minors
# =========================================================================== #
def test_m2_result_and_quiescence_bodies_capped_and_strict(db):
    c = TestClient(ts.app)
    _seed_turn(db, "m-2", "sm-2")
    tok = _claim(c, "m-2")
    base = {"node_id": NODE, "claim_token": tok, "success": True, "output": "ok"}
    assert c.post("/tasks/m-2/result-managed", json={**base, "evil": 1}, headers=H).status_code == 422
    assert c.post("/tasks/m-2/quiescence", json={"node_id": NODE, "claim_token": tok, "evil": 1}, headers=H).status_code == 422
    big = {**base, "output": "x" * (ts._MANAGED_BODY_MAX_BYTES + 1)}
    r = c.post("/tasks/m-2/result-managed", json=big, headers=H)
    assert r.status_code == 413 and r.json()["detail"]["reason"] == "payload_too_large"
    assert _row(db, "m-2")["status"] == "running"


def test_m3_definitive_4xx_dead_letters_and_enters_recovery(db, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_execute_task", _FakeBackend())
    http = _ClientHTTP(TestClient(ts.app))
    http.faults["/result-managed"] = [422]
    w = _worker(tmp_path, http)
    _seed_turn(db, "m-3", "sm-3")
    _run_one(w, "m-3")
    assert _spooled(w) == []
    assert len(list(w._result_spool.dead_dir.glob("*.json"))) == 1
    assert "m-3" not in w._pending_result_delivery
    assert _row(db, "m-3")["status"] == "recovery_required"
    n_before = len(http.calls)
    asyncio.run(w._redeliver_spooled_results())
    assert not any("/result-managed" in c[1] for c in http.calls[n_before:]), "dead letter retried"


def test_m4_orphan_tmp_files_cleaned_at_boot(tmp_path):
    w = _worker(tmp_path, _RecordingHTTP())
    for d in (w._result_spool.dir, w._claim_store.dir):
        d.mkdir(parents=True, exist_ok=True)
        (d / ".spool-dead.tmp").write_text("partial")
    w._replay_result_spool()
    assert not list(w._result_spool.dir.glob(".spool-*.tmp"))
    assert not list(w._claim_store.dir.glob(".spool-*.tmp"))


def test_m5_flag_off_constructs_no_carrier_state(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_STATE_DIR", str(tmp_path / "state"))
    w = _worker(tmp_path, None, managed=False)
    w._init_managed_state()
    assert w._result_spool is None and w._claim_store is None
    assert not (tmp_path / "state").exists()
    w.cfg.managed_turns = True
    w._init_managed_state()
    assert w._result_spool is not None and not (tmp_path / "state").exists()
    # Leftover state from a managed run still drains with the flag OFF.
    (tmp_path / "state" / NODE).mkdir(parents=True)
    w.cfg.managed_turns = False
    w._init_managed_state()
    assert w._result_spool is not None and w._claim_store is not None


def test_m6_reservation_released_on_cancellation(db, tmp_path, monkeypatch):
    async def cancelled(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(agent_mod, "_execute_task", cancelled)
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    _seed_turn(db, "m-6", "sm-6")
    with pytest.raises(asyncio.CancelledError):
        _run_one(w, "m-6")
    assert w._result_spool.reserved_bytes() == 0
    assert w._claim_store.get("m-6")["invoked"] is True  # still held for the reconciler
