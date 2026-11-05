"""A109: real receipt/queue transactions, no agent or subprocess execution."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.control import turn_admission as ta
from src.control.db import producer_turn_id
from src.control.turn_queue import OwnershipConflictError
from tests.test_job_completion_reliability import _register
from tests.test_turn_queue_4b import _pass, _run
from tests.test_turn_queue_4d import _env, _kind, _process, _restart
from tests.test_turn_queue_producer1 import _no_cli_spawn  # noqa: F401


@pytest.fixture(autouse=True)
def allowance(monkeypatch):
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


def _terminal(db):
    _register(db, "job_result", agent=True)
    db.complete_job("job_result", 0, "finished evidence")
    return db.get_job("job_result")


def test_consumed_before_poll_remains_consumed_after_restart(tmp_path, monkeypatch):
    db, orch = _env(tmp_path, monkeypatch)
    job = _terminal(db)
    result = db.consume_job_result("job_result", "sess-1", "worker-a")
    assert result["consumed"] is True and result["tail"] == "finished evidence"
    restarted = _restart(orch)
    _process(restarted, job)
    _process(restarted, job)
    assert _kind(db, "watched_job") == []
    assert restarted.notifier.calls == ["job_result"]  # operator notice remains independent
    assert db.get_job_completion("job_result")["consumed_at"]


def test_consumed_job_is_one_system_notice_and_not_completed_chat(tmp_path, monkeypatch):
    from src.control import db as db_module, transcript
    from src.control.session_timeline import build_session_timeline

    db, orch = _env(tmp_path, monkeypatch)
    _process(orch, _terminal(db))
    monkeypatch.setattr(db_module, "_db_instance", db)
    before = transcript.get_transcript(tmp_path, tmp_path, "sess-1")
    assert len(before) == 1
    assert before[0]["kind"] == "system_notice"
    assert before[0]["task_id"] == "job_result"
    assert before[0]["instruction"] == ""

    db.consume_job_result("job_result", "sess-1", "worker-a")
    after = transcript.get_transcript(tmp_path, tmp_path, "sess-1")
    assert len(after) == 1
    assert after[0]["task_id"] == before[0]["task_id"]
    assert "Result consumed" in after[0]["result"]
    assert "withdrawn" not in after[0]["result"]
    assert db.get_session_turns("sess-1") == []
    assert db.get_session_turns_tail("sess-1") == []  # no fake restart context
    activity = build_session_timeline(db=db, telemetry_store=None, session_id="sess-1")
    assert [(item.kind, item.job_id) for item in activity.items] == [("job_state", "job_result")]
    # Internal audit remains durable, including for already-existing records.
    assert db.get_task("job_result")["action"] == "watched_job"


@pytest.mark.parametrize("phase", ["queued", "pending", "claimed"])
def test_consumption_fences_every_unstarted_phase(tmp_path, monkeypatch, phase):
    db, orch = _env(tmp_path, monkeypatch)
    _process(orch, _terminal(db))
    wake = _kind(db, "watched_job")[0]["id"]
    if phase != "queued":
        _pass(db, orch)
    token = None
    if phase == "claimed":
        token = db.claim_turn(wake, "worker-a", "worker_daemon", "inc-1")
    assert db.get_task(wake)["status"] == phase
    result = db.consume_job_result("job_result", "sess-1", "worker-a")
    assert result["consumed"]
    assert db.get_task(wake)["status"] == "withdrawn"
    assert db.get_task(wake)["effects_state"] == "telemetry"
    if token:
        with pytest.raises(OwnershipConflictError):
            db.start_turn(wake, token, incarnation_id="inc-1")
    _process(_restart(orch), db.get_job("job_result"))
    assert len(_kind(db, "watched_job")) == 1
    assert db.get_task(wake)["status"] == "withdrawn"


def test_running_delivery_wins_and_cannot_be_consumed_elsewhere(tmp_path, monkeypatch):
    db, orch = _env(tmp_path, monkeypatch)
    _process(orch, _terminal(db))
    wake = _kind(db, "watched_job")[0]["id"]
    _pass(db, orch)
    _run(db, wake)
    with pytest.raises(OwnershipConflictError):
        db.consume_job_result("job_result", "sess-1", "worker-a")
    assert not db.get_job_completion("job_result")["consumed_at"]
    assert db.get_task(wake)["status"] == "running"


def test_running_job_read_does_not_consume_future_completion(tmp_path, monkeypatch):
    db, orch = _env(tmp_path, monkeypatch)
    _register(db, "job_result", agent=True)
    result = db.consume_job_result("job_result", "sess-1", "worker-a")
    assert result["status"] == "running" and result["consumed"] is False
    assert db.get_job_completion("job_result") is None
    db.complete_job("job_result", 0)
    _process(orch, db.get_job("job_result"))
    assert len(_kind(db, "watched_job")) == 1


@pytest.mark.parametrize("session,node", [("other", "worker-a"), ("sess-1", "other")])
def test_foreign_owner_cannot_read_or_consume(tmp_path, monkeypatch, session, node):
    db, _ = _env(tmp_path, monkeypatch)
    _terminal(db)
    with pytest.raises(PermissionError):
        db.consume_job_result("job_result", session, node)
    assert db.get_job_completion("job_result") is None


def test_consumption_during_admission_cannot_resurrect_wake(tmp_path, monkeypatch):
    db, orch = _env(tmp_path, monkeypatch)
    job = _terminal(db)
    original = db.enqueue_turn

    def enqueue(*args, **kwargs):
        db.consume_job_result("job_result", "sess-1", "worker-a")
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "enqueue_turn", enqueue)
    _process(orch, job)
    assert _kind(db, "watched_job") == []
    receipt = db.get_job_completion("job_result")
    assert receipt["action_state"] == "done" and receipt["consumed_at"]


def test_completed_wake_records_consumption(tmp_path, monkeypatch):
    db, orch = _env(tmp_path, monkeypatch)
    _process(orch, _terminal(db))
    wake = producer_turn_id("watched:job_result", "sess-1", 1, prefix="jturn")
    _pass(db, orch)
    token = _run(db, wake)
    db.complete_turn(wake, token, {"success": True})
    assert db.get_job_completion("job_result")["consumed_at"]
    assert db.consume_job_result("job_result", "sess-1", "worker-a")["consumed"]


def test_start_and_consumption_serialize_to_one_winner(tmp_path, monkeypatch):
    db, orch = _env(tmp_path, monkeypatch)
    _process(orch, _terminal(db))
    wake = _kind(db, "watched_job")[0]["id"]
    _pass(db, orch)
    token = db.claim_turn(wake, "worker-a", "worker_daemon", "inc-1")
    barrier = threading.Barrier(2)

    def start():
        barrier.wait(timeout=5)
        try:
            db.start_turn(wake, token, incarnation_id="inc-1")
            return "started"
        except OwnershipConflictError:
            return "refused"

    def consume():
        barrier.wait(timeout=5)
        try:
            db.consume_job_result("job_result", "sess-1", "worker-a")
            return "consumed"
        except OwnershipConflictError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start), pool.submit(consume)]
        results = [f.result(timeout=10) for f in futures]
    assert results.count("refused") == 1
    assert bool(db.get_job_completion("job_result")["consumed_at"]) == ("consumed" in results)
    assert db.get_task(wake)["status"] == ("withdrawn" if "consumed" in results else "running")


def test_audit_failure_rolls_back_consumption_and_withdrawal(tmp_path, monkeypatch):
    from src.control import db as db_module

    db, orch = _env(tmp_path, monkeypatch)
    _process(orch, _terminal(db))
    wake = _kind(db, "watched_job")[0]["id"]

    def fail(*args):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(db_module, "_insert_watched_job_withdrawal_audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        db.consume_job_result("job_result", "sess-1", "worker-a")
    assert db.get_task(wake)["status"] == "queued"
    assert not db.get_job_completion("job_result")["consumed_at"]
