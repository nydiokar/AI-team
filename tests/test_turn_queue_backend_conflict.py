"""[A82 step 4 rework, review round 1] Carrier/server seams shared by the
Codex and OpenCode managed contracts.

m2/m5 — a backend that refuses a turn BEFORE submit (``managed_conflict``:
native busy, an identity-less legacy owner, a held turn) must not requeue it
silently forever: the refusal reason is operator-visible on the row
(``blocked_reason``) and a repeated refusal moves the row back to ``queued``
under the existing blocked-head backoff (the ``carrier_offline`` pattern),
with a ``turn_queue_changed`` signal.

m3 — a late managed reply the carrier can no longer bind falls through to the
proactive-turn post with the session's REAL backend (not hard-coded claude).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from fastapi.testclient import TestClient

import src.control.task_server as ts
import src.worker.agent as agent_mod
from tests.test_turn_queue_carrier_integration import (  # noqa: F401  (fixtures)
    NODE, _ClientHTTP, _FakeBackend, _RecordingHTTP, _row, _run_one, _seed_turn, _worker, db,
)


class _Conflict(_FakeBackend):
    async def __call__(self, *a: Any, **k: Any) -> Dict[str, Any]:
        await super().__call__(*a, **k)
        return {"success": False, "output": "", "execution_time": 0.0,
                "errors": ["not_submitted: OwnershipConflictError: codex_thread_busy"],
                "error_class": "managed_conflict"}


def test_m2_backend_conflict_is_visible_and_repeated_conflict_backs_off(db, tmp_path, monkeypatch):
    from src.control import turn_queue

    signals: List[tuple] = []
    real_emit = turn_queue.emit_turn_queue_changed
    monkeypatch.setattr(turn_queue, "emit_turn_queue_changed",
                        lambda sid, change, **kw: (signals.append((sid, change, kw.get("status"))),
                                                   real_emit(sid, change, **kw)))
    monkeypatch.setattr(agent_mod, "_execute_task", _Conflict())
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    _seed_turn(db, "t-c", "sess-c")

    _run_one(w, "t-c")
    row = _row(db, "t-c")
    assert row["status"] == "pending" and row["claim_token"] is None, "first refusal: retried at once"
    assert row["blocked_reason"] == "backend_conflict: not_submitted: OwnershipConflictError: codex_thread_busy"
    assert row["blocked_attempts"] == 1

    _run_one(w, "t-c")
    _run_one(w, "t-c")
    row = _row(db, "t-c")
    assert row["status"] == "queued", "a repeated refusal must not loop claim→refuse silently"
    assert row["blocked_reason"].startswith("backend_conflict: ") and row["blocked_until"]
    assert row["prompt"] or row["payload"], "the prompt is preserved"
    assert ("sess-c", "released", "queued") in signals


def test_m2_release_without_a_reason_keeps_the_unblocked_contract(db, tmp_path):
    """A not-invoked release with no backend refusal reason (e.g. a lost start
    response) is unchanged: back to pending, no blocked reason."""
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))  # registers the incarnation
    _seed_turn(db, "t-n", "sess-n")
    token = db.claim_turn("t-n", w.cfg.node_id, "worker", w._incarnation_id)
    assert db.release_turn("t-n", token, backend_not_invoked=True, node_id=w.cfg.node_id)
    row = _row(db, "t-n")
    assert row["status"] == "pending" and row["blocked_reason"] is None and row["blocked_attempts"] == 0


def test_m3_uncaptured_late_reply_posts_proactive_turn_with_the_real_backend(tmp_path):
    http = _RecordingHTTP()
    w = _worker(tmp_path, http, managed=True)
    sinks: Dict[str, Any] = {}

    class _Backend:
        def __init__(self, name: str) -> None:
            self.name = name

        def set_proactive_sink(self, sink: Any) -> None:
            sinks[self.name] = sink

    w._backends = {"codex": _Backend("codex"), "opencode-server": _Backend("opencode-server")}
    w._setup_proactive_delivery()
    late = SimpleNamespace(late_managed=True, managed_turn_uuid="uuid-unknown", output="late text",
                           is_error=False, error_text="", backend_session_id="thr-1", raw_ndjson="")
    sinks["codex"]("sess-x", late)
    sinks["opencode-server"]("sess-y", late)
    posts = {c[2]["session_id"]: c[2]["backend"] for c in http.calls if "proactive-turn" in c[1]}
    assert posts == {"sess-x": "codex", "sess-y": "opencode-server"}


# =========================================================================== #
# [A82 pre-cutover backend carries] m2 remainder — the carrier's PRE-START
# not-quiescent release is visible + backs off like a run-time refusal (with a
# backend-supplied reason when there is one), and the backoff GROWS across
# conflict-release / re-activation cycles for the same reason.
# =========================================================================== #
class _Busy:
    """A managed-capable backend whose session never reads quiescent."""

    def __init__(self, reason: str | None) -> None:
        self.reason = reason

    def supports_managed_turns(self) -> bool:
        return True

    def is_quiescent(self, session: Any) -> bool:
        return False

    def quiescence_reason(self, session: Any) -> str | None:
        return self.reason


class _BusyNoReason:
    def supports_managed_turns(self) -> bool:
        return True

    def is_quiescent(self, session: Any) -> bool:
        return False


def _backoff_sec(row: Dict[str, Any]) -> float:
    from datetime import datetime, timezone

    return (datetime.fromisoformat(row["blocked_until"]) - datetime.now(timezone.utc)).total_seconds()


def _reactivate(db, task_id: str) -> None:
    head = _row(db, task_id)
    srev = db.get_session(head["session_id"])["config_revision"]
    db._conn().execute("UPDATE mesh_tasks SET blocked_until = NULL WHERE id = ?", (task_id,))
    assert db.activate_prepared_turn(
        task_id, expected_revision=int(head["revision"]), expected_config_revision=int(srev),
        action="resume_session", payload={"task_id": task_id, "prompt": "frozen prompt"},
        machine_id=NODE,
    ) == "activated"


def test_m2_pre_start_not_quiescent_release_carries_the_backend_reason_and_backs_off(db, tmp_path, monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(agent_mod, "_execute_task", backend)
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    w._backends = {"claude": _Busy("codex_owner_not_provably_gone")}
    _seed_turn(db, "t-q", "sess-q")

    _run_one(w, "t-q")
    row = _row(db, "t-q")
    assert row["status"] == "pending" and row["claim_token"] is None
    assert row["blocked_reason"] == "backend_conflict: session_not_quiescent: codex_owner_not_provably_gone"
    assert row["blocked_attempts"] == 1
    _run_one(w, "t-q")
    _run_one(w, "t-q")
    row = _row(db, "t-q")
    assert row["status"] == "queued" and row["blocked_until"], "a wedged session must back off, not spin"
    assert backend.rows == [], "the backend was never invoked"


def test_m2_pre_start_not_quiescent_without_a_backend_reason_is_still_visible(db, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_execute_task", _FakeBackend())
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    w._backends = {"claude": _BusyNoReason()}
    _seed_turn(db, "t-r", "sess-r")
    _run_one(w, "t-r")
    row = _row(db, "t-r")
    assert row["status"] == "pending" and row["blocked_reason"] == "backend_conflict: session_not_quiescent"


def test_m2_backoff_grows_across_reactivation_for_the_same_reason_and_resets_on_progress(db, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_execute_task", _Conflict())
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    _seed_turn(db, "t-g", "sess-g")
    for _ in range(3):
        _run_one(w, "t-g")
    row = _row(db, "t-g")
    assert row["status"] == "queued" and row["blocked_attempts"] == 3
    first = _backoff_sec(row)

    _reactivate(db, "t-g")
    row = _row(db, "t-g")
    assert row["status"] == "pending" and row["blocked_attempts"] == 3, "activation must not reset the count"
    _run_one(w, "t-g")
    row = _row(db, "t-g")
    assert row["status"] == "queued" and row["blocked_attempts"] == 4
    assert _backoff_sec(row) > first + 5, "the backoff never grew across re-activation"

    # Real progress (the turn runs and completes) resets the refusal record.
    monkeypatch.setattr(agent_mod, "_execute_task", _FakeBackend())
    _reactivate(db, "t-g")
    _run_one(w, "t-g")
    row = _row(db, "t-g")
    assert row["status"] == "completed"
    assert row["blocked_attempts"] == 0 and row["blocked_reason"] is None


def test_m2_a_different_refusal_reason_restarts_the_count(db, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_execute_task", _Conflict())
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    _seed_turn(db, "t-d", "sess-d")
    _run_one(w, "t-d")
    _run_one(w, "t-d")
    assert _row(db, "t-d")["blocked_attempts"] == 2
    w._backends = {"claude": _Busy("held_turn")}
    _run_one(w, "t-d")
    row = _row(db, "t-d")
    assert row["status"] == "pending" and row["blocked_attempts"] == 1
    assert row["blocked_reason"] == "backend_conflict: session_not_quiescent: held_turn"
