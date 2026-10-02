"""Stage 4e adversarial races at the canonical DB transaction boundary."""

import asyncio
from datetime import datetime, timezone

import pytest

from src.control.db import MeshDB, RESPAWN_ACTION, TRANSIENT_RESUME_ACTION
from src.control.turn_queue import OwnershipConflictError
from src.core.interfaces import Session, SessionStatus
from src.orchestrator import TaskOrchestrator


NOW = datetime(2026, 9, 28, tzinfo=timezone.utc).isoformat()


def _session(db: MeshDB, sid: str) -> None:
    db.upsert_session(Session(
        session_id=sid, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.AWAITING_INPUT, created_at=NOW, updated_at=NOW,
        machine_id="worker-a",
    ))
    db.enroll_session(sid)


def test_retry_admission_rechecks_intervening_human_inside_transaction(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    _session(db, "manager")
    db.enqueue_turn("failed-a", "manager", body="A", turn_source="human")
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET status='failed' WHERE id='failed-a'")
    assert db.retry_decision("manager", "failed-a").action == "retry"
    db.enqueue_turn("human-b", "manager", body="B", turn_source="human")

    with pytest.raises(OwnershipConflictError):
        db.enqueue_turn(
            "retry-r", "manager", body="retry A", turn_source="system",
            turn_kind="retry", parent_task_id="failed-a",
        )
    assert db.get_task("retry-r") is None
    assert db.get_task("human-b")["queue_sequence"] == 2


def test_successor_waits_for_pending_and_recorded_retry_pause(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    _session(db, "manager")
    case_id = db.open_case("objective", "manager", role="manager")
    db.enqueue_turn("failed-a", "manager", body="A", turn_source="human", flow_run_id=case_id)
    db.enqueue_turn("human-b", "manager", body="B", turn_source="human", flow_run_id=case_id)
    with db._write() as conn:
        conn.execute(
            "UPDATE mesh_tasks SET status='failed', retry_pause_state='pending' WHERE id='failed-a'"
        )

    assert db.select_eligible_turn_heads() == []
    revision = int(db.get_session("manager")["config_revision"])
    assert db.activate_prepared_turn(
        "human-b", expected_revision=1, expected_config_revision=revision,
        action="resume_session", payload={"prompt": "B"}, machine_id="worker-a",
    ) == "ineligible"

    db.append_flow_event(case_id, "flow.quota_paused", "system", entity_type="task",
                         entity_id="failed-a", payload={"paused_task_id": "failed-a"})
    db.mark_retry_pause_done("failed-a")
    assert db.select_eligible_turn_heads() == []
    assert db.activate_prepared_turn(
        "human-b", expected_revision=1, expected_config_revision=revision,
        action="resume_session", payload={"prompt": "B"}, machine_id="worker-a",
    ) == "ineligible"

    db.append_flow_event(case_id, "flow.quota_pause_declined", "operator")
    assert [r["id"] for r in db.select_eligible_turn_heads()] == ["human-b"]


def test_linked_retry_runs_through_its_pause_and_renewed_failure_holds_b(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    _session(db, "manager")
    case_id = db.open_case("objective", "manager", role="manager")
    db.enqueue_turn("failed-a", "manager", body="A", turn_source="human", flow_run_id=case_id)
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET status='failed' WHERE id='failed-a'")
    event_id = db.append_flow_event(
        case_id, "flow.transient_paused", "system", entity_type="task",
        entity_id="failed-a", payload={"paused_task_id": "failed-a"},
    )
    token_id = f"tresume:{case_id}:failed-a:1"
    meta = {"case_id": case_id, "pause_event_id": event_id, "paused_task_id": "failed-a"}
    db.enqueue_task(token_id, None, None, "claude", TRANSIENT_RESUME_ACTION, meta)
    db.enqueue_turn(
        "retry-r", "manager", body="retry A", turn_source="system", turn_kind="retry",
        parent_task_id="failed-a", flow_run_id=case_id, producer_token=token_id,
        producer_meta=meta,
    )
    db.enqueue_turn("human-b", "manager", body="B", turn_source="human", flow_run_id=case_id)
    assert [r["id"] for r in db.select_eligible_turn_heads()] == ["retry-r"]
    revision = int(db.get_session("manager")["config_revision"])
    assert db.activate_prepared_turn(
        "retry-r", expected_revision=1, expected_config_revision=revision,
        action="resume_session", payload={"prompt": "retry A"}, machine_id="worker-a",
    ) == "activated"
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET status='failed', retry_pause_state='pending' WHERE id='retry-r'")
    db.append_flow_event(case_id, "flow.transient_resumed", "system")
    assert db.select_eligible_turn_heads() == []


def test_pause_gate_uses_turn_case_and_does_not_hold_case_worker(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    _session(db, "manager")
    _session(db, "worker")
    case_a = db.open_case("A", "manager", role="manager")
    case_b = db.open_case("B", "manager", role="manager")
    db.append_flow_event(case_a, "flow.quota_paused", "system", entity_type="task",
                         entity_id="old", payload={"paused_task_id": "old"})
    db.enqueue_turn("on-a", "manager", body="A work", turn_source="human", flow_run_id=case_a)
    db.enqueue_turn("on-b", "manager", body="B work", turn_source="human", flow_run_id=case_b)
    db.enqueue_turn("worker-on-a", "worker", body="worker work", turn_source="human",
                    flow_run_id=case_a)
    heads = {r["id"] for r in db.select_eligible_turn_heads()}
    assert "on-a" not in heads
    assert "worker-on-a" in heads
    db.withdraw_turn("on-a", expected_revision=1)
    assert "on-b" in {r["id"] for r in db.select_eligible_turn_heads()}


def test_managed_recovery_mark_drains_with_case_continuation_flag_off(tmp_path, monkeypatch):
    from src.control import db as db_module

    db = MeshDB(str(tmp_path / "mesh.db"))
    _session(db, "manager")
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    monkeypatch.setattr(db_module, "case_continuation_enabled", lambda: False)
    monkeypatch.setattr(db_module, "cache_heartbeat_active_enabled", lambda: False)

    class _Tick:
        def __init__(self):
            self.calls = 0

        async def _reconcile_continuation_finalizers(self, _db):
            return 0

        async def _reconcile_managed_recovery(self, _db):
            self.calls += 1
            return 0

    tick = _Tick()
    assert asyncio.run(TaskOrchestrator._wake_dispatcher_tick_once(tick)) == 0
    assert tick.calls == 1


def _wedge_setup(tmp_path, monkeypatch, *, record_quota: bool):
    """A linked continuation wake W fails ``usage_limit`` while the Case
    continuation flag is OFF; a human B is queued behind its pause mark."""
    from src.control import db as db_module
    from src.control.db import CONTINUATION_MACHINE_SENTINEL

    db = MeshDB(str(tmp_path / "mesh.db"))
    _session(db, "manager")
    db.upsert_node(node_id="worker-a", tailscale_ip="100.64.0.10", api_port=9001,
                   backends=["claude"], max_concurrent=2, incarnation_id="inc-a")
    case_id = db.open_case("objective", "manager", role="manager")
    token = f"cont:{case_id}:1"
    db.enqueue_task(token, None, CONTINUATION_MACHINE_SENTINEL, "claude",
                    "manager_continuation", {"case_id": case_id, "generation": 1})
    db.enqueue_turn("wake-w", "manager", body="wake", turn_source="system",
                    turn_kind="continuation",
                    idempotency_scope="automation:manager:continuation",
                    flow_run_id=case_id, producer_token=token,
                    producer_meta={"case_id": case_id}, machine_id="worker-a")
    assert db.activate_turn("wake-w")
    claim = db.claim_turn("wake-w", node_id="worker-a", carrier_kind="gateway_local",
                          incarnation_id="inc-a")
    db.start_turn("wake-w", claim_token=str(claim), incarnation_id="inc-a")
    db.complete_turn("wake-w", claim_token=str(claim), result={"output": ""},
                     status="failed", error="429 usage limit", error_class="usage_limit")
    assert db.get_task("wake-w")["retry_pause_state"] == "pending"
    db.enqueue_turn("human-b", "manager", body="B", turn_source="human", flow_run_id=case_id)

    monkeypatch.setattr(db_module, "get_db", lambda: db)
    monkeypatch.setattr(db_module, "case_continuation_enabled", lambda: False)
    monkeypatch.setattr(db_module, "cache_heartbeat_active_enabled", lambda: False)
    monkeypatch.setattr(db_module, "case_quota_resume_enabled", lambda: record_quota)
    monkeypatch.setattr(db_module, "transient_provider_resume_enabled", lambda: False)
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.events = []
    orch._emit_event = lambda name, task, data=None: orch.events.append(name)
    orch._harness_flow_drive_enabled = lambda: True
    orch.quota_window_state = lambda provider="claude": {
        "exhausted": True, "reset_at": None, "provider": "claude", "evidence": "limit_reached"}
    return db, orch, case_id, token


def test_failed_continuation_wake_does_not_wedge_manager_with_flag_off(tmp_path, monkeypatch):
    """[4e review F1] The real tick on a real MeshDB: the linked continuation
    token is finalized with the Case continuation flag OFF, so the pause mark
    drains (``done``) and human B is head-selected — no permanent wedge."""
    db, orch, _case, token = _wedge_setup(tmp_path, monkeypatch, record_quota=False)
    for _ in range(2):
        asyncio.run(TaskOrchestrator._wake_dispatcher_tick_once(orch))
    assert db.get_task(token)["status"] == "completed"
    assert db.get_task("wake-w")["retry_pause_state"] == "done"
    assert [r["id"] for r in db.select_eligible_turn_heads()] == ["human-b"]


def test_failed_continuation_wake_records_pause_with_operator_exit_flag_off(tmp_path, monkeypatch):
    """[4e review F1] With quota-pause recording ON the drained mark becomes a
    recorded Case pause (B held by the pause, not by the mark); the existing
    operator decline releases B."""
    db, orch, case_id, token = _wedge_setup(tmp_path, monkeypatch, record_quota=True)
    asyncio.run(TaskOrchestrator._wake_dispatcher_tick_once(orch))
    assert db.get_task(token)["status"] == "completed"
    assert db.get_task("wake-w")["retry_pause_state"] == "done"
    assert db.case_quota_pause(case_id) is not None
    assert db.select_eligible_turn_heads() == []
    db.append_flow_event(case_id, "flow.quota_pause_declined", "operator")
    assert [r["id"] for r in db.select_eligible_turn_heads()] == ["human-b"]


def test_queued_old_manager_work_stays_held_after_case_rebind(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    _session(db, "old-manager")
    _session(db, "new-manager")
    case_id = db.open_case("objective", "old-manager", role="manager")
    db.enqueue_turn("human-b", "old-manager", body="B", turn_source="human",
                    flow_run_id=case_id)
    assert [r["id"] for r in db.select_eligible_turn_heads()] == ["human-b"]
    db.create_flow_link(case_id, "session", "new-manager", "manager")
    db.append_flow_event(case_id, "case.manager_respawned", "system",
                         entity_type="session", entity_id="new-manager")
    assert db.select_eligible_turn_heads() == []
    assert db.activate_prepared_turn(
        "human-b", expected_revision=1,
        expected_config_revision=int(db.get_session("old-manager")["config_revision"]),
        action="resume_session", payload={"prompt": "B"}, machine_id="worker-a",
    ) == "ineligible"
    assert db.get_task("human-b")["status"] == "queued"


@pytest.mark.parametrize("rebind_after_claim", [False, True])
def test_pending_or_claimed_old_manager_turn_cannot_start_after_rebind(
    tmp_path, rebind_after_claim,
):
    db = MeshDB(str(tmp_path / "mesh.db"))
    _session(db, "old-manager")
    _session(db, "new-manager")
    case_id = db.open_case("objective", "old-manager", role="manager")
    db.upsert_node("worker-a", "100.64.0.10", 9001, ["claude"], 2,
                   incarnation_id="inc-a")
    db.enqueue_turn("old-b", "old-manager", body="B", turn_source="human",
                    flow_run_id=case_id, machine_id="worker-a")
    db.activate_turn("old-b")
    token = None
    if rebind_after_claim:
        token = db.claim_turn("old-b", "worker-a", "gateway_local", "inc-a")
    db.create_flow_link(case_id, "session", "new-manager", "manager")
    with pytest.raises(OwnershipConflictError):
        if rebind_after_claim:
            db.start_turn("old-b", token, "inc-a")
        else:
            db.claim_turn("old-b", "worker-a", "gateway_local", "inc-a")
    assert db.get_task("old-b")["status"] == ("claimed" if rebind_after_claim else "queued")


def test_rebound_pending_turn_is_deactivated_with_an_operator_exit(tmp_path):
    """An activated (pending) turn of the former Manager must not wedge the old
    session's slot behind an unclaimable row: the refused claim returns it to
    the queue, held by the binding gate, visible with a reason, withdrawable."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    _session(db, "old-manager")
    _session(db, "new-manager")
    case_id = db.open_case("objective", "old-manager", role="manager")
    db.upsert_node("worker-a", "100.64.0.10", 9001, ["claude"], 2, incarnation_id="inc-a")
    db.enqueue_turn("old-b", "old-manager", body="B", turn_source="human",
                    flow_run_id=case_id, machine_id="worker-a")
    db.activate_turn("old-b")
    db.create_flow_link(case_id, "session", "new-manager", "manager")
    for _ in range(2):  # every carrier retry is refused the same way
        with pytest.raises(OwnershipConflictError):
            db.claim_turn("old-b", "worker-a", "gateway_local", "inc-a")
    row = db.get_task("old-b")
    assert row["status"] == "queued" and row["blocked_reason"] == "case_manager_rebound"
    assert row["prompt"] == "B"
    assert db.get_active_turn("old-manager") is None
    assert db.select_eligible_turn_heads() == []
    db.withdraw_turn("old-b", expected_revision=int(row["revision"]))
    assert db.get_task("old-b")["status"] == "withdrawn"


@pytest.mark.parametrize("intervening", ["rebind", "stop"])
def test_respawn_link_refuses_stale_manager_binding(tmp_path, intervening):
    db = MeshDB(str(tmp_path / "mesh.db"))
    for sid in ("dead", "new", "other"):
        _session(db, sid)
    case_id = db.open_case("objective", "dead", role="manager")
    token_id = f"respawn:{case_id}:1"
    db.enqueue_task(token_id, None, None, "claude", RESPAWN_ACTION,
                    {"case_id": case_id, "generation": 1, "dead_session_id": "dead"})
    if intervening == "rebind":
        db.create_flow_link(case_id, "session", "other", "manager")
    else:
        with db._write() as conn:
            conn.execute("UPDATE sessions SET turn_queue_hold='operator_stop' WHERE session_id='dead'")

    with pytest.raises(OwnershipConflictError):
        db.record_respawn_link(token_id, case_id=case_id, new_session_id="new",
                               dead_session_id="dead", generation=1)
    assert db.case_manager_session_id(case_id) == ("other" if intervening == "rebind" else "dead")
    assert db.get_session("new")["current_case_id"] is None
