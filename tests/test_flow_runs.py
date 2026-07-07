"""
A19 — FlowRun record (v0.4 §13 item 1) tests.

Covers the smallest additive slice: migration 21 applies (flow_runs table +
version bump), the create/update/list round-trip works with the task_id filter,
and the orchestrator's best-effort write hook swallows a DB write failure so a
telemetry write can never fail or delay a real task.

This is a RECORD, not a stage machine — nothing here (or anywhere) reads
current_stage to drive behavior.
"""

import types

from src.control.db import MeshDB, _CURRENT_VERSION
from src.orchestrator import TaskOrchestrator


def _fresh_db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


# ---------------------------------------------------------------------------
# (a) Migration: fresh temp DB converges at version 22 and has the table.
#     A21 promoted the record to the full §11 model, so the 5 A19 columns
#     remain (byte-identical writes) alongside the new NULLable columns.
# ---------------------------------------------------------------------------

def test_migration_version_and_table(tmp_path):
    db = _fresh_db(tmp_path)
    conn = db._conn()

    max_version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert max_version == 22
    assert _CURRENT_VERSION == 22

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='flow_runs'"
    ).fetchone()
    assert row is not None, "flow_runs table missing after migration"

    # The 5 original A19 columns must still be present (additive-only guarantee).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(flow_runs)").fetchall()}
    assert {
        "flow_run_id",
        "task_id",
        "current_stage",
        "objective_lock",
        "created_at",
    } <= cols


# ---------------------------------------------------------------------------
# (b) create / update / list round-trip incl. task_id filter.
# ---------------------------------------------------------------------------

def test_create_update_list_round_trip(tmp_path):
    db = _fresh_db(tmp_path)

    fid = db.create_flow_run("task-A", "dispatch_start", objective_lock='{"lock":"x"}')
    assert isinstance(fid, str) and fid

    rows = db.list_flow_runs(task_id="task-A")
    assert len(rows) == 1
    r = rows[0]
    assert r["flow_run_id"] == fid
    assert r["task_id"] == "task-A"
    assert r["current_stage"] == "dispatch_start"
    assert r["objective_lock"] == '{"lock":"x"}'
    assert r["created_at"]

    # update_flow_stage changes the recorded stage.
    db.update_flow_stage(fid, "queued")
    r2 = db.list_flow_runs(task_id="task-A")[0]
    assert r2["current_stage"] == "queued"

    # A second flow for a different task; task_id filter isolates it.
    db.create_flow_run("task-B", "dispatch_start")
    assert len(db.list_flow_runs(task_id="task-A")) == 1
    assert len(db.list_flow_runs(task_id="task-B")) == 1
    assert len(db.list_flow_runs()) == 2


# ---------------------------------------------------------------------------
# (c) The orchestrator hook swallows a DB write failure.
# ---------------------------------------------------------------------------

def test_orchestrator_hook_swallows_failure(monkeypatch):
    """Monkeypatch create_flow_run to raise → the best-effort hook must NOT
    propagate the exception (task path unaffected) and must return None."""

    class _BoomDB:
        def create_flow_run(self, *a, **k):
            raise RuntimeError("db write boom")

        def update_flow_stage(self, *a, **k):
            raise RuntimeError("db write boom")

    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: _BoomDB())

    # Call the helpers as unbound methods with a lightweight stub self — this
    # exercises the exact try/except without spinning up a full orchestrator.
    fake_self = types.SimpleNamespace()
    task = types.SimpleNamespace(id="task-boom")

    # Must not raise, and returns None because the write failed.
    result = TaskOrchestrator._record_flow_run_start(fake_self, task)
    assert result is None

    # Stage update with a truthy id must also swallow the failure.
    TaskOrchestrator._record_flow_stage(fake_self, "flow-123", "queued")

    # A None flow_run_id short-circuits before touching the DB.
    TaskOrchestrator._record_flow_stage(fake_self, None, "queued")
