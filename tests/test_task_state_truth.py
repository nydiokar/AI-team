import json
from datetime import datetime, timedelta

from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from src.core.task_state_truth import (
    derive_job_execution_state,
    derive_task_execution_state,
)


NOW = datetime(2026, 7, 1, 12, 0, 0)


def _db(tmp_path):
    return MeshDB(str(tmp_path / "mesh.db"))


def _enqueue(db: MeshDB, task_id: str = "task_truth", session_id: str = "sess_truth") -> None:
    db.upsert_session(
        Session(
            session_id=session_id,
            backend="codex",
            repo_path="/tmp/repo",
            status=SessionStatus.BUSY,
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
            machine_id="worker-a",
            last_task_id=task_id,
        )
    )
    db.enqueue_task(
        task_id=task_id,
        session_id=session_id,
        machine_id="worker-a",
        backend="codex",
        action="resume_session",
        payload={"task_id": task_id, "prompt": "state truth"},
    )


def _node(db: MeshDB, incarnation_id: str = "inc-a") -> dict[str, object]:
    db.upsert_node(
        node_id="worker-a",
        tailscale_ip="100.64.0.10",
        api_port=9001,
        backends=["codex"],
        max_concurrent=2,
        incarnation_id=incarnation_id,
    )
    return db.get_node("worker-a")


def _age_node(db: MeshDB, *, seconds: int) -> None:
    old = (NOW - timedelta(seconds=seconds)).isoformat()
    with db._write() as conn:
        conn.execute(
            """
            UPDATE nodes
            SET last_heartbeat = ?, live_state_updated_at = ?, updated_at = ?
            WHERE node_id = 'worker-a'
            """,
            (old, old, old),
        )


def test_pending_task_derives_queued(tmp_path) -> None:
    db = _db(tmp_path)
    _enqueue(db, task_id="task_pending")

    derived = derive_task_execution_state(db.get_task("task_pending"), now=NOW)

    assert derived.state == "queued"
    assert derived.confidence == "high"
    assert derived.authoritative_source == "mesh_task_status"


def test_claimed_task_with_fresh_worker_live_state_derives_worker_running(tmp_path) -> None:
    db = _db(tmp_path)
    _node(db)
    _enqueue(db, task_id="task_fresh")
    assert db.claim_task("task_fresh", "worker-a")
    db.heartbeat_node(
        "worker-a",
        live_state=json.dumps(
            {
                "v": 1,
                "active_tasks": ["task_fresh"],
                "active_task_details": {
                    "task_fresh": {
                        "backend": "codex",
                        "phase": "running",
                        "started_at": NOW.isoformat(),
                    }
                },
                "slots_used": 1,
                "slots_total": 2,
                "incarnation_id": "inc-a",
            }
        ),
    )

    derived = derive_task_execution_state(
        db.get_task("task_fresh"),
        node_row=db.get_node("worker-a"),
        now=NOW,
    )

    assert derived.state == "worker_running"
    assert derived.confidence == "high"
    assert derived.authoritative_source == "worker_live_state"


def test_claimed_task_with_stale_worker_heartbeat_is_unknown_not_running(tmp_path) -> None:
    db = _db(tmp_path)
    _node(db)
    _enqueue(db, task_id="task_stale_heartbeat")
    assert db.claim_task("task_stale_heartbeat", "worker-a")
    db.heartbeat_node(
        "worker-a",
        live_state=json.dumps({"v": 1, "active_tasks": ["task_stale_heartbeat"]}),
    )
    _age_node(db, seconds=600)

    derived = derive_task_execution_state(
        db.get_task("task_stale_heartbeat"),
        node_row=db.get_node("worker-a"),
        now=NOW,
        live_state_max_age_sec=90,
    )

    assert derived.state == "worker_unknown"
    assert derived.authoritative_source == "stale_claim_evidence"
    assert "stale" in derived.reason


def test_claimed_task_after_node_incarnation_changed_is_stale_claim(tmp_path) -> None:
    db = _db(tmp_path)
    _node(db, incarnation_id="inc-a")
    _enqueue(db, task_id="task_old_incarnation")
    assert db.claim_task("task_old_incarnation", "worker-a")
    _node(db, incarnation_id="inc-b")

    derived = derive_task_execution_state(
        db.get_task("task_old_incarnation"),
        node_row=db.get_node("worker-a"),
        now=NOW,
    )

    assert derived.state == "stale_claim"
    assert derived.confidence == "high"
    assert "incarnation" in derived.reason


def test_claimed_task_with_fresh_claim_but_no_live_state_derives_claimed(tmp_path) -> None:
    db = _db(tmp_path)
    _node(db, incarnation_id="inc-a")
    _enqueue(db, task_id="task_claimed")
    assert db.claim_task("task_claimed", "worker-a")

    derived = derive_task_execution_state(
        db.get_task("task_claimed"),
        node_row=db.get_node("worker-a"),
        now=NOW,
    )

    assert derived.state == "claimed"
    assert derived.confidence == "medium"
    assert derived.authoritative_source == "mesh_task_claim"


def test_terminal_task_result_wins_over_stale_worker_state(tmp_path) -> None:
    db = _db(tmp_path)
    _node(db)
    _enqueue(db, task_id="task_done")
    assert db.claim_task("task_done", "worker-a")
    db.heartbeat_node("worker-a", live_state=json.dumps({"v": 1, "active_tasks": ["task_done"]}))
    _age_node(db, seconds=600)
    db.complete_task("task_done", {"success": True, "output": "done"})

    derived = derive_task_execution_state(
        db.get_task("task_done"),
        node_row=db.get_node("worker-a"),
        now=NOW,
    )

    assert derived.state == "completed"
    assert derived.confidence == "high"
    assert derived.authoritative_source == "mesh_task_terminal"


def test_terminal_failed_task_derives_failed(tmp_path) -> None:
    db = _db(tmp_path)
    _enqueue(db, task_id="task_failed")
    db.fail_task("task_failed", "backend exited")

    derived = derive_task_execution_state(db.get_task("task_failed"), now=NOW)

    assert derived.state == "failed"
    assert derived.confidence == "high"
    assert derived.authoritative_source == "mesh_task_terminal"


def test_fresh_live_state_without_claimed_task_derives_detached(tmp_path) -> None:
    db = _db(tmp_path)
    _node(db)
    _enqueue(db, task_id="task_detached")
    assert db.claim_task("task_detached", "worker-a")
    db.heartbeat_node(
        "worker-a",
        live_state=json.dumps({"v": 1, "active_tasks": ["other_task"]}),
    )

    derived = derive_task_execution_state(
        db.get_task("task_detached"),
        node_row=db.get_node("worker-a"),
        now=NOW,
    )

    assert derived.state == "detached"
    assert derived.confidence == "high"
    assert derived.authoritative_source == "stale_claim_evidence"


def test_state_derivation_distinguishes_non_terminal_authority_states() -> None:
    base = {
        "id": "task_vocab",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }

    cases = [
        (
            "accepted",
            {"status": "accepted"},
            {},
            "accepted",
        ),
        (
            "waiting_for_input",
            {"status": "pending"},
            {"session_row": {"session_id": "sess_vocab", "status": "awaiting_input", "updated_at": NOW.isoformat()}},
            "waiting_for_input",
        ),
        (
            "waiting_for_approval",
            {"status": "pending"},
            {"approval_pending": True},
            "waiting_for_approval",
        ),
        (
            "cancel_requested",
            {"status": "pending"},
            {"cancel_requested": True},
            "cancel_requested",
        ),
        (
            "cancelled",
            {"status": "cancelled"},
            {},
            "cancelled",
        ),
        (
            "backend_running",
            {"status": "processing"},
            {"telemetry_turn": {"turn_id": "task_vocab", "final_status": "running", "updated_at": NOW.isoformat()}},
            "backend_running",
        ),
        (
            "recovered",
            {"status": "completed", "completed_at": NOW.isoformat()},
            {"recovery_evidence": True},
            "recovered",
        ),
    ]

    for label, task_patch, kwargs, expected in cases:
        derived = derive_task_execution_state(
            {**base, **task_patch},
            now=NOW,
            **kwargs,
        )
        assert derived.state == expected, label


def test_claimed_task_with_lost_driver_session_derives_driver_lost() -> None:
    task_row = {
        "id": "task_driver_lost",
        "status": "claimed",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "claimed_at": NOW.isoformat(),
    }
    session_row = {
        "session_id": "sess_driver_lost",
        "status": "awaiting_input",
        "driver_status": "lost",
        "updated_at": NOW.isoformat(),
    }

    derived = derive_task_execution_state(task_row, session_row=session_row, now=NOW)

    assert derived.state == "driver_lost"
    assert derived.confidence == "high"
    assert derived.authoritative_source == "mesh_task_status"


def test_completed_task_with_lost_driver_session_still_derives_completed() -> None:
    task_row = {
        "id": "task_completed_lost_driver",
        "status": "completed",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "completed_at": NOW.isoformat(),
    }
    session_row = {
        "session_id": "sess_completed_lost_driver",
        "status": "awaiting_input",
        "driver_status": "lost",
        "updated_at": NOW.isoformat(),
    }

    derived = derive_task_execution_state(task_row, session_row=session_row, now=NOW)

    assert derived.state == "completed"
    assert derived.authoritative_source == "mesh_task_terminal"


def test_watched_job_states_preserve_session_ownership_when_present(tmp_path) -> None:
    db = _db(tmp_path)
    cases = [
        ("job_running_session", "running", "sess_jobs"),
        ("job_done_session", "done", "sess_jobs"),
        ("job_failed_session", "failed", "sess_jobs"),
        ("job_lost_session", "lost", "sess_jobs"),
        ("job_running_unowned", "running", None),
        ("job_done_unowned", "done", None),
        ("job_failed_unowned", "failed", None),
        ("job_lost_unowned", "lost", None),
    ]
    for job_id, status, session_id in cases:
        db.register_job(job_id=job_id, node_id="worker-a", label=job_id, session_id=session_id)
        if status == "done":
            db.complete_job(job_id, exit_code=0, tail="ok")
        elif status == "failed":
            db.fail_job(job_id, "exit code 1")
        elif status == "lost":
            db.fail_job(job_id, "pid identity changed", status="lost")

    for job_id, status, session_id in cases:
        derived = derive_job_execution_state(db.get_job(job_id))
        assert derived.state == status
        assert derived.confidence == "high"
        if session_id:
            assert derived.raw_refs["session_id"] == session_id
        else:
            assert "session_id" not in derived.raw_refs
