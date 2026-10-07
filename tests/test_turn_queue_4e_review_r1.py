"""A82 Stage 4e review round 1 — regression tests for findings F2-F5.

Real pieces: a file-backed ``MeshDB`` as ``get_db()``, REAL bound orchestrator
methods on a bare instance, the REAL scheduler pass (``_prepare_managed_turn``
→ activation-time revalidation → withdrawal) and the REAL DB seams. No
backend/CLI (autouse spawn guard from the producer-1 suite).
"""
import asyncio
from typing import Any, Dict, List

import pytest

from src.control import turn_admission as ta
from src.core.interfaces import Session, SessionStatus
from tests.test_turn_queue_4b import _pass, _wire
from tests.test_turn_queue_producer1 import (  # noqa: F401
    NOW, _flags, _no_cli_spawn, _setup,
)
from tests.stage8a_legacy import enqueue_pre_cutover, unenrolled


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch):
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


def _enrolled(db: Any, sid: str) -> None:
    db.upsert_session(Session(
        session_id=sid, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a",
    ))
    db.enroll_session(sid)


def _rebound_case(db: Any, old: str, new: str) -> str:
    """A Case whose Manager binding moved from ``old`` to ``new``."""
    case_id: str = db.open_case("objective", old, role="manager")
    _enrolled(db, new)
    db.create_flow_link(case_id, "session", new, "manager")
    return case_id


def _automation(db: Any, tid: str, sid: str, case_id: str, kind: str,
                payload: Dict[str, Any] | None = None) -> None:
    db.enqueue_turn(tid, sid, body=f"{kind} notification", turn_source="system",
                    turn_kind=kind, idempotency_scope=f"automation:{sid}:{kind}",
                    flow_run_id=case_id, payload=payload, machine_id="worker-a")


def _row(db: Any, tid: str) -> Dict[str, Any]:
    return dict(db.get_task(tid))


# ---------------------------------------------------------------- F2

def test_watched_job_on_rebound_manager_withdrawn_on_first_pass_with_audit(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    case_id = _rebound_case(db, "sess-1", "sess-new")
    _automation(db, "job-turn", "sess-1", case_id, "watched_job",
                payload={"metadata": {"job_id": "job-42"}})

    res = _pass(db, o)

    row = _row(db, "job-turn")
    assert row["status"] == "withdrawn", row
    actor = db._conn().execute(
        "SELECT actor FROM mesh_turn_revisions WHERE task_id = 'job-turn' "
        "AND change_kind = 'withdraw'").fetchone()["actor"]
    assert actor == "scheduler:obsolete:manager_rebound"
    assert res.withdrawn == 1 and res.ineligible == 0
    audit = db.get_task("job-42")
    assert audit is not None and audit["status"] == "completed"
    assert "manager_rebound" in str(audit["reply_text"])
    # Nothing left for head selection to re-select.
    assert db.select_eligible_turn_heads() == []


def test_heartbeat_on_rebound_manager_withdrawn_not_reselected(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    case_id = _rebound_case(db, "sess-1", "sess-new")
    _automation(db, "hb-turn", "sess-1", case_id, "heartbeat")

    async def _otherwise_valid(_db: Any, _row: Dict[str, Any]) -> None:
        return None  # every heartbeat-specific check passes

    o._managed_heartbeat_obsolete = _otherwise_valid
    res = _pass(db, o)
    assert _row(db, "hb-turn")["status"] == "withdrawn"
    assert res.withdrawn == 1 and res.ineligible == 0


def test_stale_automation_cannot_starve_a_human_past_the_head_limit(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    stale: List[str] = []
    for i in range(26):
        old, new = f"old-{i}", f"new-{i}"
        _enrolled(db, old)
        case_id = _rebound_case(db, old, new)
        _automation(db, f"job-{i}", old, case_id, "watched_job",
                    payload={"metadata": {"job_id": f"jobid-{i}"}})
        stale.append(f"job-{i}")
    db.enqueue_turn("human", "sess-1", body="do it", turn_source="human",
                    machine_id="worker-a")

    first = _pass(db, o)
    assert first.selected == 25 and first.withdrawn == 25 and first.ineligible == 0
    _pass(db, o)
    assert _row(db, "human")["status"] == "pending"
    assert all(_row(db, t)["status"] == "withdrawn" for t in stale)


def test_withdraw_rebound_automation_covers_every_case_automation_kind(tmp_path, monkeypatch):
    db, _o = _setup(tmp_path, monkeypatch)
    case_id = _rebound_case(db, "sess-1", "sess-new")
    _automation(db, "job-turn", "sess-1", case_id, "watched_job",
                payload={"metadata": {"job_id": "job-7"}})
    db.enqueue_turn("human", "sess-1", body="mine", turn_source="human", flow_run_id=case_id)
    out = db.withdraw_rebound_automation("sess-1", case_id, actor="respawn")
    assert out == ["job-turn"]
    assert _row(db, "human")["status"] == "queued"
    assert db.get_task("job-7")["status"] == "completed"  # 4d audit record


# ---------------------------------------------------------------- F3

def _fence_db(tmp_path: Any) -> Any:
    from src.control.db import MeshDB

    db = MeshDB(str(tmp_path / "fence.db"))
    _enrolled(db, "s")
    db.upsert_node(node_id="worker-a", tailscale_ip="100.64.0.10", api_port=9001,
                   backends=["claude"], max_concurrent=2, incarnation_id="inc-1")
    return db


def _pending(db: Any, tid: str) -> None:
    db.enqueue_turn(tid, "s", body="x", turn_source="human", machine_id="worker-a")
    assert db.activate_turn(tid)


def test_claim_refused_for_a_superseded_incarnation(tmp_path):
    from src.control.turn_queue import OwnershipConflictError

    db = _fence_db(tmp_path)
    db.upsert_node(node_id="worker-a", tailscale_ip="100.64.0.10", api_port=9001,
                   backends=["claude"], max_concurrent=2, incarnation_id="inc-2")
    _pending(db, "t1")
    for stale in ("inc-1", None):
        with pytest.raises(OwnershipConflictError):
            db.claim_turn("t1", node_id="worker-a", carrier_kind="worker", incarnation_id=stale)
    assert _row(db, "t1")["status"] == "pending"
    tok = db.claim_turn("t1", node_id="worker-a", carrier_kind="worker", incarnation_id="inc-2")
    assert db.start_turn("t1", claim_token=str(tok), incarnation_id="inc-2").status == "running"


def test_zombie_cannot_supersede_the_live_incarnation_claim(tmp_path):
    from src.control.turn_queue import OwnershipConflictError

    db = _fence_db(tmp_path)
    db.upsert_node(node_id="worker-a", tailscale_ip="100.64.0.10", api_port=9001,
                   backends=["claude"], max_concurrent=2, incarnation_id="inc-2")
    _pending(db, "t2")
    tok = db.claim_turn("t2", node_id="worker-a", carrier_kind="worker", incarnation_id="inc-2")
    with pytest.raises(OwnershipConflictError):
        db.claim_turn("t2", node_id="worker-a", carrier_kind="worker", incarnation_id="inc-1")
    assert db.start_turn("t2", claim_token=str(tok), incarnation_id="inc-2").status == "running"


@pytest.mark.parametrize("presented", ["inc-1", None])
def test_start_refused_after_node_reregistered_new_incarnation(tmp_path, presented):
    from src.control.turn_queue import OwnershipConflictError

    db = _fence_db(tmp_path)
    _pending(db, "t3")
    tok = db.claim_turn("t3", node_id="worker-a", carrier_kind="worker", incarnation_id="inc-1")
    # The restart registered inc-2 (the release hook may not have run yet).
    db.upsert_node(node_id="worker-a", tailscale_ip="100.64.0.10", api_port=9001,
                   backends=["claude"], max_concurrent=2, incarnation_id="inc-2")
    with pytest.raises(OwnershipConflictError):
        db.start_turn("t3", claim_token=str(tok), incarnation_id=presented)
    assert _row(db, "t3")["status"] == "claimed"


def test_superseded_grant_release_covers_null_claim_incarnation(tmp_path):
    db = _fence_db(tmp_path)
    _pending(db, "t4")
    tok = db.claim_turn("t4", node_id="worker-a", carrier_kind="worker", incarnation_id="inc-1")
    with db._write() as conn:  # a pre-fence grant minted without an incarnation
        conn.execute("UPDATE mesh_tasks SET claim_incarnation = NULL WHERE id = 't4'")
    assert str(tok)
    assert db.release_superseded_managed_grants("worker-a", "inc-2") == ["t4"]
    assert _row(db, "t4")["status"] == "pending"


def test_managed_claim_payload_requires_incarnation():
    from pydantic import ValidationError

    from src.control.task_server import ManagedClaimPayload

    with pytest.raises(ValidationError):
        ManagedClaimPayload(node_id="worker-a")
    with pytest.raises(ValidationError):
        ManagedClaimPayload(node_id="worker-a", incarnation_id="")
    assert ManagedClaimPayload(node_id="worker-a", incarnation_id="inc-1").incarnation_id == "inc-1"


def test_registration_hook_failure_logged_at_warning(monkeypatch, caplog):
    import logging

    from src.control import db as db_mod
    from src.control.node_registry import NodeRegistry

    class _Broken:
        def release_superseded_managed_grants(self, *_a: Any) -> List[str]:
            raise RuntimeError("db locked")

    monkeypatch.setattr(db_mod, "get_db", lambda: _Broken())
    reg = NodeRegistry.__new__(NodeRegistry)
    with caplog.at_level(logging.WARNING, logger="src.control.node_registry"):
        assert reg._db_release_superseded_managed_grants("worker-a", "inc-2") == []
    assert any("db_release_superseded_grants_err" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- F4

def test_legacy_execution_insert_refused_for_enrolled_session(tmp_path):
    from src.control.db import MeshDB
    from src.control.turn_queue import LegacyExecutionRefusedError

    db = MeshDB(str(tmp_path / "l.db"))
    _enrolled(db, "s")
    for action in ("create_session", "resume_session", "compact_session"):
        with pytest.raises(LegacyExecutionRefusedError):
            db.enqueue_task(f"legacy-{action}", "s", "worker-a", "claude", action,
                            {"prompt": "x"})
        assert db.get_task(f"legacy-{action}") is None
    # Control rows stay allowed for an enrolled session.
    for action in ("close_session", "cancel_turn"):
        db.enqueue_task(f"ctl-{action}", "s", "worker-a", "claude", action, {})
        assert db.get_task(f"ctl-{action}")["status"] == "pending"
    # [A82 Stage 8a] The fence is unconditional: an UNENROLLED session's legacy
    # execution row is refused too (was: "unchanged").
    db.upsert_session(Session(session_id="u", backend="claude", repo_path="/tmp/repo",
                              status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    unenrolled(db, "u")
    with pytest.raises(LegacyExecutionRefusedError):
        db.enqueue_task("legacy-u", "u", "worker-a", "claude", "resume_session", {"prompt": "x"})
    assert db.get_task("legacy-u") is None


def test_legacy_execution_claim_refused_for_enrolled_session(tmp_path):
    from src.control.db import MeshDB
    from src.control.turn_queue import LegacyExecutionRefusedError

    db = MeshDB(str(tmp_path / "l.db"))
    db.upsert_session(Session(session_id="s", backend="claude", repo_path="/tmp/repo",
                              status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    # Inserted while the session was still legacy (pre-cutover); enrolled before a poll.
    enqueue_pre_cutover(db, "legacy-1", "s", "worker-a", "claude", "resume_session", {"prompt": "x"})
    db.enroll_session("s")
    with pytest.raises(LegacyExecutionRefusedError):
        db.claim_task("legacy-1", "worker-a")
    row = _row(db, "legacy-1")
    assert row["status"] == "failed" and row["claimed_by"] is None
    assert "legacy_execution_refused" in str(row["error"])
    assert db.claim_task("legacy-1", "worker-a") is False  # terminal: never re-offered


def test_reconcile_row_is_never_claimable(tmp_path, monkeypatch):
    """The spool-replay row is inserted directly in its terminal state: a
    remote poll between the insert and the finalize cannot claim (re-run) it."""
    from src.core.interfaces import Task, TaskPriority, TaskResult, TaskStatus, TaskType

    db, o = _setup(tmp_path, monkeypatch, enroll=False)  # a legacy session's replay
    seen: List[Any] = []
    real_complete = o._mesh_complete_task

    def _poll_in_gap(task: Any, result: Any, artifact: Any) -> None:
        seen.append((_row(db, task.id)["status"], db.claim_task(task.id, "worker-a")))
        real_complete(task, result, artifact)

    o._mesh_complete_task = _poll_in_gap
    task = Task(id="spooled-1", type=TaskType.ANALYZE, priority=TaskPriority.MEDIUM,
                status=TaskStatus.COMPLETED, created=NOW, title="t", target_files=[],
                prompt="p", success_criteria=[], context="",
                metadata={"session_id": "sess-1"})
    result = TaskResult(task_id="spooled-1", success=True, output="done", errors=[],
                        files_modified=[], execution_time=0.0, timestamp=NOW)
    o._ensure_reconcile_task_row(db, task, result)
    o._mesh_complete_task(task, result, None)
    assert seen == [("completed", False)]
    assert _row(db, "spooled-1")["status"] == "completed"


# ---------------------------------------------------------------- F5

def test_unbound_quota_resume_refused_while_a_pause_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    case_id: str = db.open_case("objective", "sess-1", role="manager")
    db.append_flow_event(case_id, "flow.quota_paused", "system", entity_type="task",
                         entity_id="failed-a", payload={"paused_task_id": "failed-a"})
    pause = db.case_quota_pause(case_id)
    assert pause is not None
    out: Dict[str, Any] = {"ok": False, "reason": "", "case_id": case_id,
                           "mode": "in_place", "session_id": None}
    res = asyncio.run(o._quota_resume_managed(
        db, case_id, db.get_flow_run(case_id), pause, "other-task",
        o.session_store.get("sess-1"), "operator", out,
    ))
    assert res["ok"] is False and res["reason"] == "pause_not_bound"
    assert db._conn().execute(
        "SELECT COUNT(*) FROM mesh_tasks WHERE queue_protocol = 1").fetchone()[0] == 0
    assert "case_resumed" not in o.events
