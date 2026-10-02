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
    _ClientHTTP, _FakeBackend, _RecordingHTTP, _row, _run_one, _seed_turn, _worker, db,
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
