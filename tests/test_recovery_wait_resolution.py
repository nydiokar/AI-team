"""
recovery-wait-resolution — a worker task that reaches a terminal state via a path
that does NOT emit ``task.finished`` (restart recovery, reapers) must still resolve
its Manager's wait.

Root cause context (live incident, Case ``99330a8f9e…``): the live result path
emits the durable ``task.finished`` ledger fact that the wake-dispatcher reads
(``compute_continuation_tick``) and that ``reconcile_worker_waits`` reads. The
recovery path (``_recover_completed_session``, taken when the gateway is recreated
mid-turn) updated only the session row, so the UI showed the worker ``closed``
while the Manager's wait-group dangled forever — burning cache-heartbeat turns.

``backfill_missing_task_finished`` reconciles the ledger from TASK TRUTH so the
single durable fact is sufficient to wake the Manager regardless of which path
finalised the worker. Covers BOTH wait subsystems: M3.4 wait-groups and A46
per-task waits.
"""
import json

from src.control.db import MeshDB


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _task(db: MeshDB, task_id: str, status: str = "completed") -> None:
    # Insert a task directly in a terminal state — mimics a row that was written
    # by the live/remote completion while the gateway was down (no event emitted).
    db.enqueue_task(task_id, None, None, "claude", "resume_session", {}, status=status)


def _arm_group(db: MeshDB, case_id: str, gid: str, members, condition: str = "ALL") -> None:
    db.append_flow_event(
        case_id, "worker.wait_pending", "manager",
        entity_type="wait_group", entity_id=gid,
        payload={"wait_group_id": gid, "condition": condition, "member_task_ids": members},
    )


def _finished(db: MeshDB, case_id: str):
    return [e for e in db.list_flow_events(case_id) if e["event_type"] == "task.finished"]


# --- the incident: wait-group member completed-in-DB but no task.finished -------

def test_backfill_emits_finished_for_completed_group_member(tmp_path):
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-1", role="manager")
    _task(db, "task_9dfd", status="completed")
    _arm_group(db, case_id, "p1.3", ["task_9dfd"], condition="ALL")

    # No task.finished yet -> the wake loop sees nothing satisfied (the live bug).
    assert db.compute_continuation_tick(case_id)["satisfied"] is False

    backfilled = db.backfill_missing_task_finished(case_id)
    assert backfilled == ["task_9dfd"]
    fin = _finished(db, case_id)
    assert len(fin) == 1
    assert json.loads(fin[0]["payload_json"])["outcome"] == "success"

    # The single durable fact is now present -> the Manager would be woken.
    assert db.compute_continuation_tick(case_id)["satisfied"] is True


def test_backfill_skips_still_running_member(tmp_path):
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-1", role="manager")
    _task(db, "task_run", status="claimed")  # genuinely still running
    _arm_group(db, case_id, "g", ["task_run"])
    assert db.backfill_missing_task_finished(case_id) == []
    assert _finished(db, case_id) == []


def test_backfill_idempotent(tmp_path):
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-1", role="manager")
    _task(db, "task_x")
    _arm_group(db, case_id, "g", ["task_x"])
    assert db.backfill_missing_task_finished(case_id) == ["task_x"]
    assert db.backfill_missing_task_finished(case_id) == []  # event already present
    assert len(_finished(db, case_id)) == 1


def test_backfill_failed_outcome_from_task_status(tmp_path):
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-1", role="manager")
    _task(db, "task_f", status="failed")
    _arm_group(db, case_id, "g", ["task_f"])
    assert db.backfill_missing_task_finished(case_id) == ["task_f"]
    assert json.loads(_finished(db, case_id)[0]["payload_json"])["outcome"] == "failed"


def test_backfill_ignores_resolved_group(tmp_path):
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-1", role="manager")
    _task(db, "task_done")
    _arm_group(db, case_id, "g", ["task_done"])
    db.append_flow_event(
        case_id, "worker.wait_resolved", "system",
        entity_type="wait_group", entity_id="g",
        payload={"wait_group_id": "g", "outcome": "drained"},
    )
    assert db.backfill_missing_task_finished(case_id) == []


# --- A46 per-task waits also covered, end to end via reconcile ------------------

def test_reconcile_resolves_from_task_truth(tmp_path, monkeypatch):
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-1", role="manager")
    db.record_worker_wait(case_id, "task_t")  # A46 per-task wait (entity_type='task')
    _task(db, "task_t", status="completed")

    # No task.finished was ever emitted (recovery path) — reconcile must still
    # resolve it from the task-row truth (via the backfill it now runs first).
    out = db.reconcile_worker_waits(case_id)
    assert out["ok"] is True
    assert [r["task_id"] for r in out["resolved"]] == ["task_t"]
