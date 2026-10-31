"""J3: real SQLite statement budgets and reason equivalence; no backend calls."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.control import db as db_module
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from src.core.session_reason import SessionReason, build_reason_batch, derive_session_reasons
from src.services.session_service import SessionService
from src.services.session_store import SessionStore


@pytest.fixture
def db(tmp_path) -> Iterator[MeshDB]:
    store = MeshDB(str(tmp_path / "reason.db"))
    yield store
    store.close()


def _page(db: MeshDB, count: int) -> list[Session]:
    sessions: list[Session] = [
        Session(session_id=f"s{i}", backend="claude", repo_path="/tmp",
                status=SessionStatus.AWAITING_INPUT, created_at="2026-10-29",
                updated_at="2026-10-29", current_case_id=f"c{i}")
        for i in range(count)
    ]
    # Bulk fixture setup keeps the count test independent of write/fsync cost.
    with db._write() as conn:
        conn.executemany(
            "INSERT INTO flow_runs(flow_run_id,status) VALUES (?,NULL)",
            [(s.current_case_id,) for s in sessions],
        )
        conn.executemany(
            "INSERT INTO flow_events(flow_run_id,event_type,actor,created_at) "
            "VALUES (?,'flow.note','system','2026-10-29')",
            [(s.current_case_id,) for s in sessions],
        )
        conn.executemany(
            "INSERT INTO sessions(session_id,backend,repo_path,status,created_at,updated_at,"
            "current_case_id,turn_queue_enrolled) VALUES (?,'claude','/tmp','awaiting_input',"
            "'2026-10-29','2026-10-29',?,1)",
            [(s.session_id, s.current_case_id) for s in sessions],
        )
    db.refresh_enrollment_presence()
    return sessions


@pytest.mark.parametrize("count,expected", [(1, 5), (20, 5), (200, 5), (501, 10)])
def test_reason_data_selects_grow_only_by_500_id_chunks(db, count, expected) -> None:
    sessions = _page(db, count)
    statements: list[str] = []
    db._conn().set_trace_callback(statements.append)
    try:
        reasons = derive_session_reasons(db, sessions)
    finally:
        db._conn().set_trace_callback(None)
    assert len(reasons) == count
    assert set(reasons.values()) == {SessionReason("open_case_idle", "medium")}
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == expected
    assert not any("SELECT * FROM flow_events" in s for s in selects)


@pytest.mark.parametrize("count,expected", [(1, 7), (20, 7), (200, 7), (501, 13)])
def test_full_list_select_budget_includes_base_and_enrolled_overlay(db, monkeypatch, count, expected) -> None:
    _page(db, count)
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    service = SessionService(SessionStore())
    statements: list[str] = []
    db._conn().set_trace_callback(statements.append)
    try:
        views = service.list_views(limit=count, db=db)
    finally:
        db._conn().set_trace_callback(None)
    assert len(views) == count
    assert all(v.reason == SessionReason("open_case_idle", "medium") for v in views)
    assert all(v.turn_queue is not None and v.turn_queue.queued == 0 for v in views)
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == expected


def test_old_running_job_survives_more_than_200_newer_terminal_jobs(db) -> None:
    session = _page(db, 1)[0]
    db.register_job(job_id="old-running", session_id=session.session_id,
                    node_id="node", label="still running")
    with db._write() as conn:
        conn.execute("UPDATE jobs SET created_at='2000-01-01' WHERE id='old-running'")
        conn.executemany(
            "INSERT INTO jobs(id,session_id,node_id,label,status,started_at,created_at,updated_at) "
            "VALUES (?,?,'node','new completion','done','2026-10-29','2026-10-29','2026-10-29')",
            [(f"done-{i}", session.session_id) for i in range(201)],
        )
    assert all(j["status"] != "running" for j in db.list_jobs_for_sessions([session.session_id]))
    assert derive_session_reasons(db, [session])[session.session_id] == SessionReason("waiting_job", "high")


_EVENT_SEQUENCES: list[tuple[str, ...]] = [
    (), ("flow.quota_paused",), ("flow.transient_paused",),
    ("flow.quota_paused", "flow.transient_paused"),
    ("flow.quota_paused", "flow.quota_resumed", "flow.transient_paused"),
    ("flow.transient_paused", "flow.transient_resumed", "flow.quota_paused"),
    ("flow.quota_paused", "flow.quota_pause_declined"),
    ("flow.transient_paused", "flow.transient_pause_exhausted"),
    ("flow.quota_paused", "flow.transient_paused", "case.manager_respawned"),
    ("case.manager_respawned", "flow.quota_paused"),
    ("flow.quota_paused", "flow.transient_resumed"),
    ("flow.transient_paused", "flow.quota_resumed"),
    ("flow.quota_paused", "flow.note"),
]


@pytest.mark.parametrize("case_status", [None, "open", "blocked", "closed", "cancelled", " CLOSED "])
@pytest.mark.parametrize("events", _EVENT_SEQUENCES)
def test_batched_pause_evidence_and_all_primary_states_match_single_case_helpers(db, case_status, events) -> None:
    cid = db.open_case("objective", "owner")
    with db._write() as conn:
        conn.execute("UPDATE flow_runs SET status=? WHERE flow_run_id=?", (case_status, cid))
    for i, event in enumerate(events):
        db.append_flow_event(cid, event, "system", payload={"marker": i})
    # Reverse timestamps: id, not wall-clock order, determines the newest fact.
    with db._write() as conn:
        conn.execute("UPDATE flow_events SET created_at=printf('%020d', 100000-id) WHERE flow_run_id=?", (cid,))
    quota, retry = db.case_quota_pause(cid), db.transient_pause(cid)
    for pause in (quota, retry):
        if pause is not None:
            assert {"marker", "paused_at", "event_id"} <= pause.keys()
    sessions = [
        Session(session_id=f"{status.value}-{role}", backend="claude", repo_path="/tmp",
                status=status, created_at="2026-10-29", updated_at="2026-10-29",
                current_case_id=cid, case_role=role, machine_id="offline-node")
        for status in SessionStatus for role in ("manager", "worker")
    ]
    reasons = derive_session_reasons(db, sessions)
    open_case = str(case_status or "").strip().lower() not in ("closed", "cancelled")
    for s in sessions:
        if s.status in (SessionStatus.PINNED_NODE_OFFLINE, SessionStatus.PAUSED_PINNED_NODE_OFFLINE):
            expected = SessionReason("node_offline", "high", "offline-node")
        elif s.status not in (SessionStatus.AWAITING_INPUT, SessionStatus.IDLE):
            expected = None
        elif not open_case:
            expected = SessionReason("idle", "high")
        elif quota is not None:
            expected = SessionReason("paused_quota", "high")
        elif retry is not None:
            expected = SessionReason("paused_retry", "high")
        else:
            expected = SessionReason("open_case_idle", "medium")
        assert reasons[s.session_id] == expected


def test_missing_cases_shared_cases_and_duplicate_ids_keep_complete_evidence(db) -> None:
    sessions = _page(db, 1)
    shared = Session(**{**sessions[0].__dict__, "session_id": "shared"})
    missing = Session(**{**sessions[0].__dict__, "session_id": "missing", "current_case_id": "gone"})
    batch = build_reason_batch(db, [*sessions, shared, shared, missing])
    assert batch.open_cases == {"c0"}
    reasons = derive_session_reasons(db, [*sessions, shared, missing])
    assert reasons == {"s0": SessionReason("open_case_idle", "medium"),
                       "shared": SessionReason("open_case_idle", "medium"),
                       "missing": SessionReason("idle", "high")}


def test_deduplicated_queries_use_existing_indexes_without_history_materialization(db) -> None:
    _page(db, 1)
    statements: list[str] = []
    db._conn().set_trace_callback(statements.append)
    try:
        assert db.case_reason_evidence(["c0"] * 1001)["c0"].status is None
        assert db.running_job_sessions(["s0"] * 1001) == set()
        assert db.case_reason_evidence([]) == {}
        assert db.running_job_sessions([]) == set()
    finally:
        db._conn().set_trace_callback(None)
    assert len(statements) == 2
    case_plan = [str(row[3]) for row in db._conn().execute("EXPLAIN QUERY PLAN " + statements[0])]
    assert sum("SEARCH e USING INDEX idx_flow_events_flow" in detail for detail in case_plan) == 2
    assert not any("TEMP B-TREE" in detail for detail in case_plan)
    assert "payload_json" not in statements[0] and "objective_lock" not in statements[0]
    job_plan = [str(row[3]) for row in db._conn().execute("EXPLAIN QUERY PLAN " + statements[1])]
    assert any("idx_jobs_session" in detail for detail in job_plan)


@pytest.mark.parametrize("method", ["running_job_sessions", "inbox_waiting_for", "live_task_sessions", "case_reason_evidence"])
def test_failed_evidence_never_establishes_idle(db, monkeypatch, method) -> None:
    sessions = _page(db, 1)
    def unavailable(*args, **kwargs):
        raise RuntimeError("private database detail")
    monkeypatch.setattr(db, method, unavailable)
    assert derive_session_reasons(db, sessions)["s0"] == SessionReason("unavailable", "medium")


def test_failed_chunk_does_not_hide_successful_chunk(db, monkeypatch) -> None:
    sessions = _page(db, 501)
    original = db.inbox_waiting_for
    def read(ids):
        if "s0" in ids:
            raise RuntimeError("offline")
        return original(ids)
    monkeypatch.setattr(db, "inbox_waiting_for", read)
    reasons = derive_session_reasons(db, sessions)
    assert reasons["s0"] == SessionReason("unavailable", "medium")
    assert reasons["s500"] == SessionReason("open_case_idle", "medium")
