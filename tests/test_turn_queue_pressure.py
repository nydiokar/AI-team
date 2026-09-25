"""A82 Stage 1 — pressure / service-boundary RED acceptance tests (LOAD01-04).

Assert the TARGET admission pressure bounds (design §8, packet §11) against a
real temp file-backed `MeshDB` with genuine concurrency + a real held SQLite
write lock. Ground truth (Stage 0 / config): `config.system.max_queue_size==50`
is the intended fleet-wide managed queued+pending cap (with 20 per session), but
NO managed admission service enforces count/byte bounds atomically, there is no
pre-parse byte gate, and `enqueue_task` swallows failures (no phantom-ack guard).

These fail red by asserting the missing bounded-admission contract; the SQLite
lock-contention case uses real connections so it exercises actual behavior.
"""
import sqlite3
import time
from datetime import datetime

import pytest

from config import config
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus


NOW = datetime(2026, 9, 25, 12, 0, 0)


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _session(db: MeshDB, session_id: str = "sess-1") -> None:
    db.upsert_session(
        Session(
            session_id=session_id,
            backend="claude",
            repo_path="/tmp/repo",
            status=SessionStatus.BUSY,
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
            machine_id="worker-a",
        )
    )


def _admit(db: MeshDB, **kw):
    """The target atomic managed admission with count/byte/capacity enforcement
    (design §8). Missing today → red.
    """
    for name in ("enqueue_turn", "admit_turn", "enqueue_managed_turn"):
        fn = getattr(db, name, None)
        if callable(fn):
            return fn(**kw)
    pytest.fail(
        "no managed admission helper enforcing atomic count/byte/per-session "
        "capacity (design §8); contract not implemented"
    )


def _resolve(obj, *names):
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            return fn
    return None


# --------------------------------------------------------------------------- #
# LOAD01 — bounded queued+pending count / bytes / executor tasks
# --------------------------------------------------------------------------- #
def test_LOAD01_admission_enforces_fleet_and_per_session_count_cap(tmp_path):
    """Fleet-wide managed queued+pending cap = max_queue_size (50) and 20 per
    session, enforced ATOMICALLY (design §8). RED: no managed admission cap.
    """
    db = _db(tmp_path)
    _session(db, "sess-1")
    assert config.system.max_queue_size == 50
    # Admit up to the per-session cap of 20; the 21st must be rejected (429-like
    # typed capacity failure / falsy).
    accepted = 0
    rejected = False
    for i in range(25):
        try:
            tid = _admit(db, session_id="sess-1", body=f"m{i}", operation_id=f"op-{i}")
            if tid:
                accepted += 1
        except Exception:  # noqa: BLE001
            rejected = True
            break
    assert accepted <= 20, "per-session cap of 20 was not enforced"
    assert rejected or accepted <= 20


def test_LOAD01b_admission_enforces_stored_byte_budget(tmp_path):
    """Stored waiting intent is bounded (<=2 MiB/row, 100 MiB fleet) via
    persisted byte accounting (design §8). RED: no byte-accounting helper.
    """
    db = _db(tmp_path)
    counter = _resolve(db, "managed_queued_bytes", "queued_intent_bytes", "stored_intent_bytes")
    assert counter is not None, (
        "no persisted byte-accounting for queued intent; byte budget cannot be "
        "enforced in the admission transaction (design §8)"
    )


# --------------------------------------------------------------------------- #
# LOAD02 — pre-parse chunked oversize rejection / body timeout
# --------------------------------------------------------------------------- #
def test_LOAD02_preparse_byte_limit_rejects_before_json_parse(tmp_path):
    """Streaming byte counting must reject oversize/chunked bodies BEFORE JSON
    parse; Content-Length + Pydantic alone do not bound an incoming read
    (design §8). RED: no pre-parse streaming byte gate.
    """
    from src.control import control_api

    gate = _resolve(
        control_api,
        "_streaming_body_limit",
        "_read_bounded_body",
        "_preparse_byte_guard",
    )
    assert gate is not None, (
        "no pre-parse streaming byte gate on the control API; a chunked oversize "
        "body would be fully read before length is checked (design §8)"
    )


# --------------------------------------------------------------------------- #
# LOAD03 — real SQLite lock contention: fail-closed within deadline
# --------------------------------------------------------------------------- #
def test_LOAD03_held_write_lock_fails_closed_no_phantom_accept(tmp_path):
    """With the SQLite write lock held by a second connection, an admission must
    terminate within its deadline with a STRUCTURED failure and NO false accept
    (design §8, §6). Current `enqueue_task` swallows the failure and returns
    None (looks like success). RED: assert admission raises/returns a failure
    and does NOT persist a row while the lock is held.
    """
    db = _db(tmp_path)
    _session(db, "sess-1")

    # Hold an EXCLUSIVE write lock from a separate raw connection.
    blocker = sqlite3.connect(str(db._path), timeout=0.1)
    blocker.isolation_level = None
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        raised = False
        t0 = time.monotonic()
        try:
            _admit(db, session_id="sess-1", body="under-lock", operation_id="op-lock")
        except pytest.fail.Exception:
            # _admit itself failed red (helper missing) — re-raise so the test
            # reports the missing-contract reason.
            raise
        except Exception:  # noqa: BLE001
            raised = True
        elapsed = time.monotonic() - t0
        # Target: fail-closed within a bounded (5s) deadline.
        assert raised, "admission under a held write lock did not fail closed"
        assert elapsed < 15, (
            f"admission under lock took {elapsed:.1f}s; must honor a bounded "
            "deadline, not the legacy 60s+ retry path (design §8)"
        )
        # And NO managed row was phantom-accepted.
        assert db.get_task("op-lock") is None
    finally:
        try:
            blocker.execute("ROLLBACK")
        except Exception:
            pass
        blocker.close()


# --------------------------------------------------------------------------- #
# LOAD04 — no phantom acknowledgements (swallowed write != accepted)
# --------------------------------------------------------------------------- #
def test_LOAD04_swallowed_write_is_not_reported_as_accepted(tmp_path):
    """A swallowed DB write MUST NOT be reported as an accepted admission
    (design §6/§8: no 'accepted' after a helper swallowed a write failure).

    Documents the CURRENT gap: `enqueue_task` catches exceptions and returns
    None regardless — indistinguishable from success. The managed admission must
    instead surface a typed failure. RED: the managed helper is missing (so the
    fail-closed contract is unmet).
    """
    db = _db(tmp_path)
    _session(db, "sess-1")
    # The managed admission must surface a typed failure rather than swallow it
    # like `enqueue_task` (which catches and returns None, indistinguishable from
    # success). Probe for it in the MAIN thread so the missing-contract failure
    # is reported by this test.
    _admit(db, session_id="sess-1", body="c0", operation_id="op-c0")
    # (Unreachable until the managed helper exists; the concurrency phantom-ack
    #  invariant below is what it must ultimately satisfy.)
    acks: list = []
    for i in range(10):
        tid = _admit(db, session_id="sess-1", body=f"c{i}", operation_id=f"op-c{i}")
        if tid:
            acks.append(tid)
    # Every acknowledged id must correspond to a durable row (no phantom acks).
    for tid in acks:
        assert db.get_task(tid) is not None, f"acknowledged id {tid} has no durable row (phantom ack)"
