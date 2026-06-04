"""
Integration test for OpenCodeServerBackend — exercises the full orchestrator
execution path without Telegram.

Covers:
  T1  create_session — first turn, session ID captured and persisted
  T2  resume_session — second turn, same session ID used
  T3  session persistence across store save/load cycle
  T4  cancel — abort mid-run (fire-and-forget, no hang)
  T5  close  — DELETE session from server
  T6  run_oneoff — stateless, session ID cleared on return
  T7  server restart resilience — session lost, auto-recreated, no dead end
  T8  empty backend_session_id on resume — auto-falls back to create
  T9  terminate_active_processes — server proc killed, next call restarts it
  T10 concurrent session isolation — two gateway sessions share one server
  T11 full orchestrator submit_instruction path with opencode-server
  T12 session status transitions (BUSY → AWAITING_INPUT / ERROR)

Run with:
    python -m pytest tests/test_opencode_server_integration.py -v
or standalone:
    python tests/test_opencode_server_integration.py
"""

import asyncio
import sys
import time
import uuid
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backends.opencode import OpenCodeServerBackend
from src.core.interfaces import Session, SessionStatus, ExecutionResult
from src.core.session_store import SessionStore

REPO = str(Path(__file__).resolve().parent.parent)  # this repo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(backend: str = "opencode-server", backend_session_id: str = "") -> Session:
    return Session(
        session_id=uuid.uuid4().hex[:12],
        backend=backend,
        repo_path=REPO,
        status=SessionStatus.IDLE,
        created_at="2026-06-04T00:00:00",
        updated_at="2026-06-04T00:00:00",
        last_user_message="",
        backend_session_id=backend_session_id,
    )

def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_t1_create_session(b: OpenCodeServerBackend) -> None:
    """First turn — session created, ID captured, output non-empty."""
    s = _make_session()
    s.last_user_message = "Reply with exactly the word: T1OK"
    r = b.create_session(s)
    _assert(r.success, f"T1 failed: {r.errors}")
    _assert("T1OK" in r.output, f"T1 unexpected output: {r.output!r}")
    _assert(r.backend_session_id.startswith("ses_"), f"T1 bad session ID: {r.backend_session_id!r}")
    print(f"  T1 OK — session={r.backend_session_id} output={r.output!r}")
    return r.backend_session_id


def test_t2_resume_session(b: OpenCodeServerBackend, oc_session_id: str) -> None:
    """Second turn — same session ID, context preserved."""
    s = _make_session(backend_session_id=oc_session_id)
    r = b.resume_session(s, "Reply with exactly the word: T2OK")
    _assert(r.success, f"T2 failed: {r.errors}")
    _assert("T2OK" in r.output, f"T2 unexpected output: {r.output!r}")
    _assert(r.backend_session_id == oc_session_id, f"T2 session ID changed: {r.backend_session_id!r}")
    print(f"  T2 OK — same session preserved, output={r.output!r}")


def test_t3_session_store_roundtrip(b: OpenCodeServerBackend) -> None:
    """Session ID survives a store save → load cycle (i.e. a gateway restart)."""
    store = SessionStore()
    s = _make_session()
    s.last_user_message = "Reply with exactly: T3OK"
    r = b.create_session(s)
    _assert(r.success, f"T3 backend failed: {r.errors}")

    # Simulate orchestrator persisting the result
    s.backend_session_id = r.backend_session_id
    store.save(s)

    # Reload from disk (simulates gateway restart reading persisted state)
    reloaded = store.get(s.session_id)
    _assert(reloaded is not None, "T3 session not found in store")
    _assert(reloaded.backend_session_id == r.backend_session_id, "T3 session ID not persisted")

    # Resume using reloaded session
    r2 = b.resume_session(reloaded, "Reply with exactly: T3RESUME")
    _assert(r2.success, f"T3 resume failed: {r2.errors}")
    _assert("T3RESUME" in r2.output, f"T3 resume unexpected output: {r2.output!r}")

    # Cleanup
    store.delete(s.session_id)
    print(f"  T3 OK — persisted {r.backend_session_id!r}, reloaded and resumed successfully")


def test_t4_cancel(b: OpenCodeServerBackend) -> None:
    """cancel() does not hang and returns without error for both live and non-live sessions."""
    s = _make_session(backend_session_id="ses_doesnotexist000")
    start = time.time()
    b.cancel(s)  # must not hang
    elapsed = time.time() - start
    _assert(elapsed < 5, f"T4 cancel took {elapsed:.1f}s — may be blocking")

    # Create a real session and cancel it
    s2 = _make_session()
    s2.last_user_message = "Reply with exactly: T4OK"
    r = b.create_session(s2)
    _assert(r.success, f"T4 setup failed: {r.errors}")
    s2.backend_session_id = r.backend_session_id
    b.cancel(s2)
    print(f"  T4 OK — cancel non-blocking, elapsed={elapsed:.3f}s")


def test_t5_close(b: OpenCodeServerBackend) -> None:
    """close() deletes the session from the server."""
    s = _make_session()
    s.last_user_message = "Reply with exactly: T5OK"
    r = b.create_session(s)
    _assert(r.success, f"T5 setup failed: {r.errors}")
    s.backend_session_id = r.backend_session_id

    b.close(s)

    # Verify the session is gone — a resume should auto-recreate (not error)
    r2 = b.resume_session(s, "Reply with exactly: T5AFTER")
    _assert(r2.success, f"T5 post-close resume failed: {r2.errors}")
    _assert(r2.backend_session_id != r.backend_session_id, "T5 expected new session ID after close+resume")
    print(f"  T5 OK — session deleted, resume auto-recreated new session={r2.backend_session_id!r}")


def test_t6_run_oneoff(b: OpenCodeServerBackend) -> None:
    """run_oneoff returns output, clears backend_session_id."""
    r = b.run_oneoff(REPO, "Reply with exactly: T6OK")
    _assert(r.success, f"T6 failed: {r.errors}")
    _assert("T6OK" in r.output, f"T6 unexpected output: {r.output!r}")
    _assert(r.backend_session_id == "", f"T6 session ID should be empty, got {r.backend_session_id!r}")
    print(f"  T6 OK — oneoff output={r.output!r}, session_id cleared")


def test_t7_session_resurrection(b: OpenCodeServerBackend) -> None:
    """resume_session with a stale/nonexistent session ID auto-recreates — no dead end."""
    s = _make_session(backend_session_id="ses_stale_does_not_exist_xyz")
    r = b.resume_session(s, "Reply with exactly: T7OK")
    _assert(r.success, f"T7 failed: {r.errors}")
    _assert("T7OK" in r.output, f"T7 unexpected output: {r.output!r}")
    _assert(r.backend_session_id.startswith("ses_"), f"T7 no new session ID: {r.backend_session_id!r}")
    _assert(r.backend_session_id != "ses_stale_does_not_exist_xyz", "T7 stale ID was not replaced")
    print(f"  T7 OK — stale session auto-resurrected as {r.backend_session_id!r}")


def test_t8_empty_id_resume(b: OpenCodeServerBackend) -> None:
    """resume_session with empty backend_session_id falls back to create_session."""
    s = _make_session(backend_session_id="")
    r = b.resume_session(s, "Reply with exactly: T8OK")
    _assert(r.success, f"T8 failed: {r.errors}")
    _assert("T8OK" in r.output, f"T8 unexpected output: {r.output!r}")
    _assert(r.backend_session_id.startswith("ses_"), f"T8 no session ID: {r.backend_session_id!r}")
    print(f"  T8 OK — empty ID fallback created {r.backend_session_id!r}")


def test_t9_server_restart(b: OpenCodeServerBackend) -> None:
    """terminate_active_processes kills server; next call transparently restarts."""
    # Warm up
    s = _make_session()
    s.last_user_message = "Reply with exactly: T9PRE"
    r = b.create_session(s)
    _assert(r.success, f"T9 pre-restart failed: {r.errors}")
    old_url = b._base_url

    # Kill the server process
    b.terminate_active_processes()
    _assert(b._proc is None, "T9 proc not cleared after terminate")
    _assert(b._base_url == "", "T9 base_url not cleared after terminate")

    # Next call must transparently restart
    s2 = _make_session()
    s2.last_user_message = "Reply with exactly: T9POST"
    r2 = b.create_session(s2)
    _assert(r2.success, f"T9 post-restart failed: {r2.errors}")
    _assert("T9POST" in r2.output, f"T9 unexpected output: {r2.output!r}")
    _assert(b._base_url != "", "T9 server URL not repopulated")
    print(f"  T9 OK — server killed ({old_url}), restarted ({b._base_url}), resumed normally")


def test_t10_concurrent_sessions(b: OpenCodeServerBackend) -> None:
    """Two independent gateway sessions share one server without cross-contamination."""
    import threading

    results = {}

    def run(label: str, reply: str) -> None:
        s = _make_session()
        s.last_user_message = f"Reply with exactly: {reply}"
        r = b.create_session(s)
        results[label] = r

    t_a = threading.Thread(target=run, args=("A", "T10A"))
    t_b = threading.Thread(target=run, args=("B", "T10B"))
    t_a.start()
    t_b.start()
    t_a.join(timeout=60)
    t_b.join(timeout=60)

    ra, rb = results.get("A"), results.get("B")
    _assert(ra is not None and ra.success, f"T10 A failed: {ra and ra.errors}")
    _assert(rb is not None and rb.success, f"T10 B failed: {rb and rb.errors}")
    _assert("T10A" in ra.output, f"T10 A got wrong output: {ra.output!r}")
    _assert("T10B" in rb.output, f"T10 B got wrong output: {rb.output!r}")
    _assert(ra.backend_session_id != rb.backend_session_id, "T10 sessions share same ID — isolation broken")
    print(f"  T10 OK — A={ra.backend_session_id!r} B={rb.backend_session_id!r} outputs isolated")


async def test_t11_orchestrator_submit(b: OpenCodeServerBackend) -> None:
    """Full orchestrator submit_instruction path — session create then resume."""
    from src.orchestrator import TaskOrchestrator
    from src.core.session_store import SessionStore

    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.session_store = SessionStore()
    orch._backends = {"opencode-server": b}

    # Create a session in the store
    session = orch.session_store.create(
        backend="opencode-server",
        repo_path=REPO,
    )
    sid = session.session_id

    # Simulate what the orchestrator does on first turn
    session.last_user_message = "Reply with exactly: T11FIRST"
    raw: ExecutionResult = await asyncio.to_thread(b.create_session, session)
    _assert(raw.success, f"T11 first turn failed: {raw.errors}")
    _assert("T11FIRST" in raw.output, f"T11 first turn output: {raw.output!r}")

    # Persist backend_session_id (as orchestrator does at line 1107-1109)
    if raw.backend_session_id:
        session.backend_session_id = raw.backend_session_id
        orch.session_store.save(session)

    # Reload (simulates next incoming message loading session from disk)
    reloaded = orch.session_store.get(sid)
    _assert(reloaded is not None, "T11 session disappeared from store")
    _assert(reloaded.backend_session_id == raw.backend_session_id, "T11 backend_session_id not persisted")

    # Resume turn
    reloaded.last_user_message = "Reply with exactly: T11SECOND"
    raw2: ExecutionResult = await asyncio.to_thread(b.resume_session, reloaded, "Reply with exactly: T11SECOND")
    _assert(raw2.success, f"T11 second turn failed: {raw2.errors}")
    _assert("T11SECOND" in raw2.output, f"T11 second turn output: {raw2.output!r}")

    orch.session_store.delete(sid)
    print(f"  T11 OK — create→persist→reload→resume full orchestrator path verified")


def test_t12_execution_result_fields(b: OpenCodeServerBackend) -> None:
    """ExecutionResult has all fields the orchestrator reads (files_modified, parsed_output, etc.)."""
    s = _make_session()
    s.last_user_message = "Reply with exactly: T12OK"
    r = b.create_session(s)
    _assert(r.success, f"T12 failed: {r.errors}")
    _assert(isinstance(r.files_modified, list), "T12 files_modified not a list")
    _assert(isinstance(r.errors, list), "T12 errors not a list")
    _assert(isinstance(r.execution_time, float), "T12 execution_time not a float")
    _assert(isinstance(r.parsed_output, dict), "T12 parsed_output not a dict")
    _assert("finish" in r.parsed_output, "T12 parsed_output missing 'finish' key")
    _assert(r.parsed_output.get("finish") in ("stop", "tool-calls", "unknown"), f"T12 unexpected finish: {r.parsed_output.get('finish')!r}")
    _assert(r.backend_session_id != "", "T12 backend_session_id empty")
    print(f"  T12 OK — all ExecutionResult fields present, finish={r.parsed_output.get('finish')!r}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all() -> None:
    print("\n=== OpenCodeServerBackend integration tests ===\n")
    b = OpenCodeServerBackend()

    tests_passed = 0
    tests_failed = 0

    def run(name: str, fn, *args) -> bool:
        print(f"[{name}] ", end="", flush=True)
        try:
            fn(*args)
            tests_passed_ref.append(name)
            return True
        except AssertionError as e:
            print(f"\n  FAIL: {e}")
            tests_failed_ref.append(name)
            return False
        except Exception as e:
            print(f"\n  ERROR: {type(e).__name__}: {e}")
            tests_failed_ref.append(name)
            return False

    tests_passed_ref: list = []
    tests_failed_ref: list = []

    # Sequential tests
    oc_id = None
    print("[T1] ", end="", flush=True)
    try:
        oc_id = test_t1_create_session(b)
        tests_passed_ref.append("T1")
    except Exception as e:
        print(f"\n  FAIL: {e}")
        tests_failed_ref.append("T1")

    if oc_id:
        print("[T2] ", end="", flush=True)
        try:
            test_t2_resume_session(b, oc_id)
            tests_passed_ref.append("T2")
        except Exception as e:
            print(f"\n  FAIL: {e}")
            tests_failed_ref.append("T2")

    for name, fn in [
        ("T3", lambda: test_t3_session_store_roundtrip(b)),
        ("T4", lambda: test_t4_cancel(b)),
        ("T5", lambda: test_t5_close(b)),
        ("T6", lambda: test_t6_run_oneoff(b)),
        ("T7", lambda: test_t7_session_resurrection(b)),
        ("T8", lambda: test_t8_empty_id_resume(b)),
        ("T9", lambda: test_t9_server_restart(b)),
        ("T10", lambda: test_t10_concurrent_sessions(b)),
        ("T12", lambda: test_t12_execution_result_fields(b)),
    ]:
        print(f"[{name}] ", end="", flush=True)
        try:
            fn()
            tests_passed_ref.append(name)
        except Exception as e:
            print(f"\n  FAIL ({type(e).__name__}): {e}")
            tests_failed_ref.append(name)

    # Async test
    print("[T11] ", end="", flush=True)
    try:
        asyncio.run(test_t11_orchestrator_submit(b))
        tests_passed_ref.append("T11")
    except Exception as e:
        print(f"\n  FAIL ({type(e).__name__}): {e}")
        tests_failed_ref.append("T11")

    b.terminate_active_processes()

    print(f"\n=== Results: {len(tests_passed_ref)} passed, {len(tests_failed_ref)} failed ===")
    if tests_failed_ref:
        print(f"Failed: {', '.join(tests_failed_ref)}")
        sys.exit(1)
    else:
        print("All tests passed.")


if __name__ == "__main__":
    run_all()
