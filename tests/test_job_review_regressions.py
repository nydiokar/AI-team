"""Adversarial A109/A110 checks; no live worker or model invocation."""
from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from src.control import task_server
from src.control.telemetry_store import TelemetryStore
from src.core.telemetry import build_event
from tests.test_telemetry_store import _events
from tests.test_turn_queue_4d import _env
from tests.test_job_completion_reliability import _register
from tests.test_turn_queue_producer1 import _no_cli_spawn, _submit  # noqa: F401


def _event(name: str, tid: str, attrs: dict | None = None):
    return build_event(name, turn_id=tid, session_id="sess-1", node_id="gateway",
        emitter_process_instance_id="review", source="gateway", attributes=attrs or {})


def test_rebuild_preserves_canonical_withdrawal_over_old_cancelled_events(tmp_path, monkeypatch):
    db, orch = _env(tmp_path, monkeypatch)
    tid = _submit(orch, operation_id="review-history")
    db.withdraw_turn(tid, actor="review")
    store = TelemetryStore(db)
    store.insert_events([
        _event("turn.accepted", tid, {"task_id": tid}),
        _event("turn.completed", tid, {"status": "cancelled"}),
    ])
    assert store.get_turn(tid)["final_status"] == "withdrawn"
    assert store.list_turns(session_id="sess-1") == []
    assert db.get_session_turn_counts(["sess-1"]) == {}


def test_bad_historical_event_is_flagged_without_starving_other_repairs(tmp_path, monkeypatch):
    db, _ = _env(tmp_path, monkeypatch)
    store = TelemetryStore(db)
    store.insert_events([_event("turn.accepted", "bad-history"),
                         _event("turn.accepted", "good-history")])
    store.insert_events([_event("invocation.created", "bad-history", {"attempt": "invalid"}).model_copy(
        update={"invocation_id": "bad-invocation"})], rebuild=False)
    with db._write() as conn:
        conn.execute("UPDATE llm_turns SET projection_version=2")
    assert store.repair_turn_lifecycle(limit=5) == 2
    assert "lifecycle_repair_invalid_events" in store.get_turn("bad-history")["data_quality"]
    assert store.get_turn("good-history")["projection_version"] == 3
    assert store.repair_turn_lifecycle(limit=5) == 0


def test_retention_winning_during_replay_preserves_retained_usage(tmp_path, monkeypatch):
    db, _ = _env(tmp_path, monkeypatch)
    store = TelemetryStore(db)
    store.insert_events(_events())
    original = store._all_events

    def prune_after_read(tid):
        events = original(tid)
        with db._write() as conn:
            conn.execute("UPDATE llm_turns SET events_pruned_at='2026-11-05',metrics_json=? WHERE turn_id=?",
                         (json.dumps({"retained_usage": 777}), tid))
        return events

    monkeypatch.setattr(store, "_all_events", prune_after_read)
    assert store.rebuild_turn("turn_store") is None
    assert store.get_turn("turn_store")["metrics"] == {"retained_usage": 777}


@pytest.mark.asyncio
async def test_consumption_preserves_live_lineage_writer_lease(tmp_path, monkeypatch):
    db, orch = _env(tmp_path, monkeypatch)
    _register(db, "job_lineage", agent=True)
    db.complete_job("job_lineage", 0)
    original = orch._write_managed_lineage
    checked = []

    async def consume_before_lineage(task, tid, token):
        before = db.get_task(tid)
        assert before["lineage_state"] == "pending" and before["lineage_lease_until"]
        db.consume_job_result("job_lineage", "sess-1", "worker-a")
        after = db.get_task(tid)
        checked.append(after["lineage_lease_until"])
        assert after["lineage_lease_until"] == before["lineage_lease_until"]
        assert orch._void_withdrawn_lineage(tid) is False
        return await original(task, tid, token)

    monkeypatch.setattr(orch, "_write_managed_lineage", consume_before_lineage)
    await orch._process_terminal_job(db.get_job("job_lineage"))
    assert checked and checked[0] is not None


@pytest.mark.asyncio
async def test_failed_history_repair_does_not_drop_current_telemetry(tmp_path, monkeypatch):
    db, _ = _env(tmp_path, monkeypatch)
    store = TelemetryStore(db)
    tid = "fresh-telemetry"
    store.insert_events([_event("turn.accepted", tid)], rebuild=False)
    task_server._drain_projection()
    task_server._enqueue_projection([tid], [])
    monkeypatch.setattr(task_server, "get_db", lambda: db)
    monkeypatch.setattr(TelemetryStore, "repair_turn_lifecycle",
        Mock(side_effect=ValueError("invalid old event")))
    original_drain = task_server._drain_projection
    drained = asyncio.Event()

    def drain(*args):
        result = original_drain(*args)
        if result[0]:
            drained.set()
        return result

    monkeypatch.setattr(task_server, "_drain_projection", drain)
    runner = asyncio.create_task(task_server._telemetry_projection_flusher_loop(interval_sec=0.01))
    try:
        await asyncio.wait_for(drained.wait(), 2)
        for _ in range(100):
            if store.get_turn(tid):
                break
            await asyncio.sleep(0.01)
        assert store.get_turn(tid) is not None
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        task_server._drain_projection()


@pytest.mark.asyncio
async def test_cancelled_result_request_holds_slot_until_database_work_finishes(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    slots = threading.BoundedSemaphore(1)

    def consume(*args):
        entered.set()
        try:
            assert release.wait(3)
            return {"consumed": True}
        finally:
            completed.set()

    monkeypatch.setattr(task_server, "get_db", lambda: Mock(consume_job_result=consume))
    monkeypatch.setattr(task_server, "_JOB_RESULT_SLOTS", slots)
    body = json.dumps({"session_id": "sess-1", "node_id": "worker-a"}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "headers": []}, receive=receive)
    caller = asyncio.create_task(task_server.consume_job_result("job_review", request))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        acquired = slots.acquire(blocking=False)
        if acquired:
            slots.release()
        assert not acquired, "cancelled HTTP waiter released a still-running database slot"
    finally:
        release.set()
        assert await asyncio.to_thread(completed.wait, 2)
        if not caller.done():
            await caller
    for _ in range(100):
        if slots.acquire(blocking=False):
            slots.release()
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("completed database work leaked its permit")
