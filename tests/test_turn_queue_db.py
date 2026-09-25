"""A82 Stage 2 — managed turn-queue DB invariants (DB01-08 per packet §5).

Real file-backed temp SQLite (`MeshDB`); no network / paid CLI. These exercise
the STRICT protocol-1 (managed) DB helpers added in Stage 2
(``enqueue_turn`` / ``activate_turn`` / ``revise_turn`` / ``withdraw_turn`` /
``claim_turn`` / ``start_turn`` / ``complete_turn`` / ``resolve_recovery`` /
``get_turn_revisions``) and migration 34, against design §§3-4 and §6.

DB01 additive migration (survives duplicate legacy active rows / cancellation /
      NULL-session sentinels)
DB02 scoped unique active slot (one active managed turn per session; legacy rows
      unaffected)
DB03 required managed fields present + server-owned protocol
DB04 monotonic per-session sequence
DB05 original-request (admission) hash idempotency
DB06 replay after edit / withdraw / (terminal) close returns current state, never
      the original text
DB07 revision audit commits with the edit and ROLLS BACK with a failed edit
DB08 no legacy execution bypass — the managed completion refuses a never-started
      / foreign-token turn (the legacy swallowing helper is untouched)
"""
import sqlite3
from datetime import datetime

import pytest

from src.control.db import MeshDB
from src.control.turn_queue import (
    OwnershipConflictError,
    TurnNotFoundError,
    QUEUE_PROTOCOL_MANAGED,
)
from src.core.interfaces import Session, SessionStatus


NOW = datetime(2026, 9, 25, 12, 0, 0).isoformat()


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _session(db: MeshDB, session_id: str = "sess-1") -> None:
    db.upsert_session(
        Session(
            session_id=session_id,
            backend="claude",
            repo_path="/tmp/repo",
            status=SessionStatus.BUSY,
            created_at=NOW,
            updated_at=NOW,
            machine_id="worker-a",
        )
    )


def _enqueue(db: MeshDB, task_id: str, session_id: str = "sess-1", **kw):
    return db.enqueue_turn(
        task_id=task_id,
        session_id=session_id,
        backend="claude",
        action="resume_session",
        payload={"task_id": task_id, "prompt": kw.pop("prompt", "hi")},
        turn_source="human",
        turn_kind="instruction",
        **kw,
    )


# --------------------------------------------------------------------------- #
# DB01 — additive migration survives legacy duplicates / cancellation / NULLs
# --------------------------------------------------------------------------- #
def test_DB01_migration_survives_duplicate_legacy_and_null_session_rows(tmp_path):
    db = _db(tmp_path)
    _session(db, "sess-legacy")
    # Duplicate legacy pending + claimed rows for ONE session (the pre-A82 shape
    # the design §2 P0 says the scoped index must not invalidate) + a cancel row
    # + a NULL-session sentinel/scheduling token.
    with db._write() as conn:
        for tid, st in [("lg1", "pending"), ("lg2", "pending"), ("lg3", "claimed")]:
            conn.execute(
                "INSERT INTO mesh_tasks(id,session_id,backend,action,payload,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (tid, "sess-legacy", "claude", "resume_session", "{}", st, NOW, NOW),
            )
        conn.execute(
            "INSERT INTO mesh_tasks(id,session_id,backend,action,payload,status,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("cx", "sess-legacy", "claude", "cancel", "{}", "pending", NOW, NOW),
        )
        conn.execute(
            "INSERT INTO mesh_tasks(id,session_id,backend,action,payload,status,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("sched", None, "claude", "continuation", "{}", "pending", NOW, NOW),
        )
    # Migration 34 is present and every legacy row defaulted to protocol 0.
    ver = db._conn().execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert ver >= 34
    rows = db._conn().execute(
        "SELECT queue_protocol FROM mesh_tasks WHERE id IN "
        "('lg1','lg2','lg3','cx','sched')"
    ).fetchall()
    assert rows and all(r[0] == 0 for r in rows), "legacy rows are not protocol 0"
    cols = {r[1] for r in db._conn().execute("PRAGMA table_info(mesh_tasks)").fetchall()}
    for c in ("queue_protocol", "queue_sequence", "claim_token", "revision",
              "idempotency_key", "admission_hash", "coalesce_key", "started_at"):
        assert c in cols, f"migration 34 missing column {c}"


# --------------------------------------------------------------------------- #
# DB02 — scoped unique active slot (one active managed turn per session)
# --------------------------------------------------------------------------- #
def test_DB02_one_active_managed_slot_per_session(tmp_path):
    db = _db(tmp_path)
    _session(db)
    _enqueue(db, "m1")
    _enqueue(db, "m2")
    assert db.activate_turn("m1") is True
    # A second activation for the SAME session must not create a second active
    # slot — the partial unique index (design §3) blocks it.
    with pytest.raises(OwnershipConflictError):
        db.activate_turn("m2")
    active = db.get_active_turn("sess-1")
    assert active and active["id"] == "m1"
    # A DIFFERENT session activates independently.
    _session(db, "sess-2")
    _enqueue(db, "n1", session_id="sess-2")
    assert db.activate_turn("n1") is True
    assert db.get_active_turn("sess-2")["id"] == "n1"


def test_DB02b_legacy_rows_do_not_trip_the_managed_slot_index(tmp_path):
    """Legacy protocol-0 duplicates for a session must NOT block a managed active
    turn — the index is partial on queue_protocol=1 (design §2 P0)."""
    db = _db(tmp_path)
    _session(db)
    with db._write() as conn:
        for tid in ("l1", "l2"):
            conn.execute(
                "INSERT INTO mesh_tasks(id,session_id,backend,action,payload,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (tid, "sess-1", "claude", "resume_session", "{}", "pending", NOW, NOW),
            )
    _enqueue(db, "m1")
    assert db.activate_turn("m1") is True  # not blocked by l1/l2


# --------------------------------------------------------------------------- #
# DB03 — required managed fields + server-owned protocol
# --------------------------------------------------------------------------- #
def test_DB03_managed_row_carries_required_fields(tmp_path):
    db = _db(tmp_path)
    _session(db)
    _enqueue(db, "m1", idempotency_scope="web:sess-1:resume", idempotency_key="op-1",
             admission_hash="h1")
    row = db.get_task("m1")
    assert row["queue_protocol"] == QUEUE_PROTOCOL_MANAGED
    assert row["queue_sequence"] == 1
    assert row["status"] == "queued"
    assert row["revision"] == 1
    assert row["turn_source"] == "human"
    assert row["turn_kind"] == "instruction"
    assert row["idempotency_key"] == "op-1"
    assert row["admission_hash"] == "h1"


# --------------------------------------------------------------------------- #
# DB04 — monotonic per-session sequence
# --------------------------------------------------------------------------- #
def test_DB04_monotonic_per_session_sequence(tmp_path):
    db = _db(tmp_path)
    _session(db)
    _session(db, "sess-2")
    r1 = _enqueue(db, "a1")
    r2 = _enqueue(db, "a2")
    r3 = _enqueue(db, "a3")
    assert [r1["queue_sequence"], r2["queue_sequence"], r3["queue_sequence"]] == [1, 2, 3]
    # A second session has its OWN independent sequence.
    s1 = _enqueue(db, "b1", session_id="sess-2")
    assert s1["queue_sequence"] == 1
    # The (session_id, queue_sequence) unique index (design §3) forbids a
    # duplicate sequence for the same session.
    with pytest.raises(sqlite3.IntegrityError):
        with db._write() as conn:
            conn.execute(
                "INSERT INTO mesh_tasks(id,session_id,backend,action,payload,status,"
                "created_at,updated_at,queue_protocol,queue_sequence,revision) "
                "VALUES(?,?,?,?,?,?,?,?,1,2,1)",
                ("dup", "sess-1", "claude", "resume_session", "{}", "queued", NOW, NOW),
            )


# --------------------------------------------------------------------------- #
# DB05 — original-request (admission) hash idempotency
# --------------------------------------------------------------------------- #
def test_DB05_idempotent_admission_by_scope_key_hash(tmp_path):
    db = _db(tmp_path)
    _session(db)
    r1 = _enqueue(db, "m1", idempotency_scope="web:sess-1", idempotency_key="op-1",
                  admission_hash="hash-A")
    assert r1["idempotent_replay"] is False
    # Same scope/key/hash under a NEW task_id ⇒ returns the EXISTING id, no new row.
    r2 = _enqueue(db, "m2", idempotency_scope="web:sess-1", idempotency_key="op-1",
                  admission_hash="hash-A")
    assert r2["idempotent_replay"] is True
    assert r2["id"] == "m1"
    assert db.get_task("m2") is None, "a duplicate admission created a second row"
    # Same key, DIFFERENT original hash ⇒ 409.
    with pytest.raises(OwnershipConflictError):
        _enqueue(db, "m3", idempotency_scope="web:sess-1", idempotency_key="op-1",
                 admission_hash="hash-B")


# --------------------------------------------------------------------------- #
# DB06 — replay after edit / withdraw / close returns CURRENT state, not original
# --------------------------------------------------------------------------- #
def test_DB06_replay_after_edit_returns_revised_not_original(tmp_path):
    db = _db(tmp_path)
    _session(db)
    _enqueue(db, "m1", idempotency_scope="web:sess-1", idempotency_key="op-1",
             admission_hash="hash-A", prompt="original text")
    db.revise_turn("m1", expected_revision=1, body="edited text", actor="op")
    # Replaying the ORIGINAL admission returns the same id + CURRENT status/
    # revision — never the original text (design §4).
    replay = _enqueue(db, "m1b", idempotency_scope="web:sess-1", idempotency_key="op-1",
                      admission_hash="hash-A")
    assert replay["id"] == "m1"
    assert replay["idempotent_replay"] is True
    assert replay["revision"] == 2
    row = db.get_task("m1")
    assert row["prompt"] == "edited text"


def test_DB06b_replay_after_withdraw_and_after_terminal_close(tmp_path):
    db = _db(tmp_path)
    _session(db)
    _enqueue(db, "w1", idempotency_scope="s", idempotency_key="k1", admission_hash="h")
    assert db.withdraw_turn("w1", expected_revision=1, actor="op") is True
    # Replay after withdrawal returns the withdrawn (terminal) row, not a re-admit.
    replay = _enqueue(db, "w1b", idempotency_scope="s", idempotency_key="k1",
                      admission_hash="h")
    assert replay["id"] == "w1" and replay["status"] == "withdrawn"


# --------------------------------------------------------------------------- #
# DB07 — revision audit commits with the edit; rolls back with a failed edit
# --------------------------------------------------------------------------- #
def test_DB07_revision_audit_commits_with_edit(tmp_path):
    db = _db(tmp_path)
    _session(db)
    _enqueue(db, "m1", prompt="v1")
    db.revise_turn("m1", expected_revision=1, body="v2", actor="op")
    db.revise_turn("m1", expected_revision=2, body="v3", actor="op")
    audit = db.get_turn_revisions("m1")
    assert [a["revision"] for a in audit] == [2, 3]
    assert [a["body"] for a in audit] == ["v2", "v3"]
    assert all(a["change_kind"] == "edit" for a in audit)


def test_DB07b_failed_edit_rolls_back_the_audit_row(tmp_path):
    """A stale-revision edit raises and writes NEITHER the bump NOR an audit row
    (design §3: audit is inserted atomically with the queued revision)."""
    db = _db(tmp_path)
    _session(db)
    _enqueue(db, "m1", prompt="v1")
    db.revise_turn("m1", expected_revision=1, body="v2", actor="op")  # -> rev 2
    # A conflicting edit against the STALE revision 1 must fail and leave no
    # phantom audit row for a revision that never committed.
    with pytest.raises(OwnershipConflictError):
        db.revise_turn("m1", expected_revision=1, body="loser", actor="op")
    audit = db.get_turn_revisions("m1")
    assert [a["revision"] for a in audit] == [2], "a rolled-back edit left an audit row"
    assert db.get_task("m1")["revision"] == 2


# --------------------------------------------------------------------------- #
# DB08 — no legacy execution bypass on the managed path
# --------------------------------------------------------------------------- #
def test_DB08_managed_complete_refuses_never_started_turn(tmp_path):
    """The managed completion carries an ownership/status predicate: a
    never-started (queued/pending) or foreign-token turn cannot be completed
    (design §6 / OWN08b). This is the guard the legacy swallowing
    ``complete_task`` deliberately lacks — and which decision 2 keeps legacy."""
    db = _db(tmp_path)
    _session(db)
    _enqueue(db, "m1")
    db.activate_turn("m1")  # -> pending (never claimed/started)
    with pytest.raises(OwnershipConflictError):
        db.complete_turn("m1", claim_token="no-such-token", result={"output": "x"})
    # And the legacy helper is UNCHANGED: it still marks any task complete with
    # no predicate (proving we did not touch it).
    _enqueue(db, "leg1")
    db.complete_task("leg1", {"output": "legacy"})
    assert db.get_task("leg1")["status"] == "completed"


def test_DB08b_full_claim_start_complete_lifecycle_commits_native_id_atomically(tmp_path):
    """The happy path: enqueue -> activate -> claim -> start -> complete, with the
    native session id committed atomically onto the session row at completion
    (design §6 / OWN08)."""
    db = _db(tmp_path)
    _session(db)
    db.upsert_node(node_id="worker-a", tailscale_ip="100.64.0.10", api_port=9001,
                   backends=["claude"], max_concurrent=2, incarnation_id="inc-a")
    _enqueue(db, "m1")
    db.activate_turn("m1")
    token = db.claim_turn("m1", node_id="worker-a", carrier_kind="gateway_local",
                          incarnation_id="inc-a")
    assert token and db.get_task("m1")["claim_token"] == token
    auth = db.start_turn("m1", claim_token=token, incarnation_id="inc-a")
    assert auth.status == "running"
    res = db.complete_turn("m1", claim_token=token, result={"output": "done"},
                           native_session_id="native-XYZ")
    assert res.status == "completed"
    assert db.get_task("m1")["status"] == "completed"
    sess = db.get_session("sess-1")
    assert sess["backend_session_id"] == "native-XYZ"
    assert sess["last_task_id"] == "m1"
