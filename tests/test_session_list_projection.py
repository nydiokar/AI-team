"""J4: compact DB list reads preserve views, lifecycle state and fallback rules."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from config import config
from src.control import db as db_module
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionOrigin, SessionStatus
from src.core.view_models import SessionView
from src.services import session_store as store_module
from src.services.session_service import SessionService, session_turn_queue_overlay
from src.services.session_store import SessionStore


@pytest.fixture
def store_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[SessionStore, MeshDB]]:
    db = MeshDB(str(tmp_path / "projection.db"))
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    monkeypatch.setattr(store_module, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store_module, "_BINDINGS_FILE", tmp_path / "bindings.json")
    yield SessionStore(), db
    db.close()


def _session(sid: str, status: SessionStatus = SessionStatus.IDLE, **kwargs: object) -> Session:
    return Session(
        session_id=sid, backend="claude", repo_path="/repo", status=status,
        created_at="2026-10-29T00:00:00Z", updated_at="2026-10-29T00:00:00Z",
        **kwargs,
    )


def test_list_sql_omits_history_and_never_constructs_full_sessions(store_db, monkeypatch) -> None:
    store, db = store_db
    db.upsert_session(_session("s", task_history=[{"large": "x" * 100000}], last_user_message="y" * 100000))
    expected = SessionView.from_session(store.get("s")).to_dict()
    statements: list[str] = []
    db._conn().set_trace_callback(statements.append)

    def full_session_forbidden(*args, **kwargs) -> None:
        raise AssertionError("full lifecycle Session constructed in list path")

    monkeypatch.setattr(store_module, "Session", full_session_forbidden)
    actual = SessionService(store).list_views()
    assert [view.to_dict() for view in actual] == [expected]
    reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1
    assert "SELECT *" not in reads[0].upper()
    assert not any(name in reads[0] for name in ("task_history", "last_user_message", "last_artifact_path"))


def test_full_view_equality_with_defaults_origins_reasons_and_queue(store_db, monkeypatch) -> None:
    from src.core.session_reason import derive_session_reasons

    store, db = store_db
    monkeypatch.setattr(config.claude, "default_model", "haiku")
    for i, status in enumerate(SessionStatus):
        session = _session(
            f"s{i}", status, machine_id="carrier-a", backend_session_id="" if i % 2 else "active-native",
            previous_backend_session_ids=["older", "previous-native"], model="opus" if i % 2 else None,
            effort="high", last_summary="old", last_result_summary="result" if i % 2 else "",
            last_files_modified=["a.py"], origin=SessionOrigin("web", "cron"),
            continued_from="parent", keep_pinned=i % 2 == 0, keep_note="keep",
            task_history=[{"task_id": "history", "output": "x" * 10000}],
        )
        db.upsert_session(session)
    case_id = db.open_case("objective", "s0", role="manager")
    db.append_flow_event(case_id, "flow.quota_paused", "system", payload={"limit": "usage"})
    with db._write() as conn:
        conn.execute("UPDATE sessions SET origin='null' WHERE session_id='s1'")
        conn.execute("UPDATE sessions SET origin='bad-json' WHERE session_id='s2'")
        conn.execute("UPDATE sessions SET current_case_id=? WHERE session_id='s0'", (case_id,))
        conn.execute("UPDATE sessions SET turn_queue_enrolled=1, turn_queue_paused=1 WHERE session_id='s0'")
    db.refresh_enrollment_presence()
    full = store.list_all(limit=1000)
    reasons = derive_session_reasons(db, full)
    queues = session_turn_queue_overlay(db, [s.session_id for s in full])
    expected = [SessionView.from_session(s).with_reason(reasons[s.session_id]).with_turn_queue(queues.get(s.session_id)).to_dict() for s in full]
    assert [v.to_dict() for v in SessionService(store).list_views(limit=1000, db=db)] == expected
    assert expected[0]["default_model"] == "haiku"
    assert any(v.get("turn_queue") for v in expected)
    assert any(v["reason"] and v["reason"]["kind"] == "paused_quota" for v in expected)


def test_full_readers_and_save_keep_history(store_db) -> None:
    store, db = store_db
    session = _session("s", task_history=[{"task_id": "old", "output": "keep"}], last_user_message="message")
    store.save(session, touch=False)
    SessionService(store).list_views()
    full = store.get("s")
    assert full.task_history == store.list_all()[0].task_history == session.task_history
    assert full.last_user_message == "message"
    store.save(full, touch=False)
    assert json.loads(db.get_session("s")["task_history"]) == session.task_history


@pytest.mark.parametrize("failure", ["missing", "error"])
def test_json_fallback_equals_legacy_and_filters_before_limit(store_db, monkeypatch, failure) -> None:
    store, db = store_db
    for i in range(3):
        store.save(_session(f"s{i}", keep_pinned=i == 0, origin=SessionOrigin("web", "user")), touch=False)
    (store_module._SESSIONS_DIR / "malformed.json").write_text("{", encoding="utf-8")
    invalid = asdict(_session("invalid"))
    invalid["status"] = "unknown"
    (store_module._SESSIONS_DIR / "invalid.json").write_text(json.dumps(invalid), encoding="utf-8")
    if failure == "missing":
        monkeypatch.setattr(db_module, "get_db", lambda: None)
    else:
        def unavailable(*args, **kwargs) -> None:
            raise sqlite3.OperationalError("offline")
        monkeypatch.setattr(db, "list_sessions", unavailable)
        monkeypatch.setattr(db, "list_session_summaries", unavailable, raising=False)
    expected = [SessionView.from_session(s).to_dict() for s in store.list_all(limit=1, keep_pinned=True)]
    assert [v.to_dict() for v in SessionService(store).list_views(limit=1, keep_pinned=True)] == expected
    assert expected[0]["session_id"] == "s0"


def test_healthy_empty_does_not_fallback_and_bad_db_rows_are_skipped(store_db, monkeypatch) -> None:
    store, db = store_db
    mirror = asdict(_session("json-only"))
    mirror["status"] = "idle"
    (store_module._SESSIONS_DIR / "json-only.json").write_text(json.dumps(mirror), encoding="utf-8")
    assert SessionService(store).list_views() == []
    for sid in ("good", "bad-status", "bad-cache", "legacy-types"):
        db.upsert_session(_session(sid))
    with db._write() as conn:
        conn.execute("UPDATE sessions SET status='unknown' WHERE session_id='bad-status'")
        conn.execute("UPDATE sessions SET cache_unhealthy_count='invalid' WHERE session_id='bad-cache'")
        conn.execute("UPDATE sessions SET last_files_modified='\"ab\"', origin='null', model='' WHERE session_id='legacy-types'")
    expected = [SessionView.from_session(s).to_dict() for s in store.list_all()]
    assert [v.to_dict() for v in SessionService(store).list_views()] == expected
    assert {v["session_id"] for v in expected} == {"good", "legacy-types"}


def test_nullable_legacy_pin_keeps_coalesce_filter_semantics(store_db, monkeypatch) -> None:
    store, db = store_db
    db.upsert_session(_session("null-pin"))
    db.upsert_session(_session("pinned", keep_pinned=True))
    # Current schema is NOT NULL; CTAS models a historical nullable table without
    # weakening the production schema or changing the tested reader's SQL.
    legacy = sqlite3.connect(":memory:")
    legacy.row_factory = sqlite3.Row
    try:
        legacy.execute("ATTACH DATABASE ? AS source", (str(db._path),))
        legacy.execute("CREATE TABLE sessions AS SELECT * FROM source.sessions")
        legacy.execute("DETACH DATABASE source")
        legacy.execute("UPDATE sessions SET keep_pinned=NULL, origin=NULL WHERE session_id='null-pin'")
        legacy.commit()
        monkeypatch.setattr(db, "_conn", lambda: legacy)
        for pinned in (None, False, True):
            expected = [SessionView.from_session(s).to_dict() for s in store.list_all(keep_pinned=pinned)]
            assert [v.to_dict() for v in SessionService(store).list_views(keep_pinned=pinned)] == expected
        assert [v.session_id for v in SessionService(store).list_views(keep_pinned=False)] == ["null-pin"]
    finally:
        legacy.close()


def test_projection_is_frozen_and_has_no_lifecycle_history(store_db) -> None:
    from pydantic import ValidationError

    store, db = store_db
    db.upsert_session(_session("s"))
    projection = store.list_summaries()[0]
    assert not isinstance(projection, Session)
    assert not hasattr(projection, "task_history")
    with pytest.raises(ValidationError, match="frozen"):
        projection.status = SessionStatus.BUSY


def test_migration_48_adds_indexes_to_47_and_reopens_without_row_changes(store_db) -> None:
    _store, db = store_db
    db.upsert_session(_session("preserved", task_history=[{"task_id": "old"}]))
    before = db.get_session("preserved")
    with db._write() as conn:
        conn.execute("DROP INDEX idx_sessions_updated_at")
        conn.execute("DROP INDEX idx_sessions_pinned_updated_at")
        conn.execute("DELETE FROM schema_version WHERE version=48")
    path = str(db._path)
    db.close()
    for _ in range(2):
        reopened = MeshDB(path)
        try:
            assert reopened._conn().execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 48
            assert reopened.get_session("preserved") == before
            indexes = {r[1] for r in reopened._conn().execute("PRAGMA index_list(sessions)")}
            assert {"idx_sessions_updated_at", "idx_sessions_pinned_updated_at"} <= indexes
        finally:
            reopened.close()


@pytest.mark.parametrize("pinned", [None, False, True])
def test_indexes_preserve_ties_and_avoid_sorts(store_db, pinned) -> None:
    store, db = store_db
    for i in range(220):
        db.upsert_session(_session(f"s{i:03}", keep_pinned=i == 0))
    with db._write() as conn:
        conn.execute("UPDATE sessions SET updated_at='2026-01-01' WHERE session_id='s000'")
    where = "" if pinned is None else "WHERE COALESCE(keep_pinned,0)=?"
    args = [1000] if pinned is None else [int(pinned), 1000]
    baseline = [r[0] for r in db._conn().execute(f"SELECT session_id FROM sessions NOT INDEXED {where} ORDER BY updated_at DESC LIMIT ?", args)]
    statements: list[str] = []
    db._conn().set_trace_callback(statements.append)
    actual = SessionService(store).list_views(limit=1000, keep_pinned=pinned)
    assert [v.session_id for v in actual] == baseline
    query = next(s for s in statements if s.lstrip().upper().startswith("SELECT"))
    plan = [str(r[3]) for r in db._conn().execute("EXPLAIN QUERY PLAN " + query)]
    assert not any("TEMP B-TREE" in step for step in plan), plan
    if pinned:
        assert [v.session_id for v in actual] == ["s000"]


@pytest.mark.asyncio
async def test_sessions_gate_bounds_100_calls_through_serialization(store_db, monkeypatch) -> None:
    from src.control import control_api
    from src.control.routes import sessions as routes

    store, _db = store_db
    service = SessionService(store)
    class Orchestrator:
        session_service = service
        quota_coordinator = None
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "projection-token")
    slots = threading.BoundedSemaphore(4)
    monkeypatch.setattr(routes, "_SESSIONS_READ_SLOTS", slots, raising=False)
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = 0
    original_render = routes.JSONResponse.render

    def render(self, body) -> bytes:
        nonlocal calls
        if body == {"sessions": []}:
            with lock:
                calls += 1
                if calls == 4:
                    entered.set()
            assert release.wait(10)
        return original_render(self, body)

    monkeypatch.setattr(routes.JSONResponse, "render", render)
    transport = httpx.ASGITransport(app=control_api.build_control_api(Orchestrator()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": "Bearer projection-token"}) as client:
        first = [asyncio.create_task(client.get("/api/sessions")) for _ in range(4)]
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            extra = await asyncio.gather(*(client.get("/api/sessions") for _ in range(96)))
            assert all(r.status_code == 429 and r.json() == {"detail": "sessions_busy"} for r in extra)
            assert calls == 4
        finally:
            release.set()
            results = await asyncio.gather(*first)
        assert all(r.status_code == 200 for r in results)
        assert (await client.get("/api/sessions")).status_code == 200
        assert (await client.get("/api/sessions?limit=0")).status_code == 422

        def unavailable(*args, **kwargs) -> None:
            raise sqlite3.OperationalError("backing read unavailable")

        monkeypatch.setattr(service, "list_views", unavailable)
        response = await client.get("/api/sessions")
        assert response.status_code == 503 and response.json() == {"detail": "sessions_unavailable"}

        def serialization_failure(self, body) -> bytes:
            raise RuntimeError("serialization failed")

        monkeypatch.setattr(routes.JSONResponse, "render", serialization_failure)
        with pytest.raises(RuntimeError, match="serialization failed"):
            await client.get("/api/sessions")
    for _ in range(4):
        assert slots.acquire(blocking=False)
    for _ in range(4):
        slots.release()


def test_summary_read_discloses_fallback_and_partial_rows(store_db, monkeypatch) -> None:
    store, db = store_db
    store.save(_session("saved"), touch=False)
    assert store.read_summaries().source == "database"
    with db._write() as conn:
        conn.execute("UPDATE sessions SET status='invalid'")
    read = store.read_summaries()
    assert read.partial and read.sessions == []
    monkeypatch.setattr(db_module, "get_db", lambda: None)
    read = store.read_summaries()
    assert read.source == "json" and [s.session_id for s in read.sessions] == ["saved"]
    (store_module._SESSIONS_DIR / "bad.json").write_text("{")
    assert store.read_summaries().partial


def test_api_discloses_fallback_and_partial_inventory(store_db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from src.control import control_api
    store, db = store_db
    store.save(_session("saved"), touch=False)
    class Orchestrator:
        session_service = SessionService(store)
        quota_coordinator = None
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "test")
    monkeypatch.setattr(control_api, "_db", lambda: db)
    with TestClient(control_api.build_control_api(Orchestrator())) as client:
        headers = {"Authorization": "Bearer test"}
        assert "read_state" not in client.get("/api/sessions", headers=headers).json()
        with db._write() as conn:
            conn.execute("UPDATE sessions SET status='invalid'")
        response = client.get("/api/sessions", headers=headers)
        assert response.json() == {"sessions": [], "read_state": "partial"}
        monkeypatch.setattr(db_module, "get_db", lambda: None)
        response = client.get("/api/sessions", headers=headers)
        assert response.status_code == 200 and response.json()["read_state"] == "fallback"
        assert response.json()["sessions"][0]["session_id"] == "saved"


def test_api_queue_failure_is_unavailable_not_a_missing_overlay(store_db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from src.control import control_api
    store, db = store_db
    store.save(_session("saved"), touch=False)
    class Orchestrator:
        session_service = SessionService(store)
        quota_coordinator = None
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "test")
    monkeypatch.setattr(control_api, "_db", lambda: db)
    monkeypatch.setattr(db, "any_session_enrolled", lambda: True)
    def fail(*args):
        raise RuntimeError("private error")
    monkeypatch.setattr(db, "session_turn_queue_states", fail)
    with TestClient(control_api.build_control_api(Orchestrator())) as client:
        response = client.get("/api/sessions", headers={"Authorization": "Bearer test"})
        assert response.status_code == 503
        assert response.json() == {"detail": "sessions_unavailable"}


def test_explicit_file_only_gateway_keeps_healthy_session_reads(store_db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from src.control import control_api, controller_state
    store, _db = store_db
    store.save(_session("saved"), touch=False)
    monkeypatch.setattr(config.mesh, "shadow_write", False)
    monkeypatch.setattr(db_module, "get_db", lambda: None)
    monkeypatch.setattr(controller_state, "active", lambda: None)
    monkeypatch.setattr(control_api, "_db", lambda: None)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "test")
    class Orchestrator:
        session_service = SessionService(store)
        quota_coordinator = None
    with TestClient(control_api.build_control_api(Orchestrator())) as client:
        result = client.get("/api/sessions", headers={"Authorization": "Bearer test"})
        assert result.status_code == 200
        assert result.json()["sessions"][0]["session_id"] == "saved"
        assert "read_state" not in result.json()
        assert client.get("/api/sessions?keep_pinned=true", headers={"Authorization": "Bearer test"}).json() == {"sessions": []}
        # An unreadable recovery row is still partial, even in file-only mode.
        (store_module._SESSIONS_DIR / "bad.json").write_text("{")
        assert client.get("/api/sessions", headers={"Authorization": "Bearer test"}).json()["read_state"] == "partial"
