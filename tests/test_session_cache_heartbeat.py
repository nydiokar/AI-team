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


def _cache_evidence(db: MeshDB, session_id: str = "sess_hb", cache_read: int = 120_000) -> None:
    now = _iso()
    conn = db._conn()
    conn.execute(
        """
        INSERT INTO llm_turns (
            turn_id, session_id, task_id, backend, started_at, ended_at,
            final_status, created_at, updated_at
        ) VALUES ('turn_hb', ?, 'task_real', 'claude', ?, ?, 'completed', ?, ?)
        """,
        (session_id, now, now, now, now),
    )
    conn.execute(
        """
        INSERT INTO llm_invocations (
            invocation_id, turn_id, attempt, spawn_reason, action, node_id,
            backend, started_at, ended_at, status
        ) VALUES ('inv_hb', 'turn_hb', 1, 'initial', 'resume_session', 'node',
                  'claude', ?, ?, 'completed')
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO llm_model_requests (
            model_request_id, invocation_id, turn_id, sequence, work_category,
            started_at, ended_at, status, input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens, input_token_semantics,
            usage_granularity, usage_coverage, is_duplicate
        ) VALUES ('req_hb', 'inv_hb', 'turn_hb', 1, 'agent', ?, ?, 'ok',
                  10, 1, ?, 0, 'exclusive_cache', 'invocation_total',
                  'aggregate_only', 0)
        """,
        (now, now, cache_read),
    )


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
