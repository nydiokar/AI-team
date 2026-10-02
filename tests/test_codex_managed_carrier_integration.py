"""[A82 step 4a] Codex through the REAL carrier managed path.

Real pieces: file-backed ``MeshDB``, the in-process task server, the REAL
``WorkerAgent`` (`_fetch_pending` → `_handle_task` → claim/start → backend →
`/result-managed` | `/enter-recovery` → reconciler `/quiescence`), the REAL
``CodexBackend`` + ``CodexAppServerClient``, and the real-protocol fake
app-server from ``test_codex_managed_turns``. No paid CLI is reachable.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import src.control.task_server as ts
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from tests.test_codex_managed_turns import h, managed_rows, owners  # noqa: F401  (fixture)
from tests.test_turn_queue_carrier_integration import (  # noqa: F401  (db fixture)
    NODE, NOW, _ClientHTTP, _row, _sess, _worker, db,
)


def _codex_carrier(tmp_path, http, backend, incarnation: str = "inc-1"):
    w = _worker(tmp_path, http, incarnation=incarnation)
    w.cfg.backends = ["codex"]
    w._backends = {"codex": backend}
    w._register()  # the generic probe now advertises codex (no name branching)
    return w


def _seed(db: MeshDB, h, task_id: str, sid: str = "sess-c", prompt: str = "do it") -> None:
    if db._conn().execute("SELECT 1 FROM sessions WHERE session_id = ?", (sid,)).fetchone() is None:
        db.upsert_session(Session(session_id=sid, backend="codex", repo_path=h.repo, status=SessionStatus.IDLE,
                                  created_at=NOW, updated_at=NOW, machine_id=NODE))
        db.enroll_session(sid)
    db.enqueue_turn(
        task_id=task_id, session_id=sid, backend="codex", action="resume_session",
        payload={"task_id": task_id, "prompt": prompt,
                 "session": {"session_id": sid, "backend": "codex", "repo_path": h.repo}},
        turn_source="human", turn_kind="instruction", machine_id=NODE,
    )
    db.activate_turn(task_id)


def _run(w, task_id: str) -> None:
    async def scenario():
        rows = await w._fetch_pending()
        await w._handle_task([r for r in rows if r["id"] == task_id][0])

    asyncio.run(scenario())


def test_codex_managed_turn_end_to_end_commits_native_thread(db, h):
    http = _ClientHTTP(TestClient(ts.app))
    w = _codex_carrier(h.tmp_path, http, h.backend)
    assert w._managed_backends() == ["codex"]
    _seed(db, h, "t-c1")
    _run(w, "t-c1")

    row = _row(db, "t-c1")
    assert row["status"] == "completed", row
    assert json.loads(row["result"])["output"] == "native answer"
    thread_id = _sess(db, "sess-c")["backend_session_id"]
    assert thread_id.startswith("thr-")
    # The carrier's turn uuid reached the native protocol and the write-ahead map.
    start = h.requests("turn/start")[0]
    rec = managed_rows(h.home)[0]
    assert start["params"]["clientUserMessageId"] == rec["turn_uuid"]
    assert rec["state"] == "completed" and rec["thread_id"] == thread_id
    assert h.requests("turn/interrupt") == []
    assert db.get_active_turn("sess-c") is None and owners(h.home) == []
    # Second turn resumes the exact native thread.
    _seed(db, h, "t-c2", prompt="again")
    _run(w, "t-c2")
    assert _row(db, "t-c2")["status"] == "completed"
    assert h.requests("thread/resume") == [] or all(
        r["params"]["threadId"] == thread_id for r in h.requests("thread/resume"))
    assert len(h.requests("thread/start")) == 1


def test_codex_unattributable_turn_held_then_resolved_by_native_quiescence(db, h):
    http = _ClientHTTP(TestClient(ts.app))
    w = _codex_carrier(h.tmp_path, http, h.backend)
    h.ctl(foreign_event=True)
    _seed(db, h, "t-r1")
    _run(w, "t-r1")
    assert _row(db, "t-r1")["status"] == "recovery_required"
    assert h.requests("turn/interrupt") == []
    rec = w._claim_store.get("t-r1")
    assert rec and rec["invoked"] and rec["backend_identity"]["pid"] == h.requests("turn/start")[0]["pid"]

    # Native turn still active on the live app-server: the reconciler holds.
    asyncio.run(w._reconcile_managed_claims())
    assert _row(db, "t-r1")["status"] == "recovery_required"
    # Native status settles: backend-quiescent evidence resolves it (never success).
    h.ctl(read_status="idle")
    asyncio.run(w._reconcile_managed_claims())
    assert _row(db, "t-r1")["status"] == "failed"
    assert db.get_active_turn("sess-c") is None
    assert owners(h.home) == []


def test_codex_crash_before_turn_start_response_successor_resolves_with_process_proof(db, h):
    """Carrier A's app-server dies between submit and the turn/start response:
    recovery_required (never success, never re-submitted). Successor carrier B
    (new incarnation) resolves it ONLY by proof that the recorded app-server is
    gone, then runs the next turn on the same native thread."""
    from src.backends.codex_native import CodexBackend

    http = _ClientHTTP(TestClient(ts.app))
    a = _codex_carrier(h.tmp_path, http, h.backend, incarnation="inc-a")
    h.ctl(die_before_response=True)
    _seed(db, h, "t-x1")
    _run(a, "t-x1")
    assert _row(db, "t-x1")["status"] == "recovery_required"
    assert a._claim_store.get("t-x1")["backend_identity"]["pid"]

    h.ctl()
    b = _codex_carrier(h.tmp_path, http, h.make(), incarnation="inc-b")
    asyncio.run(b._reconcile_managed_claims())
    assert _row(db, "t-x1")["status"] == "failed"
    assert len(h.requests("turn/start")) == 1, "the uncertain prompt was never re-submitted"
    _seed(db, h, "t-x2", prompt="next")
    _run(b, "t-x2")
    assert _row(db, "t-x2")["status"] == "completed"
    assert isinstance(b._backends["codex"], CodexBackend)
