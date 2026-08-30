import asyncio
from datetime import datetime, timedelta, timezone

from src.control import db as db_mod
from src.control.db import (
    CACHE_HEARTBEAT_ACTION,
    CACHE_HEARTBEAT_MACHINE_SENTINEL,
    MeshDB,
)
from src.core.interfaces import Session, SessionStatus
from src.orchestrator import TaskOrchestrator


def _iso(offset_sec: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_sec)).isoformat()


def _session(status: SessionStatus = SessionStatus.AWAITING_INPUT) -> Session:
    return Session(
        session_id="sess_hb",
        backend="claude",
        repo_path="/tmp",
        status=status,
        created_at=_iso(),
        updated_at=_iso(),
        backend_session_id="claude-native",
        driver_type="sdk",
        driver_status="live",
    )


def _cache_evidence(
    db: MeshDB,
    session_id: str = "sess_hb",
    cache_read: int = 120_000,
    *,
    task_id: str = "task_real",
    turn_id: str = "turn_hb",
    offset_sec: int = 0,
) -> str:
    now = _iso(offset_sec)
    conn = db._conn()
    conn.execute(
        """
        INSERT INTO llm_turns (
            turn_id, session_id, task_id, backend, started_at, ended_at,
            final_status, created_at, updated_at
        ) VALUES (?, ?, ?, 'claude', ?, ?, 'completed', ?, ?)
        """,
        (turn_id, session_id, task_id, now, now, now, now),
    )
    conn.execute(
        """
        INSERT INTO llm_invocations (
            invocation_id, turn_id, attempt, spawn_reason, action, node_id,
            backend, started_at, ended_at, status
        ) VALUES (?, ?, 1, 'initial', 'resume_session', 'node',
                  'claude', ?, ?, 'completed')
        """,
        (f"inv_{turn_id}", turn_id, now, now),
    )
    conn.execute(
        """
        INSERT INTO llm_model_requests (
            model_request_id, invocation_id, turn_id, sequence, work_category,
            started_at, ended_at, status, input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens, input_token_semantics,
            usage_granularity, usage_coverage, is_duplicate
        ) VALUES (?, ?, ?, 1, 'agent', ?, ?, 'ok',
                  10, 1, ?, 0, 'exclusive_cache', 'invocation_total',
                  'aggregate_only', 0)
        """,
        (f"req_{turn_id}", f"inv_{turn_id}", turn_id, now, now, cache_read),
    )
    return now


def test_observe_default_creates_one_controller_for_overlapping_owners(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CACHE_HEARTBEAT_ACTIVE", raising=False)
    monkeypatch.delenv("CACHE_HEARTBEAT_OBSERVE", raising=False)
    db = MeshDB(str(tmp_path / "mesh.db"))
    _cache_evidence(db)

    first = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="manual", owner_type="operator", owner_id="one",
    )
    second = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="watched_job", owner_type="job", owner_id="job_1",
    )

    assert first is not None
    assert second is not None
    assert first["id"] == second["id"]
    heartbeats = db.list_cache_heartbeats(session_id="sess_hb")
    assert len(heartbeats) == 1
    assert heartbeats[0]["status"] == "observe_only"
    assert len([o for o in heartbeats[0]["owners"] if o["status"] == "active"]) == 2


def test_watched_job_registers_owner_when_notify_agent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "0")
    db = MeshDB(str(tmp_path / "mesh.db"))
    _cache_evidence(db)
    db.register_job(
        job_id="job_1",
        node_id="node",
        label="long",
        session_id="sess_hb",
        notify_agent=True,
    )
    db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="watched_job", owner_type="job", owner_id="job_1",
    )
    hb = db.list_cache_heartbeats(session_id="sess_hb")[0]
    assert hb["owners"][0]["reason"] == "watched_job"


def test_auto_policy_skips_arming_for_jobs_shorter_than_interval(tmp_path, monkeypatch) -> None:
    from src.control import task_server

    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "0")
    db = MeshDB(str(tmp_path / "mesh.db"))
    _cache_evidence(db)
    monkeypatch.setattr(task_server, "get_db", lambda: db)

    task_server.register_job(task_server.RegisterJobPayload(
        node_id="node", label="quick", session_id="sess_hb",
        notify_agent=True, cache_heartbeat="auto", expected_runtime_sec=10,
    ))
    assert db.list_cache_heartbeats(session_id="sess_hb") == []

    task_server.register_job(task_server.RegisterJobPayload(
        node_id="node", label="long", session_id="sess_hb",
        notify_agent=True, cache_heartbeat="auto", expected_runtime_sec=5000,
    ))
    assert len(db.list_cache_heartbeats(session_id="sess_hb")) == 1

    task_server.register_job(task_server.RegisterJobPayload(
        node_id="node", label="quick-explicit", session_id="sess_hb",
        notify_agent=True, cache_heartbeat="on", expected_runtime_sec=10,
    ))
    hb = db.list_cache_heartbeats(session_id="sess_hb")[0]
    assert len(hb["owners"]) == 2


class _Store:
    def __init__(self, session: Session):
        self.session = session

    def get(self, session_id: str):
        return self.session if session_id == self.session.session_id else None


class _Orch:
    running = True
    active_tasks = {}
    task_results = {}
    _CONTINUATION_TERMINAL_STATUSES = TaskOrchestrator._CONTINUATION_TERMINAL_STATUSES
    _sync_cache_heartbeat_state = TaskOrchestrator._sync_cache_heartbeat_state
    _cache_heartbeat_owner_live = TaskOrchestrator._cache_heartbeat_owner_live
    _cache_heartbeat_session_eligible = TaskOrchestrator._cache_heartbeat_session_eligible
    _finalize_cache_heartbeat = TaskOrchestrator._finalize_cache_heartbeat

    def __init__(self, session: Session):
        self.session_store = _Store(session)
        self.submitted = []

    async def submit_instruction(self, description, **kwargs):
        self.submitted.append((description, kwargs))
        return "task_hb_turn"

    def _emit_event(self, *args, **kwargs):
        return None


def test_due_heartbeat_claims_one_lease_and_submits_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    monkeypatch.setenv("CACHE_HEARTBEAT_MIN_CACHE_TOKENS", "100")
    db = MeshDB(str(tmp_path / "mesh.db"))
    _cache_evidence(db, cache_read=1000)
    session = _session()
    db.upsert_session(session)
    hb = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="manual", owner_type="operator", owner_id="manual",
    )
    assert hb is not None
    with db._write() as conn:
        conn.execute(
            "UPDATE session_cache_heartbeats SET next_due_at = ? WHERE id = ?",
            (_iso(-10), hb["id"]),
        )
    orch = _Orch(session)

    delivered = asyncio.run(TaskOrchestrator._process_due_cache_heartbeats(orch, db))

    assert delivered == 1
    assert len(orch.submitted) == 1
    rows = db._conn().execute(
        "SELECT action, machine_id, status FROM mesh_tasks WHERE action = ?",
        (CACHE_HEARTBEAT_ACTION,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["machine_id"] == CACHE_HEARTBEAT_MACHINE_SENTINEL
    assert rows[0]["status"] == "claimed"


def test_busy_session_is_skipped_not_interrupted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    monkeypatch.setenv("CACHE_HEARTBEAT_MIN_CACHE_TOKENS", "100")
    db = MeshDB(str(tmp_path / "mesh.db"))
    _cache_evidence(db, cache_read=1000)
    session = _session(status=SessionStatus.BUSY)
    db.upsert_session(session)
    hb = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="manual", owner_type="operator", owner_id="manual",
    )
    assert hb is not None
    with db._write() as conn:
        conn.execute(
            "UPDATE session_cache_heartbeats SET next_due_at = ? WHERE id = ?",
            (_iso(-10), hb["id"]),
        )
    orch = _Orch(session)

    delivered = asyncio.run(TaskOrchestrator._process_due_cache_heartbeats(orch, db))

    assert delivered == 0
    assert orch.submitted == []


def test_due_heartbeat_uses_real_turn_cache_touch_before_delivering(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    monkeypatch.setenv("CACHE_HEARTBEAT_MIN_CACHE_TOKENS", "100")
    db = MeshDB(str(tmp_path / "mesh.db"))
    old_touch = _cache_evidence(db, cache_read=1000, turn_id="turn_old", offset_sec=-3600)
    session = _session()
    db.upsert_session(session)
    hb = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="manual", owner_type="operator", owner_id="manual",
    )
    assert hb is not None
    assert hb["last_cache_touch_at"] == old_touch
    fresh_touch = _cache_evidence(
        db, cache_read=1000, task_id="task_fresh", turn_id="turn_fresh", offset_sec=-10,
    )
    with db._write() as conn:
        conn.execute(
            "UPDATE session_cache_heartbeats SET next_due_at = ? WHERE id = ?",
            (_iso(-10), hb["id"]),
        )
    orch = _Orch(session)

    delivered = asyncio.run(TaskOrchestrator._process_due_cache_heartbeats(orch, db))

    row = db.get_cache_heartbeat(str(hb["id"]))
    assert delivered == 0
    assert orch.submitted == []
    assert row["last_cache_touch_at"] == fresh_touch
    assert datetime.fromisoformat(row["next_due_at"]) > datetime.now(timezone.utc)


def test_rearming_owner_refreshes_stale_cache_touch_from_real_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    db = MeshDB(str(tmp_path / "mesh.db"))
    old_touch = _cache_evidence(db, turn_id="turn_old", offset_sec=-3600)
    first = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="manual", owner_type="operator", owner_id="manual",
    )
    assert first is not None
    assert first["last_cache_touch_at"] == old_touch
    fresh_touch = _cache_evidence(
        db, task_id="task_fresh", turn_id="turn_fresh", offset_sec=-30,
    )

    second = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="watched_job", owner_type="job", owner_id="job_1",
    )

    assert second is not None
    assert second["id"] == first["id"]
    assert second["last_cache_touch_at"] == fresh_touch
    assert datetime.fromisoformat(second["next_due_at"]) > datetime.now(timezone.utc)


def test_controller_creation_uses_latest_cache_touch_not_largest_recent_hit(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    db = MeshDB(str(tmp_path / "mesh.db"))
    _cache_evidence(db, cache_read=5000, turn_id="turn_old", offset_sec=-3600)
    fresh_touch = _cache_evidence(
        db, cache_read=1000, task_id="task_fresh", turn_id="turn_fresh", offset_sec=-20,
    )

    hb = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="manual", owner_type="operator", owner_id="manual",
    )

    assert hb is not None
    assert hb["last_cache_touch_at"] == fresh_touch
    assert hb["last_cache_read_tokens"] == 1000


def test_bulk_refresh_prioritizes_due_stale_controllers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    db = MeshDB(str(tmp_path / "mesh.db"))
    due_old_touch = _cache_evidence(
        db, session_id="sess_due", turn_id="turn_due_old", offset_sec=-3600,
    )
    due_hb = db.ensure_cache_heartbeat_owner(
        "sess_due", reason="manual", owner_type="operator", owner_id="manual_due",
    )
    assert due_hb is not None
    assert due_hb["last_cache_touch_at"] == due_old_touch
    due_fresh_touch = _cache_evidence(
        db,
        session_id="sess_due",
        task_id="task_due_fresh",
        turn_id="turn_due_fresh",
        offset_sec=-10,
    )
    _cache_evidence(db, session_id="sess_not_due", turn_id="turn_not_due", offset_sec=-20)
    not_due_hb = db.ensure_cache_heartbeat_owner(
        "sess_not_due", reason="manual", owner_type="operator", owner_id="manual_not_due",
    )
    assert not_due_hb is not None
    with db._write() as conn:
        conn.execute(
            "UPDATE session_cache_heartbeats SET next_due_at = ?, updated_at = ? WHERE id = ?",
            (_iso(-5), _iso(-3600), due_hb["id"]),
        )

    changed = db.refresh_cache_heartbeats_from_recent_evidence(limit=1)

    due_row = db.get_cache_heartbeat(str(due_hb["id"]))
    not_due_row = db.get_cache_heartbeat(str(not_due_hb["id"]))
    assert changed == 1
    assert due_row["last_cache_touch_at"] == due_fresh_touch
    assert not_due_row["last_cache_touch_at"] == not_due_hb["last_cache_touch_at"]


def test_eligibility_rechecks_refreshed_due_time_from_real_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    monkeypatch.setenv("CACHE_HEARTBEAT_MIN_CACHE_TOKENS", "100")
    db = MeshDB(str(tmp_path / "mesh.db"))
    _cache_evidence(db, cache_read=1000, turn_id="turn_old", offset_sec=-3600)
    session = _session()
    db.upsert_session(session)
    hb = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="manual", owner_type="operator", owner_id="manual",
    )
    assert hb is not None
    stale_hb = dict(hb)
    with db._write() as conn:
        conn.execute(
            "UPDATE session_cache_heartbeats SET next_due_at = ? WHERE id = ?",
            (_iso(-10), hb["id"]),
        )
    _cache_evidence(db, cache_read=1000, task_id="task_fresh", turn_id="turn_fresh", offset_sec=-5)
    orch = _Orch(session)

    ok, reason, eligible_session = TaskOrchestrator._cache_heartbeat_session_eligible(orch, db, stale_hb)

    assert ok is False
    assert reason == "cache_fresh"
    assert eligible_session is session


def test_finalize_timeout_does_not_stop_the_controller(tmp_path, monkeypatch) -> None:
    import src.orchestrator as orch_mod

    monkeypatch.setenv("CACHE_HEARTBEAT_ACTIVE", "1")
    db = MeshDB(str(tmp_path / "mesh.db"))
    session = _session()
    db.upsert_session(session)
    hb = db.ensure_cache_heartbeat_owner(
        "sess_hb", reason="manual", owner_type="operator", owner_id="manual",
    )
    heartbeat_id = str(hb["id"])
    lease_id = "cache_heartbeat_lease_test"
    db.enqueue_task(
        lease_id, session_id=None, machine_id=CACHE_HEARTBEAT_MACHINE_SENTINEL,
        backend="claude", action=CACHE_HEARTBEAT_ACTION, payload={},
    )
    db.claim_task(lease_id, "host")
    orch = _Orch(session)

    # _finalize_cache_heartbeat resolves get_db() itself rather than taking a db
    # argument — point it at our tmp_path db, not the process-global instance.
    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    # The wake turn never reaches a terminal status inside the 180s poll window —
    # this must NOT be treated as a heartbeat failure.
    times = iter([0.0, 500.0])
    monkeypatch.setattr(orch_mod.time, "time", lambda: next(times, 500.0))

    asyncio.run(TaskOrchestrator._finalize_cache_heartbeat(
        orch, heartbeat_id, lease_id, "task_never_lands", "sess_hb",
    ))

    row = db.get_cache_heartbeat(heartbeat_id)
    assert row["status"] in ("active", "observe_only")
    assert row["beat_count"] == 0
    task_row = db.get_task(lease_id)
    assert task_row["status"] == "completed"
