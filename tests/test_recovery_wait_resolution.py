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
import asyncio
import json
import types

from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _orch() -> TaskOrchestrator:
    return TaskOrchestrator.__new__(TaskOrchestrator)


def _patch_db(monkeypatch, db) -> None:
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)


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
    """A104: the inbox row is written in the child's TERMINAL txn, whichever path
    terminalises it — here the lost-carrier reaper (``synthesize_managed_terminal``),
    which never emits ``task.finished`` itself. Reconcile still resolves it, with
    no ``task.finished`` and no wait marker involved."""
    from tests.inbox_seed import seed_child

    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")
    db = _db(tmp_path)
    case_id = db.open_case("obj", "mgr-1", role="manager")
    seed_child(db, case_id, "task_t", requester="mgr-1")
    assert [p["task_id"] for p in db.reconcile_worker_waits(case_id)["pending"]] == ["task_t"]
    assert db.synthesize_managed_terminal("task_t") == "synthesized"

    out = db.reconcile_worker_waits(case_id)
    assert out["ok"] is True
    assert out["resolved"] == [{"task_id": "task_t", "outcome": "failed"}]
    assert out["pending"] == []
    assert _finished(db, case_id) == []  # resolved from task truth, not the ledger


# --- cross-path invariant: a task id ALONE must reach the right Case ------------

def test_emit_task_finished_resolves_case_from_lineage(tmp_path, monkeypatch):
    # The seam the recovery path depends on: given ONLY a task id (no flow_run_id
    # hint), the terminal fact must land on the Case resolved from the durable
    # task->Case lineage. If this breaks, recovery silently drops task.finished
    # again — exactly the live regression.
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    case_id = db.open_case("obj", "mgr-1", role="manager")
    db.create_flow_link(case_id, "task", "task_lin", "task")

    _orch()._emit_task_finished("task_lin", success=True)  # no flow_run_id hint

    fin = _finished(db, case_id)
    assert [e["entity_id"] for e in fin] == ["task_lin"]
    assert json.loads(fin[0]["payload_json"])["outcome"] == "success"


def test_recover_completed_session_emits_task_finished(tmp_path, monkeypatch):
    # THE regression guard for the live incident (Case 99330a8f9e): the recovery
    # path must emit the durable task.finished fact so a Manager's wait-group
    # resolves. Drives the REAL _recover_completed_session; stubbed
    # session_store/notifier isolate the one effect under test. Deleting the emit
    # from recovery fails this test.
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    case_id = db.open_case("obj", "mgr-1", role="manager")
    db.create_flow_link(case_id, "task", "task_rec", "task")
    _arm_group(db, case_id, "g", ["task_rec"], condition="ALL")
    assert db.compute_continuation_tick(case_id)["satisfied"] is False  # the bug state

    orch = _orch()
    orch.session_store = types.SimpleNamespace(save=lambda s: None)

    async def _noop_notify(*a, **k):
        return None

    orch.notifier = types.SimpleNamespace(notify_task_outcome=_noop_notify)
    orch._write_session_summary = lambda *a, **k: None
    orch._append_session_event = lambda *a, **k: None
    orch._emit_event = lambda *a, **k: None

    session = types.SimpleNamespace(
        session_id="sess-w", status=None, last_result_summary="",
        last_files_modified=[], backend_session_id="", last_artifact_path="",
        task_history=[], last_user_message="do it", telegram_chat_id=None,
        backend="claude",
    )
    task_row = {"id": "task_rec", "result": json.dumps({"output": "done"}), "artifact_path": ""}

    asyncio.run(orch._recover_completed_session(session, task_row))

    # The durable fact landed -> the dangling wait now resolves (Manager woken).
    assert [e["entity_id"] for e in _finished(db, case_id)] == ["task_rec"]
    assert db.compute_continuation_tick(case_id)["satisfied"] is True
