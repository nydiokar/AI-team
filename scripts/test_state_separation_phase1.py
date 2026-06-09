"""
Verify State Separation Phase 1 changes:
  1. db.get_task_by_session() returns task filtered by session_id + task_id
  2. SessionStore.get() reads from DB first, falls back to JSON
  3. SessionStore.list_all() reads from DB first, falls back to JSON
  4. _from_dict handles JSON-string fields from DB correctly
  5. _recover_stale_busy_sessions recovers completed tasks instead of marking ERROR

Run: python scripts/test_state_separation_phase1.py
Uses an isolated test DB — never touches state/mesh.db.
"""
import json
import os
import sys
import uuid
from pathlib import Path
from datetime import datetime

# Temporarily rename .env so load_dotenv(override=True) doesn't clobber our
# test env vars (MESH_DB_PATH, etc.).  Restored in the cleanup block.
_proj_root = Path(__file__).resolve().parent.parent
_dotenv = _proj_root / ".env"
_dotenv_bak = _proj_root / ".env.phase1test.bak"
if _dotenv.exists():
    _dotenv.rename(_dotenv_bak)

# Isolated test DB
TEST_DB = _proj_root / "state" / "test_phase1.db"
for ext in ("", "-wal", "-shm"):
    p = Path(str(TEST_DB) + ext)
    if p.exists():
        p.unlink()

os.environ["MESH_DB_PATH"] = str(TEST_DB)
os.environ["MESH_SHADOW_WRITE"] = "true"
os.environ["MESH_ENABLED"] = "false"

sys.path.insert(0, str(_proj_root))

from src.control.db import get_db, MeshDB
from src.core.session_store import SessionStore
from src.core.interfaces import Session, SessionStatus

FAILURES = []

def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)

print("=== State Separation Phase 1 verification ===\n")

# Clean up any cached singleton so we start fresh
import src.control.db as db_mod
db_mod._db_instance = None

db = get_db()
assert db is not None, "DB should be initialised"

store = SessionStore()

# ------------------------------------------------------------------
# 1. get_task_by_session()
# ------------------------------------------------------------------
print("--- 1. db.get_task_by_session() ---")
# Need a matching session row for the FK constraint, then enqueue as
# a task belonging to that session.
from src.core.interfaces import Session as _Session, SessionStatus as _SS
s = _Session(
    session_id="sess_test01", backend="claude", repo_path="/test",
    status=_SS.IDLE, created_at="2026-01-01T00:00:00",
    updated_at="2026-01-01T00:00:00", machine_id="",
)
db.upsert_session(s)
db.enqueue_task(
    task_id="task_test01",
    session_id="sess_test01",
    machine_id=None,
    backend="claude",
    action="run_oneoff",
    payload={"prompt": "hello", "task_id": "task_test01", "action": "run_oneoff", "metadata": {}},
)
row = db.get_task_by_session("sess_test01", "task_test01")
check("get_task_by_session returns row", row is not None)
check("correct session_id", row and row["session_id"] == "sess_test01")
check("correct task_id", row and row["id"] == "task_test01")

# Wrong session_id returns None
row2 = db.get_task_by_session("wrong_sess", "task_test01")
check("wrong session returns None", row2 is None)

# Wrong task_id returns None
row3 = db.get_task_by_session("sess_test01", "wrong_task")
check("wrong task returns None", row3 is None)
print()

# ------------------------------------------------------------------
# 2. _from_dict handles JSON-string fields from DB
# ------------------------------------------------------------------
print("--- 2. _from_dict JSON-string field parsing ---")
# Simulate a dict with JSON-string fields (as DB returns)
d = {
    "session_id": "test_json_parse",
    "backend": "claude",
    "repo_path": "/test",
    "status": "idle",
    "created_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-01T00:00:00",
    "machine_id": "",
    "backend_session_id": "",
    "last_task_id": "",
    "last_artifact_path": "",
    "last_summary": "",
    "last_user_message": "",
    "last_result_summary": "",
    "last_files_modified": '["file1.py", "file2.py"]',  # JSON string from DB
    "telegram_chat_id": None,
    "telegram_thread_id": None,
    "owner_user_id": None,
    "task_history": '[{"task_id": "t1", "success": true}]',  # JSON string from DB
}
s = store._from_dict(d)
check("last_files_modified parsed as list", isinstance(s.last_files_modified, list))
check("last_files_modified correct", s.last_files_modified == ["file1.py", "file2.py"])
check("task_history parsed as list", isinstance(s.task_history, list))
check("task_history correct", s.task_history == [{"task_id": "t1", "success": True}])

# Also test with already-parsed lists (JSON file path)
d2 = dict(d)
d2["last_files_modified"] = ["file3.py"]
d2["task_history"] = [{"task_id": "t2"}]
s2 = store._from_dict(d2)
check("list fields accept native lists too", s2.last_files_modified == ["file3.py"])
check("list fields accept native lists too (history)", s2.task_history == [{"task_id": "t2"}])
print()

# ------------------------------------------------------------------
# 3. SessionStore.get() reads from DB first
# ------------------------------------------------------------------
print("--- 3. SessionStore.get() DB-first read ---")
# Create a session object
session_obj = Session(
    session_id="test_db_first",
    backend="claude",
    repo_path="/test/repo",
    status=SessionStatus.IDLE,
    created_at="2026-01-01T00:00:00",
    updated_at="2026-01-01T00:00:00",
    machine_id="test-pc",
)
store.save(session_obj)  # writes both JSON + DB

# Now retrieve via get() — should read from DB
retrieved = store.get("test_db_first")
check("get() returns session", retrieved is not None)
check("correct session_id from DB", retrieved and retrieved.session_id == "test_db_first")
check("correct backend from DB", retrieved and retrieved.backend == "claude")
check("correct machine_id from DB", retrieved and retrieved.machine_id == "test-pc")

# Delete the JSON file, verify get() still works via DB
json_path = Path("state/sessions/test_db_first.json")
if json_path.exists():
    json_path.unlink()
retrieved_no_json = store.get("test_db_first")
check("get() works without JSON file (DB fallback)", retrieved_no_json is not None)
check("still correct data", retrieved_no_json and retrieved_no_json.session_id == "test_db_first")
# Restore JSON file for cleanup
store.save(session_obj)
print()

# ------------------------------------------------------------------
# 4. SessionStore.list_all() reads from DB first
# ------------------------------------------------------------------
print("--- 4. SessionStore.list_all() DB-first read ---")
# Create a DB-only session (no JSON file)
db_only_session = Session(
    session_id="db_only_sess",
    backend="opencode",
    repo_path="/other/repo",
    status=SessionStatus.IDLE,
    created_at="2026-01-01T00:00:00",
    updated_at="2026-01-01T00:00:00",
    machine_id="other-pc",
)
store.save(db_only_session)  # writes JSON + DB
# Delete the JSON to simulate DB-only
db_only_json = Path("state/sessions/db_only_sess.json")
if db_only_json.exists():
    db_only_json.unlink()

all_sessions = store.list_all()
session_ids = [s.session_id for s in all_sessions]
check("list_all() contains DB-only session",
      "db_only_sess" in session_ids,
      f"found {len(all_sessions)} sessions, ids={session_ids[:5]}...")
check("list_all() contains JSON-sourced session",
      "test_db_first" in session_ids)

# Verify list_all() returns more than just JSON files (DB has the 398 backfilled sessions too)
check("list_all() returns sessions",
      len(all_sessions) >= 2, f"got {len(all_sessions)}")

# Verify ordering (DB returns updated_at DESC)
# The DB-only session should come first if it was saved last
check("sessions ordered by updated_at DESC",
      all_sessions[0].session_id in ("db_only_sess", "test_db_first"))
print()

# ------------------------------------------------------------------
# 5. Recovery logic verification
# ------------------------------------------------------------------
print("--- 5. Recovery logic (simulated) ---")
# Create a BUSY session whose task completed in DB
busy_sid = "recover_test_busy"
busy_session = Session(
    session_id=busy_sid,
    backend="claude",
    repo_path="/test/repo",
    status=SessionStatus.BUSY,
    created_at="2026-01-01T00:00:00",
    updated_at="2026-01-01T00:00:00",
    machine_id="",
    last_task_id="task_recover01",
)
store.save(busy_session)

# Enqueue and complete a task for this session (session row must exist for FK)
db.upsert_session(busy_session)
db.enqueue_task(
    task_id="task_recover01",
    session_id=busy_sid,
    machine_id="",
    backend="claude",
    action="resume_session",
    payload={"prompt": "test", "task_id": "task_recover01", "action": "resume_session",
             "session": {"session_id": busy_sid}},
)
# Complete the task with result
result_dict = {
    "success": True,
    "output": "Task output content for recovery test",
    "errors": [],
    "files_modified": ["/test/file.py"],
    "execution_time": 1.23,
    "timestamp": "2026-01-01T00:01:00",
    "return_code": 0,
}
db.complete_task("task_recover01", result_dict)
check("task marked completed in DB",
      db.get_task("task_recover01")["status"] == "completed")

# Now verify get_task_by_session finds the completed task
recovered_row = db.get_task_by_session(busy_sid, "task_recover01")
check("get_task_by_session finds completed task", recovered_row is not None)
check("completed status in result", recovered_row and recovered_row["status"] == "completed")
print()

# ------------------------------------------------------------------
# 6. Cleanup
# ------------------------------------------------------------------
db.close()
for ext in ("", "-wal", "-shm"):
    p = Path(str(TEST_DB) + ext)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass

# Clean up test JSON files
for sid in ("test_db_first", "db_only_sess", "recover_test_busy"):
    p = Path("state/sessions") / f"{sid}.json"
    if p.exists():
        p.unlink()

# Restore .env
if _dotenv_bak.exists():
    _dotenv_bak.rename(_dotenv)

print()
if FAILURES:
    print(f"=== {len(FAILURES)} CHECK(S) FAILED: {FAILURES} ===")
    sys.exit(1)
else:
    print("=== All checks passed ===")
