"""
Integration test for Phase 9 Step B: mesh routing wired into process_task.

Validates:
  1. Local task (no machine_id) -> self-claimed, invisible to workers
  2. Remote task (machine_id set) -> stays pending, only visible to pinned worker
  3. Worker claim/complete cycle with backend_session_id propagation
  4. _dispatch_to_node reads result and updates session.backend_session_id
  5. DB unavailable -> dispatch fails loudly, no silent local fallback
  6. Node offline -> _process_task_remote fails loudly, no silent local fallback

Run: python scripts/test_routing_integration.py
"""
import asyncio
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

TEST_DB = Path(__file__).resolve().parent.parent / "state" / "test_routing.db"
for ext in ("", "-wal", "-shm"):
    p = Path(str(TEST_DB) + ext)
    if p.exists():
        p.unlink()

os.environ["WORKER_TOKEN"] = "rtest-token-99"
os.environ["MESH_DB_PATH"] = str(TEST_DB)
os.environ["MESH_SHADOW_WRITE"] = "true"
os.environ["MESH_ENABLED"] = "true"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.control.db import MeshDB
from src.core.interfaces import (
    Session, SessionStatus, Task, TaskType, TaskPriority, TaskStatus, TaskResult
)

db = MeshDB(str(TEST_DB))
HOST = socket.gethostname()
NOW = datetime.now().isoformat()

FAILURES = []

def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    suffix = f" — {detail}" if detail and not cond else ""
    print(f"[{status}] {label}{suffix}")
    if not cond:
        FAILURES.append(label)


def make_session(session_id, machine_id="", backend_session_id=""):
    s = Session(
        session_id=session_id,
        backend="claude",
        repo_path="/tmp/testrepo",
        status=SessionStatus.AWAITING_INPUT,
        created_at=NOW,
        updated_at=NOW,
        machine_id=machine_id,
        backend_session_id=backend_session_id,
        telegram_chat_id=9999,
    )
    return s


def make_task(task_id, session_id=None, prompt="hello"):
    t = Task(
        id=task_id,
        type=TaskType.FIX,
        priority=TaskPriority.MEDIUM,
        status=TaskStatus.PENDING,
        created=NOW,
        title="test task",
        target_files=[],
        prompt=prompt,
        success_criteria=[],
        context="",
        metadata={"session_id": session_id} if session_id else {},
    )
    return t


def enqueue(task_id, session_id, machine_id, backend_session_id=""):
    """Insert session + task into the test DB."""
    if session_id:
        sess = make_session(session_id, machine_id=machine_id, backend_session_id=backend_session_id)
        db.upsert_session(sess)
    db.enqueue_task(
        task_id=task_id,
        session_id=session_id,
        machine_id=machine_id or None,
        backend="claude",
        action="resume_session" if backend_session_id else "create_session",
        payload={
            "prompt": "hello",
            "task_id": task_id,
            "action": "resume_session" if backend_session_id else "create_session",
            "metadata": {"session_id": session_id} if session_id else {},
            **({"session": {
                "session_id": session_id,
                "backend": "claude",
                "repo_path": "/tmp/testrepo",
                "backend_session_id": backend_session_id,
                "machine_id": machine_id,
                "telegram_chat_id": 9999,
                "telegram_thread_id": None,
                "owner_user_id": 9999,
                "last_user_message": "hello",
            }} if session_id else {}),
        },
    )


print("=== Mesh routing integration test ===\n")

# ---------------------------------------------------------------------------
# 1. Local task (no machine_id) -> self-claim
# ---------------------------------------------------------------------------
enqueue("task_local_01", "sess_local_01", machine_id="")
db.claim_task("task_local_01", HOST)  # simulates _mesh_enqueue_task self-claim

row = db.get_task("task_local_01")
check("local task self-claimed (status=claimed)", row and row["status"] == "claimed")

pending = db.get_pending_tasks(node_id="some-worker", backends=["claude"])
check("self-claimed local task invisible to workers",
      not any(t["id"] == "task_local_01" for t in pending))

# ---------------------------------------------------------------------------
# 2. Remote task (machine_id set) -> stays pending, no self-claim
# ---------------------------------------------------------------------------
enqueue("task_remote_01", "sess_remote_01", machine_id="remote-worker-01")

row2 = db.get_task("task_remote_01")
check("remote task inserted as pending", row2 and row2["status"] == "pending",
      f"status={row2['status'] if row2 else 'NONE'}")
check("remote task machine_id correct",
      row2 and row2.get("machine_id") == "remote-worker-01")

# Only pinned worker can poll it
pending_correct = db.get_pending_tasks(node_id="remote-worker-01", backends=["claude"])
check("pinned task visible to assigned worker",
      any(t["id"] == "task_remote_01" for t in pending_correct))

pending_other = db.get_pending_tasks(node_id="other-node", backends=["claude"])
check("pinned task NOT visible to other workers",
      not any(t["id"] == "task_remote_01" for t in pending_other))

# ---------------------------------------------------------------------------
# 3. Worker claim/complete with backend_session_id
# ---------------------------------------------------------------------------
check("worker can claim pinned task", db.claim_task("task_remote_01", "remote-worker-01"))
check("double-claim rejected", not db.claim_task("task_remote_01", "other-node"))

row3 = db.get_task("task_remote_01")
check("task status = claimed after worker claim", row3 and row3["status"] == "claimed")
check("claimed_by = remote worker", row3 and row3.get("claimed_by") == "remote-worker-01")

NEW_BSID = "claude-conv-abc123"
db.complete_task("task_remote_01", {
    "success": True,
    "output": "Hello from remote!",
    "errors": [],
    "files_modified": ["src/foo.py"],
    "execution_time": 2.5,
    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    "return_code": 0,
    "backend_session_id": NEW_BSID,
}, artifact_path=None)

row4 = db.get_task("task_remote_01")
check("task marked completed", row4 and row4["status"] == "completed")
stored = json.loads(row4["result"] or "{}") if row4 else {}
check("backend_session_id stored in DB result",
      stored.get("backend_session_id") == NEW_BSID,
      f"got {stored.get('backend_session_id')!r}")

# Completed task not pollable
pending_done = db.get_pending_tasks(node_id="remote-worker-01", backends=["claude"])
check("completed task no longer pollable",
      not any(t["id"] == "task_remote_01" for t in pending_done))

# ---------------------------------------------------------------------------
# 4. _dispatch_to_node reads completed row + propagates backend_session_id
# ---------------------------------------------------------------------------
NEW_BSID2 = "claude-conv-xyz789"
enqueue("task_remote_poll", "sess_poll_01", machine_id="remote-worker-01")
db.claim_task("task_remote_poll", "remote-worker-01")
db.complete_task("task_remote_poll", {
    "success": True,
    "output": "Task done on remote node.",
    "errors": [],
    "files_modified": ["src/bar.py"],
    "execution_time": 3.1,
    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    "return_code": 0,
    "backend_session_id": NEW_BSID2,
}, artifact_path=None)

async def test_dispatch_to_node():
    fake_session = make_session("sess_poll_01", machine_id="remote-worker-01")
    saves = []

    class StubStore:
        def get(self, sid): return fake_session
        def save(self, s): saves.append(s.backend_session_id)

    class MinimalOrch:
        session_store = StubStore()
        def _resolve_task_backend(self, t): return "claude"

    orch = MinimalOrch()
    from src.orchestrator import TaskOrchestrator
    bound = TaskOrchestrator._dispatch_to_node.__get__(orch, type(orch))
    task = make_task("task_remote_poll", session_id="sess_poll_01")

    with patch("src.control.db.get_db", return_value=db):
        result = await bound(task, fake_session, node=None)

    return result, saves, fake_session

result_d, saves_d, sess_d = asyncio.run(test_dispatch_to_node())
check("_dispatch_to_node returns success", result_d.success is True)
check("_dispatch_to_node output correct", result_d.output == "Task done on remote node.")
check("_dispatch_to_node files_modified propagated", result_d.files_modified == ["src/bar.py"])
check("backend_session_id applied to session object",
      sess_d.backend_session_id == NEW_BSID2, f"got {sess_d.backend_session_id!r}")
check("session_store.save called with new bsid", NEW_BSID2 in saves_d, f"saves={saves_d}")

# ---------------------------------------------------------------------------
# 5. DB unavailable -> fail loudly, no local fallback
# ---------------------------------------------------------------------------
async def test_db_unavailable():
    fake_session = make_session("sess_nodb", machine_id="remote-worker-01")

    class StubStore:
        def get(self, sid): return fake_session
        def save(self, s): pass

    class MinimalOrch:
        session_store = StubStore()
        def _resolve_task_backend(self, t): return "claude"

    orch = MinimalOrch()
    from src.orchestrator import TaskOrchestrator
    bound = TaskOrchestrator._dispatch_to_node.__get__(orch, type(orch))
    task = make_task("task_nodb")

    with patch("src.control.db.get_db", return_value=None):
        result = await bound(task, fake_session, node=None)
    return result

result_nodb = asyncio.run(test_db_unavailable())
check("DB unavailable -> result is failure", result_nodb.success is False)
check("DB unavailable -> error mentions 'Mesh DB'",
      any("Mesh DB" in e for e in (result_nodb.errors or [])))

# ---------------------------------------------------------------------------
# 6. Node offline -> _process_task_remote fails loudly
# ---------------------------------------------------------------------------
async def test_node_offline():
    from src.control.node_registry import NodeRegistry

    fake_session = make_session("sess_offline", machine_id="missing-node-99",
                                backend_session_id="prev-bsid")
    saves = []

    class StubStore:
        def get(self, sid): return fake_session
        def save(self, s): saves.append(str(s.status))

    empty_registry = NodeRegistry.__new__(NodeRegistry)
    empty_registry._nodes = {}

    class MinimalOrch:
        session_store = StubStore()
        telegram_interface = None
        _task_cancel_events = {}
        def _classify_error(self, r): return "fatal"
        def _emit_event(self, *a, **kw): pass

    orch = MinimalOrch()
    from src.orchestrator import TaskOrchestrator
    bound = TaskOrchestrator._process_task_remote.__get__(orch, type(orch))
    task = make_task("task_offline", session_id="sess_offline")

    with patch("src.control.node_registry.get_registry", return_value=empty_registry):
        result = await bound(task, fake_session, start_time=time.time(), timeout_s=60)

    return result, saves, fake_session

result_off, saves_off, sess_off = asyncio.run(test_node_offline())
check("offline node -> failure result", result_off.success is False)
check("offline node -> 'offline' in error",
      any("offline" in e.lower() for e in (result_off.errors or [])))
check("offline node -> no local fallback (error_class=fatal)",
      getattr(result_off, "error_class", None) == "fatal")
check("offline node -> session status = error",
      "error" in str(sess_off.status).lower(), f"got {sess_off.status!r}")

# ---------------------------------------------------------------------------
# 7. Missing row on first poll -> fast-fail, not 600s timeout
# ---------------------------------------------------------------------------
async def test_missing_row_fast_fail():
    fake_session = make_session("sess_missing_row", machine_id="remote-worker-01")

    class StubStore:
        def get(self, sid): return fake_session
        def save(self, s): pass

    class MinimalOrch:
        session_store = StubStore()
        def _resolve_task_backend(self, t): return "claude"

    orch = MinimalOrch()
    from src.orchestrator import TaskOrchestrator
    bound = TaskOrchestrator._dispatch_to_node.__get__(orch, type(orch))
    # Use a task_id that was never enqueued — row will be missing on first poll
    task = make_task("task_never_enqueued", session_id="sess_missing_row")

    with patch("src.control.db.get_db", return_value=db):
        t0 = time.time()
        result = await bound(task, fake_session, node=None)
        elapsed = time.time() - t0

    return result, elapsed

result_missing, elapsed_missing = asyncio.run(test_missing_row_fast_fail())
check("missing row -> failure result", result_missing.success is False)
check("missing row -> error mentions enqueue failure",
      any("enqueue" in e.lower() for e in (result_missing.errors or [])))
check("missing row -> fast-fail (not 600s timeout)", elapsed_missing < 5.0,
      f"took {elapsed_missing:.1f}s")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if FAILURES:
    print(f"=== {len(FAILURES)} CHECK(S) FAILED: {FAILURES} ===")
else:
    print("=== All routing integration checks passed ===")

db.close()
for ext in ("", "-wal", "-shm"):
    p = Path(str(TEST_DB) + ext)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass

if FAILURES:
    sys.exit(1)
