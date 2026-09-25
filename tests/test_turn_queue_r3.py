"""A82 Stage 3 rework 5 — A87 round-3 review: adopted probes as permanent
tests (stale-claim late-reply misroute, body caps) + MINOR-2 retirement.
Offline only (TestClient, temp SQLite, fake backends)."""
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

import src.control.task_server as ts
import src.worker.agent as agent_mod
from tests.test_turn_queue_carrier_integration import (  # noqa: F401  (fixtures)
    TOKEN, _ClientHTTP, _FakeBackend, _row, _run_one, _seed_turn, _spooled, _worker, db,
)
from tests.test_turn_queue_carrier_recovery import _claims, _crashing_backend, admin  # noqa: F401

H = {"Authorization": f"Bearer {TOKEN}"}


def test_R3_late_reply_bound_to_turn_identity_not_session(db, tmp_path, monkeypatch, admin):
    """MAJOR-1: an earlier incarnation's attempt (no process proof ⇒ held),
    later operator-resolved, must be DROPPED by the reconciler (server
    consulted), and a late reply for the next turn on the same session must be
    bound to exactly that turn's identity — never to the stale record."""
    live = {"pid": __import__("os").getpid()}  # no boot-relative ticks ⇒ no proof ⇒ held
    monkeypatch.setattr(agent_mod, "_execute_task", _crashing_backend(live))
    client = TestClient(ts.app)
    w1 = _worker(tmp_path, _ClientHTTP(client), incarnation="inc-old")
    _seed_turn(db, "t-a", "sess-X")
    try:
        _run_one(w1, "t-a")
    except Exception:
        pass
    w2 = _worker(tmp_path, _ClientHTTP(client), incarnation="inc-new")
    asyncio.run(w2._reconcile_managed_claims())
    assert _row(db, "t-a")["status"] == "recovery_required" and _claims(w2) == ["t-a"]
    api, ah = admin
    r = api.post("/api/turn-requests/t-a/resolve-recovery",
                 json={"decision": "failed", "acknowledge_uncertain": True}, headers=ah)
    assert r.status_code == 200 and _row(db, "t-a")["status"] == "failed"
    asyncio.run(w2._reconcile_managed_claims())
    assert "t-a" not in _claims(w2), "stale record never dropped after operator resolution"
    # Next turn on the same session, invoked by this incarnation; its deadline
    # hit and the late reply arrives carrying ITS turn identity.
    w2._claim_record("t-b", claim_token="tok-b", session_id="sess-X", invoked=True,
                     incarnation_id="inc-new", backend="claude", turn_uuid="uuid-b")
    outcome = SimpleNamespace(late_managed=True, managed_turn_uuid="uuid-b", output="REPLY-FOR-T-B",
                              is_error=False, error_text="", error_class="", backend_session_id="n",
                              raw_ndjson="")
    assert w2._capture_late_managed_result("sess-X", outcome) is True
    assert _spooled(w2) == ["t-b"], "late reply for t-b spooled under another task"


def test_R3_late_reply_without_matching_identity_is_never_bound_by_session(db, tmp_path):
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    w._claim_record("t-old", claim_token="tok-o", session_id="sess-Y", invoked=True,
                    incarnation_id="inc-1", backend="claude", turn_uuid="uuid-old")
    for uid in ("", "uuid-other"):
        outcome = SimpleNamespace(late_managed=True, managed_turn_uuid=uid, output="x", is_error=False,
                                  error_text="", error_class="", backend_session_id="", raw_ndjson="")
        assert w._capture_late_managed_result("sess-Y", outcome) is False
    assert _spooled(w) == []


def test_R3_held_record_probe_is_rate_limited(db, tmp_path, monkeypatch):
    live = {"pid": __import__("os").getpid()}
    monkeypatch.setattr(agent_mod, "_execute_task", _crashing_backend(live))
    client = TestClient(ts.app)
    w1 = _worker(tmp_path, _ClientHTTP(client), incarnation="inc-old")
    _seed_turn(db, "t-r", "sess-r")
    try:
        _run_one(w1, "t-r")
    except Exception:
        pass
    http = _ClientHTTP(client)
    w2 = _worker(tmp_path, http, incarnation="inc-new")
    w2._held_probe_interval_sec = 3600.0
    for _ in range(5):
        asyncio.run(w2._reconcile_managed_claims())
    assert sum(1 for c in http.calls if c[1] == "/tasks/t-r/enter-recovery") == 1
    assert _claims(w2) == ["t-r"]


def test_R3_body_caps(db):
    c = TestClient(ts.app)
    big = b"x" * (9 * 1024 * 1024)
    # Legacy route: no managed cap applied (behavior unchanged).
    r = c.post("/tasks/zz/result", content=big, headers={**H, "content-type": "application/json"})
    assert r.status_code != 413

    def gen():
        for _ in range(10):
            yield b"y" * (1024 * 1024)
    r = c.post("/tasks/zz/result-managed", content=gen(), headers={**H, "content-type": "application/json"})
    assert r.status_code == 413
    r = c.post("/tasks/zz/claim-managed", content=b"{" + b" " * 20000 + b"}", headers={**H, "content-type": "application/json"})
    assert r.status_code == 413
    r = c.post("/tasks/zz/result-managed", content=b"x" * 10,
               headers={**H, "content-type": "application/json", "content-length": "abc"})
    assert r.status_code in (400, 413)
    # Unauthenticated: small body ⇒ 401/403; huge body ⇒ rejected by the cap first.
    r = c.post("/tasks/zz/result-managed", content=b"{}", headers={"content-type": "application/json"})
    assert r.status_code in (401, 403)
    r = c.post("/tasks/zz/result-managed", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_R3_dead_letter_retired_on_later_reconciler_ack(db, tmp_path, monkeypatch):
    """MINOR-2: the refused envelope is dead-lettered while the recovery POST
    fails; a LATER reconciler pass that gets the recovery acknowledged retires
    it (it leaves the budget)."""
    monkeypatch.setattr(agent_mod, "_execute_task", _FakeBackend())
    http = _ClientHTTP(TestClient(ts.app))
    http.faults["/result-managed"] = [422]
    http.faults["/enter-recovery"] = [503]
    w = _worker(tmp_path, http)
    _seed_turn(db, "d-2", "sd-2")
    _run_one(w, "d-2")
    assert len(list(w._result_spool.dead_dir.glob("*.json"))) == 1
    assert w._claim_store.get("d-2")["recovery_acked"] is False
    asyncio.run(w._reconcile_managed_claims())
    assert _row(db, "d-2")["status"] in ("recovery_required", "failed")
    assert len(list(w._result_spool.dead_dir.glob("*.json"))) == 0
    assert w._result_spool._retained_bytes() == 0
