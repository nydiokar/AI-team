"""A84 carry (o) — the durable, Case-scoped worker-completion outbox.

The load-bearing invariant (ACCEPTANCE 1): a terminal managed Case **worker
child** of an outbox-mode Case has EXACTLY ONE matching ``completion_outbox``
row, or NEITHER the terminal write nor the outbox row commits. Legacy Cases and
control/non-Case tasks are unaffected. Proven here against a REAL file-backed
SQLite database (no mocks) exercising the real ``MeshDB.complete_turn`` /
``MeshDB.resolve_recovery`` transactions, including a real DB-level failure
(a RAISE(ABORT) trigger) to force the rollback leg and a duplicate terminal
report.

T01 success child            → one row, outcome 'success', terminal committed
T02 failed child             → one row, outcome 'failed'
T03 cancelled child          → one row, outcome 'cancelled'
T04 rollback on DB failure   → neither the status flip nor the outbox row commit
T05 duplicate terminal (same token) → still exactly one row (idempotent replay)
T06 foreign-token completion → raises, no second row, first row intact
T07 legacy Case (mode NULL)  → NO outbox row (byte-identical legacy path)
T08 control task (no Case)   → NO outbox row
T09 non-child turn of a Case (no task flow-link) → NO outbox row
T10 resolve_recovery child   → one row, outcome = resolved status, atomic
T11 continuation_mode is immutable across update_flow_run
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

import src.control.db as db_mod
from src.control.db import MeshDB


def _seed_running_child(
    db: MeshDB,
    task_id: str,
    case_id: Optional[str],
    *,
    token: str = "tok-1",
    status: str = "running",
    link_as_child: bool = True,
    action: str = "resume_session",
) -> None:
    """Insert one protocol-1 managed turn directly in ``status`` with ``token``,
    scoped to ``case_id`` (NULL = a control/non-Case task). When
    ``link_as_child`` the task is recorded as a Case worker child via the
    authoritative ``flow_links`` membership row (entity_type='task'), exactly as
    a real dispatch does. session_id is left NULL to avoid the sessions FK — the
    outbox predicate reads only flow_run_id + the Case mode + the task link."""
    now = db_mod._now()
    conn = db._conn()
    conn.execute(
        "INSERT INTO mesh_tasks "
        "(id, session_id, backend, action, payload, status, queue_protocol, "
        " claim_token, flow_run_id, created_at, updated_at) "
        "VALUES (?, NULL, 'claude', ?, '{}', ?, 1, ?, ?, ?, ?)",
        (task_id, action, status, token, case_id, now, now),
    )
    conn.commit()
    if case_id and link_as_child:
        db.create_flow_link(case_id, "task", task_id, "task", created_by="manager")


def _open_outbox_case(db: MeshDB, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("CASE_COMPLETION_OUTBOX_ENABLED", "1")
    return db.open_case("obj", "mgr-sess")


def _open_legacy_case(db: MeshDB, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.delenv("CASE_COMPLETION_OUTBOX_ENABLED", raising=False)
    return db.open_case("obj", "mgr-sess")


@pytest.fixture()
def db(tmp_path: Any) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _outbox_rows(db: MeshDB, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM completion_outbox WHERE case_id = ? ORDER BY child_task_id",
        (case_id,),
    ).fetchall()]


# --- schema / mode marker ------------------------------------------------- #
def test_migration_applies_and_mode_marker(db: MeshDB, monkeypatch: pytest.MonkeyPatch) -> None:
    assert db._conn().execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 44
    outbox_case = _open_outbox_case(db, monkeypatch)
    legacy_case = _open_legacy_case(db, monkeypatch)
    assert db.case_continuation_mode(outbox_case) == "outbox"
    assert db.case_continuation_mode(legacy_case) is None
    assert db.case_continuation_mode("no-such-case") is None


# --- T01/T02/T03: the committed invariant --------------------------------- #
@pytest.mark.parametrize("status,outcome", [
    ("completed", "success"), ("failed", "failed"), ("cancelled", "cancelled"),
])
def test_terminal_child_writes_exactly_one_outbox_row(
    db: MeshDB, monkeypatch: pytest.MonkeyPatch, status: str, outcome: str,
) -> None:
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_running_child(db, "t1", case_id)
    # A cancelled terminal cannot be produced by a plain complete_turn without a
    # cancel mark, so drive the status directly through complete_turn's status arg.
    db.complete_turn("t1", "tok-1", {"output": "x"}, status=status)
    rows = _outbox_rows(db, case_id)
    assert len(rows) == 1
    assert rows[0]["child_task_id"] == "t1"
    assert rows[0]["outcome"] == outcome
    assert rows[0]["delivered_at"] is None
    # terminal write committed too
    trow = db._conn().execute("SELECT status FROM mesh_tasks WHERE id='t1'").fetchone()
    assert trow["status"] == status


# --- T04: rollback — a DB failure in the outbox insert aborts BOTH writes -- #
def test_outbox_failure_rolls_back_the_terminal_write(
    db: MeshDB, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_running_child(db, "t4", case_id)
    conn = db._conn()
    # A REAL DB-level failure on the outbox INSERT (not a mock): a BEFORE INSERT
    # trigger that aborts. It lives in the schema so it fires on complete_turn's
    # own write connection.
    conn.execute(
        "CREATE TRIGGER outbox_boom BEFORE INSERT ON completion_outbox "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    conn.commit()
    with pytest.raises(Exception):
        db.complete_turn("t4", "tok-1", {"output": "x"}, status="completed")
    # NEITHER write committed: the task is still running, no outbox row.
    trow = db._conn().execute(
        "SELECT status, effects_state FROM mesh_tasks WHERE id='t4'").fetchone()
    assert trow["status"] == "running"
    assert trow["effects_state"] is None
    assert _outbox_rows(db, case_id) == []
    # Remove the fault and prove the SAME turn now commits exactly one row.
    db._conn().execute("DROP TRIGGER outbox_boom")
    db._conn().commit()
    db.complete_turn("t4", "tok-1", {"output": "x"}, status="completed")
    assert len(_outbox_rows(db, case_id)) == 1
    assert db._conn().execute(
        "SELECT status FROM mesh_tasks WHERE id='t4'").fetchone()["status"] == "completed"


# --- T05: duplicate terminal report (same token) stays exactly one row ---- #
def test_duplicate_terminal_same_token_is_idempotent(
    db: MeshDB, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_running_child(db, "t5", case_id)
    r1 = db.complete_turn("t5", "tok-1", {"output": "x"}, status="completed")
    r2 = db.complete_turn("t5", "tok-1", {"output": "x"}, status="completed")
    assert r1.status == r2.status == "completed"
    assert len(_outbox_rows(db, case_id)) == 1


# --- T06: a foreign/superseded token is refused, no second row ------------ #
def test_foreign_token_completion_refused_no_second_row(
    db: MeshDB, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_running_child(db, "t6", case_id)
    db.complete_turn("t6", "tok-1", {"output": "x"}, status="completed")
    with pytest.raises(Exception):
        db.complete_turn("t6", "WRONG-token", {"output": "y"}, status="failed")
    rows = _outbox_rows(db, case_id)
    assert len(rows) == 1 and rows[0]["outcome"] == "success"


# --- T07/T08/T09: legacy / control / non-child are unaffected ------------- #
def test_legacy_mode_case_writes_no_outbox_row(
    db: MeshDB, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = _open_legacy_case(db, monkeypatch)
    _seed_running_child(db, "t7", case_id)
    db.complete_turn("t7", "tok-1", {"output": "x"}, status="completed")
    assert _outbox_rows(db, case_id) == []


def test_control_task_without_case_writes_no_outbox_row(
    db: MeshDB, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CASE_COMPLETION_OUTBOX_ENABLED", "1")
    _seed_running_child(db, "t8", None, link_as_child=False)
    db.complete_turn("t8", "tok-1", {"output": "x"}, status="completed")
    assert db._conn().execute("SELECT COUNT(*) FROM completion_outbox").fetchone()[0] == 0


def test_non_child_turn_of_outbox_case_writes_no_row(
    db: MeshDB, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A turn carrying the Case scope but with NO task membership link (e.g. the
    # Manager's own turn) must NOT produce an outbox row — it is the awaiter.
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_running_child(db, "t9", case_id, link_as_child=False)
    db.complete_turn("t9", "tok-1", {"output": "x"}, status="completed")
    assert _outbox_rows(db, case_id) == []


# --- T10: resolve_recovery is atomic too ---------------------------------- #
def test_resolve_recovery_child_writes_one_row_atomically(
    db: MeshDB, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = _open_outbox_case(db, monkeypatch)
    _seed_running_child(db, "t10", case_id, status="recovery_required")
    db.resolve_recovery(
        "t10", "tok-1", {"result": {"output": "done"}}, resolved_status="completed",
    )
    rows = _outbox_rows(db, case_id)
    assert len(rows) == 1 and rows[0]["outcome"] == "success"
    assert db._conn().execute(
        "SELECT status FROM mesh_tasks WHERE id='t10'").fetchone()["status"] == "completed"


# --- T11: the cutover marker is immutable --------------------------------- #
def test_continuation_mode_is_immutable_via_update_flow_run(
    db: MeshDB, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id = _open_outbox_case(db, monkeypatch)
    # update_flow_run must refuse the field (not in _FLOW_EXTRA_FIELDS).
    with pytest.raises(ValueError):
        db.update_flow_run(case_id, continuation_mode="legacy")
    assert db.case_continuation_mode(case_id) == "outbox"
