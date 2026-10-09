"""A84 carry (o) → A104 — the worker-completion message, written atomically in
the child's TERMINAL txn.

The load-bearing invariant (A84 ACCEPTANCE 1, carried by A104): a terminal
managed child that was REQUESTED by an agent session
(``mesh_tasks.sender_session_id``) has EXACTLY ONE matching ``agent_inbox``
message addressed to that requester, or NEITHER the terminal write nor the
message commits. A child nobody requested (human / system / own turn) writes no
message. Proven against a REAL file-backed SQLite database (no mocks) exercising
the real ``MeshDB.complete_turn`` / ``MeshDB.resolve_recovery`` transactions,
including a real DB-level failure (a RAISE(ABORT) trigger) to force the rollback
leg and a duplicate terminal report. The legacy Case-addressed
``completion_outbox`` table is history only — nothing writes it any more.

T01 success child            → one message, outcome 'success', terminal committed
T02 failed child             → one message, outcome 'failed'
T03 cancelled child          → one message, outcome 'cancelled'
T04 rollback on DB failure   → neither the status flip nor the message commit
T05 duplicate terminal (same token) → still exactly one message (idempotent replay)
T06 foreign-token completion → raises, no second message, first intact
T07 unrequested Case child   → NO message
T08 control task (no Case, no requester) → NO message
T09 the requester's own turn (self-request) → NO message
T10 resolve_recovery child   → one message, outcome = resolved status, atomic

Deleted with A104 Phase 5 (the Case ``continuation_mode`` birth marker and the
``CASE_COMPLETION_OUTBOX_ENABLED`` flag are gone): the mode-marker migration test
(``case_continuation_mode`` no longer exists) and T11 (``continuation_mode``
immutable via ``update_flow_run`` — the column is no longer written or read).
"""
from __future__ import annotations

from typing import Any

import pytest

import src.control.db as db_mod
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus

from tests.inbox_seed import seed_child

REQUESTER = "mgr-sess"


@pytest.fixture()
def db(tmp_path: Any) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _case(db: MeshDB) -> str:
    return db.open_case("obj", REQUESTER)


def _inbox_rows(db: MeshDB) -> list[dict[str, Any]]:
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM agent_inbox ORDER BY about_task_id").fetchall()]


def _status(db: MeshDB, task_id: str) -> str:
    return db._conn().execute("SELECT status FROM mesh_tasks WHERE id = ?", (task_id,)).fetchone()["status"]


def _no_legacy_outbox(db: MeshDB) -> bool:
    return db._conn().execute("SELECT COUNT(*) FROM completion_outbox").fetchone()[0] == 0


# --- T01/T02/T03: the committed invariant --------------------------------- #
@pytest.mark.parametrize("status,outcome", [
    ("completed", "success"), ("failed", "failed"), ("cancelled", "cancelled"),
])
def test_terminal_child_writes_exactly_one_outbox_row(db: MeshDB, status: str, outcome: str) -> None:
    case_id = _case(db)
    seed_child(db, case_id, "t1", requester=REQUESTER, token="tok-1")
    # A cancelled terminal cannot be produced by a plain complete_turn without a
    # cancel mark, so drive the status directly through complete_turn's status arg.
    db.complete_turn("t1", "tok-1", {"output": "x"}, status=status)
    rows = _inbox_rows(db)
    assert len(rows) == 1
    (row,) = rows
    assert (row["about_task_id"], row["recipient_session_id"], row["case_id"]) == ("t1", REQUESTER, case_id)
    assert (row["outcome"], row["state"], row["attempts"]) == (outcome, "pending", 0)
    assert _status(db, "t1") == status  # terminal write committed too
    assert _no_legacy_outbox(db)


# --- T04: rollback — a DB failure in the message insert aborts BOTH writes - #
def test_outbox_failure_rolls_back_the_terminal_write(db: MeshDB) -> None:
    case_id = _case(db)
    seed_child(db, case_id, "t4", requester=REQUESTER, token="tok-1")
    conn = db._conn()
    # A REAL DB-level failure on the inbox INSERT (not a mock): a BEFORE INSERT
    # trigger that aborts. It lives in the schema so it fires on complete_turn's
    # own write connection.
    conn.execute(
        "CREATE TRIGGER inbox_boom BEFORE INSERT ON agent_inbox "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    conn.commit()
    with pytest.raises(Exception):
        db.complete_turn("t4", "tok-1", {"output": "x"}, status="completed")
    # NEITHER write committed: the task is still running, no message.
    trow = db._conn().execute("SELECT status, effects_state FROM mesh_tasks WHERE id='t4'").fetchone()
    assert trow["status"] == "running"
    assert trow["effects_state"] is None
    assert _inbox_rows(db) == []
    # Remove the fault and prove the SAME turn now commits exactly one message.
    db._conn().execute("DROP TRIGGER inbox_boom")
    db._conn().commit()
    db.complete_turn("t4", "tok-1", {"output": "x"}, status="completed")
    assert len(_inbox_rows(db)) == 1
    assert _status(db, "t4") == "completed"


# --- T05: duplicate terminal report (same token) stays exactly one message - #
def test_duplicate_terminal_same_token_is_idempotent(db: MeshDB) -> None:
    case_id = _case(db)
    seed_child(db, case_id, "t5", requester=REQUESTER, token="tok-1")
    r1 = db.complete_turn("t5", "tok-1", {"output": "x"}, status="completed")
    r2 = db.complete_turn("t5", "tok-1", {"output": "x"}, status="completed")
    assert r1.status == r2.status == "completed"
    assert len(_inbox_rows(db)) == 1


# --- T06: a foreign/superseded token is refused, no second message -------- #
def test_foreign_token_completion_refused_no_second_row(db: MeshDB) -> None:
    case_id = _case(db)
    seed_child(db, case_id, "t6", requester=REQUESTER, token="tok-1")
    db.complete_turn("t6", "tok-1", {"output": "x"}, status="completed")
    with pytest.raises(Exception):
        db.complete_turn("t6", "WRONG-token", {"output": "y"}, status="failed")
    rows = _inbox_rows(db)
    assert len(rows) == 1 and rows[0]["outcome"] == "success"


# --- T07/T08/T09: nobody requested it ⇒ no message ------------------------ #
def test_legacy_mode_case_writes_no_outbox_row(db: MeshDB) -> None:
    """T07 (was: legacy-mode Case): a Case child with NO requester (a human /
    system dispatch) is linked to the Case but addressed to nobody."""
    case_id = _case(db)
    seed_child(db, case_id, "t7", requester=None, token="tok-1")
    db.complete_turn("t7", "tok-1", {"output": "x"}, status="completed")
    assert _status(db, "t7") == "completed"
    assert _inbox_rows(db) == [] and _no_legacy_outbox(db)


def test_control_task_without_case_writes_no_outbox_row(db: MeshDB) -> None:
    seed_child(db, None, "t8", requester=None, token="tok-1")
    db.complete_turn("t8", "tok-1", {"output": "x"}, status="completed")
    assert _inbox_rows(db) == [] and _no_legacy_outbox(db)


def test_non_child_turn_of_outbox_case_writes_no_row(db: MeshDB) -> None:
    """T09: the requester's OWN turn carrying the Case scope (sender == the
    session it runs on) is the awaiter, not a child — no self-addressed message."""
    now = db_mod._now()
    db.upsert_session(Session(
        session_id=REQUESTER, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.BUSY, created_at=now, updated_at=now, machine_id="worker-a",
    ))
    case_id = _case(db)
    seed_child(db, case_id, "t9", requester=REQUESTER, token="tok-1", link=False)
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET session_id = ? WHERE id = 't9'", (REQUESTER,))
    db.complete_turn("t9", "tok-1", {"output": "x"}, status="completed")
    assert _status(db, "t9") == "completed"
    assert _inbox_rows(db) == []


# --- T10: resolve_recovery is atomic too ---------------------------------- #
def test_resolve_recovery_child_writes_one_row_atomically(db: MeshDB) -> None:
    case_id = _case(db)
    seed_child(db, case_id, "t10", requester=REQUESTER, status="recovery_required", token="tok-1")
    db.resolve_recovery("t10", "tok-1", {"result": {"output": "done"}}, resolved_status="completed")
    rows = _inbox_rows(db)
    assert len(rows) == 1
    assert (rows[0]["about_task_id"], rows[0]["recipient_session_id"], rows[0]["outcome"]) == (
        "t10", REQUESTER, "success")
    assert _status(db, "t10") == "completed"
    assert _no_legacy_outbox(db)
