"""
A21 — Flow schema extension (v0.4 §11 + dispatch lineage) tests.

Migration 22 promotes the 5-column A19 flow_runs RECORD to the full §11 field
set (approved_plan, plan_review, burn_down_items, execution_result,
implementation_review, waived_findings, closure_summary, role_assignments,
artifact_links, status, updated_at) PLUS three lineage columns
(parent_flow_run_id, dispatched_by, dispatch_file).

The migration is ADDITIVE + IDEMPOTENT and every new column is NULLable, so:
  * A fresh DB converges at version 22 with all columns present.
  * A pre-existing version-21 (5-column) DB migrates cleanly and its old rows
    still read (absent fields NULL).
  * Re-opening an already-migrated DB is a no-op (no error, no version churn).
  * New fields round-trip through create/update; absent fields stay NULL.

This stays a RECORD: nothing reads any of these fields to drive execution.
"""

import sqlite3

from src.control.db import MeshDB, FLOW_STAGES, _CURRENT_VERSION, _now


# Every column migration 22 adds. All TEXT/NULLable.
_A21_COLUMNS = {
    "approved_plan",
    "plan_review",
    "burn_down_items",
    "execution_result",
    "implementation_review",
    "waived_findings",
    "closure_summary",
    "role_assignments",
    "artifact_links",
    "status",
    "updated_at",
    "parent_flow_run_id",
    "dispatched_by",
    "dispatch_file",
}

_A19_COLUMNS = {
    "flow_run_id",
    "task_id",
    "current_stage",
    "objective_lock",
    "created_at",
}


def _fresh_db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _cols(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(flow_runs)").fetchall()}


# ---------------------------------------------------------------------------
# (1) Fresh DB: migration applies, version 22, all columns present.
# ---------------------------------------------------------------------------

def test_fresh_db_has_full_schema(tmp_path):
    db = _fresh_db(tmp_path)
    conn = db._conn()

    max_version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    # A fresh DB now converges at 23 (A25 substrate); migration 22's columns
    # (asserted below) remain part of that convergence.
    assert max_version == 23
    assert _CURRENT_VERSION == 23

    cols = _cols(conn)
    assert _A19_COLUMNS <= cols, "A19 columns must remain (additive-only)"
    assert _A21_COLUMNS <= cols, "all §11 + lineage columns present"


# ---------------------------------------------------------------------------
# (2) Existing (pre-migration) DB: build a raw version-21 5-column DB, open it
#     with MeshDB, and confirm it migrates to 22 while old rows still read.
# ---------------------------------------------------------------------------

def _build_v21_db(path: str) -> str:
    """Construct a minimal DB pinned at schema_version 21 with the old
    5-column flow_runs table and one pre-existing row. Returns the flow_run_id."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT)"
    )
    conn.execute(
        """
        CREATE TABLE flow_runs (
            flow_run_id     TEXT PRIMARY KEY,
            task_id         TEXT,
            current_stage   TEXT,
            objective_lock  TEXT,
            created_at      TEXT
        )
        """
    )
    # Mark every version up through 21 as applied so MeshDB only runs migration 22.
    for v in range(1, 22):
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (v, _now()),
        )
    conn.execute(
        "INSERT INTO flow_runs (flow_run_id, task_id, current_stage, objective_lock, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("legacy-1", "task-legacy", "dispatch_start", '{"lock":"y"}', _now()),
    )
    conn.commit()
    conn.close()
    return "legacy-1"


def test_existing_db_migrates_and_legacy_row_reads(tmp_path):
    path = str(tmp_path / "mesh.db")
    legacy_id = _build_v21_db(path)

    # Sanity: raw DB is 5 columns at version 21 before MeshDB touches it.
    raw = sqlite3.connect(path)
    assert _cols(raw) == _A19_COLUMNS
    assert raw.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 21
    raw.close()

    # Opening with MeshDB applies migration 22 (and the later 23 substrate);
    # the v21 DB converges at the current version with migration-22's columns.
    db = MeshDB(path)
    conn = db._conn()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 23
    assert _A21_COLUMNS <= _cols(conn)

    # The pre-existing 5-column row still reads; new fields are NULL.
    row = db.get_flow_run(legacy_id)
    assert row is not None
    assert row["task_id"] == "task-legacy"
    assert row["current_stage"] == "dispatch_start"
    assert row["objective_lock"] == '{"lock":"y"}'
    for col in _A21_COLUMNS:
        assert row[col] is None, f"legacy row {col} should be NULL, got {row[col]!r}"


# ---------------------------------------------------------------------------
# (3) Idempotent: re-opening an already-migrated DB is a no-op.
# ---------------------------------------------------------------------------

def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "mesh.db")

    db1 = MeshDB(path)
    v1 = db1._conn().execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    n1 = db1._conn().execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]

    # Re-open the same file — migrations must not re-run or error.
    db2 = MeshDB(path)
    conn = db2._conn()
    v2 = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    n2 = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]

    assert v1 == v2 == 23
    assert n1 == n2, "no duplicate schema_version rows on re-open"
    assert _A21_COLUMNS <= _cols(conn)


# ---------------------------------------------------------------------------
# (4) New fields round-trip through create_flow_run; absent fields stay NULL.
# ---------------------------------------------------------------------------

def test_create_persists_new_fields(tmp_path):
    db = _fresh_db(tmp_path)

    fid = db.create_flow_run(
        "task-full",
        "plan",
        objective_lock='{"lock":"z"}',
        approved_plan='{"steps":[1,2]}',
        plan_review='{"verdict":"approve"}',
        burn_down_items='["a","b"]',
        status="in_progress",
        parent_flow_run_id="parent-1",
        dispatched_by="A20",
        dispatch_file=".ai/dispatch/AGENT_21.md",
    )

    row = db.get_flow_run(fid)
    assert row is not None
    assert row["approved_plan"] == '{"steps":[1,2]}'
    assert row["plan_review"] == '{"verdict":"approve"}'
    assert row["burn_down_items"] == '["a","b"]'
    assert row["status"] == "in_progress"
    assert row["parent_flow_run_id"] == "parent-1"
    assert row["dispatched_by"] == "A20"
    assert row["dispatch_file"] == ".ai/dispatch/AGENT_21.md"

    # Fields NOT passed stay NULL (updated_at is NULL on create).
    for col in {
        "execution_result",
        "implementation_review",
        "waived_findings",
        "closure_summary",
        "role_assignments",
        "artifact_links",
        "updated_at",
    }:
        assert row[col] is None, f"{col} should be NULL on create"


# ---------------------------------------------------------------------------
# (5) A19 3-arg create leaves every new field NULL (byte-identical write path).
# ---------------------------------------------------------------------------

def test_a19_create_leaves_new_fields_null(tmp_path):
    db = _fresh_db(tmp_path)
    fid = db.create_flow_run("task-a19", "dispatch_start", objective_lock='{"x":1}')

    row = db.get_flow_run(fid)
    assert row["current_stage"] == "dispatch_start"
    assert row["objective_lock"] == '{"x":1}'
    for col in _A21_COLUMNS:
        assert row[col] is None, f"{col} should be NULL for A19-style create"


# ---------------------------------------------------------------------------
# (6) update_flow_run persists a subset and stamps updated_at.
# ---------------------------------------------------------------------------

def test_update_flow_run_subset_and_timestamp(tmp_path):
    db = _fresh_db(tmp_path)
    fid = db.create_flow_run("task-u", "plan")
    assert db.get_flow_run(fid)["updated_at"] is None

    db.update_flow_run(
        fid,
        current_stage="impl_review",
        implementation_review='{"verdict":"approve"}',
        closure_summary="done",
    )
    row = db.get_flow_run(fid)
    assert row["current_stage"] == "impl_review"
    assert row["implementation_review"] == '{"verdict":"approve"}'
    assert row["closure_summary"] == "done"
    assert row["updated_at"] is not None, "update must stamp updated_at"
    # Untouched field stays NULL.
    assert row["approved_plan"] is None


# ---------------------------------------------------------------------------
# (7) update_flow_stage stamps updated_at (A19 path extended).
# ---------------------------------------------------------------------------

def test_update_flow_stage_stamps_updated_at(tmp_path):
    db = _fresh_db(tmp_path)
    fid = db.create_flow_run("task-s", "dispatch_start")
    assert db.get_flow_run(fid)["updated_at"] is None

    db.update_flow_stage(fid, "queued")
    row = db.get_flow_run(fid)
    assert row["current_stage"] == "queued"
    assert row["updated_at"] is not None


# ---------------------------------------------------------------------------
# (8) Unknown field names are rejected (guards against silent column typos).
# ---------------------------------------------------------------------------

def test_unknown_field_rejected(tmp_path):
    db = _fresh_db(tmp_path)
    try:
        db.create_flow_run("task-x", "plan", worker_task_ids='["nope"]')
        assert False, "expected ValueError for unknown field"
    except ValueError as e:
        assert "worker_task_ids" in str(e)

    fid = db.create_flow_run("task-y", "plan")
    try:
        db.update_flow_run(fid, bogus_col="v")
        assert False, "expected ValueError for unknown update field"
    except ValueError as e:
        assert "bogus_col" in str(e)


# ---------------------------------------------------------------------------
# (9) get_flow_run returns None for a missing id.
# ---------------------------------------------------------------------------

def test_get_flow_run_missing(tmp_path):
    db = _fresh_db(tmp_path)
    assert db.get_flow_run("does-not-exist") is None


# ---------------------------------------------------------------------------
# (10) Stage vocabulary constant is the canonical ordered §11 sequence.
# ---------------------------------------------------------------------------

def test_flow_stages_vocabulary():
    assert FLOW_STAGES == (
        "intent",
        "objective_lock",
        "plan",
        "plan_review",
        "execution",
        "impl_review",
        "closure",
    )
    # A19 legacy free-text values remain writable (constant does not constrain).
    # (No CHECK on current_stage — verified by test_update_flow_stage above.)
