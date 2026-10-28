"""Durable watched-job discovery/effect recovery. Real SQLite, no paid backend."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from unittest.mock import AsyncMock

import pytest

from config import config
from src.control import db as db_module
from src.control import turn_admission as ta
from src.control.db import MeshDB, producer_turn_id
from tests.test_turn_queue_4d import _env, _job, _kind, _restart
from tests.test_turn_queue_producer1 import _flags, _no_cli_spawn  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_allowance(monkeypatch) -> None:
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())
    monkeypatch.setattr(config.mesh, "jobs_read_mode", "controller")


def _register(db: MeshDB, job_id: str, *, agent: bool = False) -> None:
    db.register_job(job_id=job_id, session_id="sess-1", node_id="worker-a",
                    label="offline job", notify=True, notify_agent=agent)


def _expire(db: MeshDB) -> None:
    with db._write() as conn:
        conn.execute("UPDATE job_completion_effects SET lease_until=0")


@pytest.mark.asyncio
async def test_delayed_commit_is_missed_by_timestamp_but_durably_discovered(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    _register(db, "delayed")
    _register(db, "other")
    db.complete_job("other", exit_code=0)
    with db._write() as conn:
        conn.execute("UPDATE jobs SET updated_at='2026-10-28T00:00:11Z' WHERE id='other'")
    timestamped = threading.Event()
    main_thread = threading.get_ident()
    original_now = db_module._now

    def old_timestamp() -> str:
        if threading.get_ident() != main_thread:
            timestamped.set()
            return "2026-10-28T00:00:10Z"
        return original_now()

    monkeypatch.setattr(db_module, "_now", old_timestamp)
    db._write_lock.acquire()
    writer = threading.Thread(target=db.complete_job, args=("delayed", 0))
    writer.start()
    try:
        assert await asyncio.to_thread(timestamped.wait, 5)
        assert [j["id"] for j in db.get_terminal_jobs_since("2026-10-28T00:00:00Z")] == ["other"]
        # The old poll advanced to :20 here. Even a pre-read :12 boundary loses it.
    finally:
        db._write_lock.release()
        await asyncio.to_thread(writer.join, 5)
    assert not writer.is_alive()
    assert db.get_job("delayed")["status"] == "done"
    assert db.get_terminal_jobs_since("2026-10-28T00:00:12Z") == []
    orch.notifier.notify_task_outcome = AsyncMock()
    await orch._poll_job_completions_once()
    assert {c.args[0] for c in orch.notifier.notify_task_outcome.call_args_list} == {"delayed", "other"}


@pytest.mark.asyncio
async def test_notification_failure_is_retried_not_marked_processed(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    orch.notifier.notify_task_outcome = AsyncMock(side_effect=[RuntimeError("send failed"), None])
    job = _job(notify_agent=0)
    await orch._process_terminal_job(job)
    await orch._process_terminal_job(job)
    assert orch.notifier.notify_task_outcome.call_count == 2
    receipt = db.get_job_completion("job_x")
    assert receipt["notification_state"] == receipt["action_state"] == "done"
    assert receipt["notification_attempts"] == 2
    assert db.get_task("job_x")["reply_text"]


@pytest.mark.asyncio
async def test_notification_retry_does_not_duplicate_admission(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    orch.notifier.notify_task_outcome = AsyncMock(side_effect=[RuntimeError("send failed"), None])
    await orch._process_terminal_job(_job())
    assert len(_kind(db, "watched_job")) == 1
    await orch._process_terminal_job(_job())
    await orch._process_terminal_job(_job())
    assert orch.notifier.notify_task_outcome.call_count == 2
    assert len(_kind(db, "watched_job")) == 1


@pytest.mark.asyncio
async def test_admission_failure_retries_without_repeating_successful_notice(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    submit = orch.submit_instruction
    orch.submit_instruction = AsyncMock(side_effect=RuntimeError("carrier read failed"))
    orch.notifier.notify_task_outcome = AsyncMock()
    await orch._process_terminal_job(_job())
    assert db.get_job_completion("job_x")["action_state"] == "pending"
    orch.submit_instruction = submit
    restarted = _restart(orch)
    restarted.notifier = orch.notifier
    await restarted._process_terminal_job(_job())
    assert orch.notifier.notify_task_outcome.call_count == 1
    assert len(_kind(db, "watched_job")) == 1
    assert db.get_job_completion("job_x")["action_state"] == "done"


@pytest.mark.asyncio
async def test_commit_before_ack_replays_one_turn_across_real_db_reopen(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    original = db.finish_job_effect

    def lost_ack(job_id, token, effect, state, error="") -> bool:
        if effect == "action":
            raise sqlite3.OperationalError("ack storage failed")
        return original(job_id, token, effect, state, error)

    monkeypatch.setattr(db, "finish_job_effect", lost_ack)
    await orch._process_terminal_job(_job())
    assert len(_kind(db, "watched_job")) == 1
    path = str(db._path)
    db.close()
    reopened = MeshDB(path)
    monkeypatch.setattr(db_module, "get_db", lambda: reopened)
    _expire(reopened)
    try:
        restarted = _restart(orch)
        await restarted._process_terminal_job(_job())
        assert len(_kind(reopened, "watched_job")) == 1
        assert reopened.get_job_completion("job_x")["action_state"] == "done"
        assert restarted.notifier.calls == []  # successful notice receipt survived
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_send_before_ack_crash_can_repeat_notice_but_not_action(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    original = db.finish_job_effect
    orch.notifier.notify_task_outcome = AsyncMock()

    def lost_ack(job_id, token, effect, state, error="") -> bool:
        if effect == "notification":
            raise sqlite3.OperationalError("ack failed after send")
        return original(job_id, token, effect, state, error)

    monkeypatch.setattr(db, "finish_job_effect", lost_ack)
    await orch._process_terminal_job(_job())
    monkeypatch.setattr(db, "finish_job_effect", original)
    _expire(db)
    restarted = _restart(orch)
    restarted.notifier = orch.notifier
    await restarted._process_terminal_job(_job())
    assert orch.notifier.notify_task_outcome.call_count == 2  # explicitly at-least-once attempt
    assert len(_kind(db, "watched_job")) == 1


@pytest.mark.asyncio
async def test_failed_notice_is_bounded_and_does_not_starve_later_jobs(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    orch.notifier.notify_task_outcome = AsyncMock(side_effect=RuntimeError("offline"))
    for _ in range(8):
        await orch._process_terminal_job(_job(notify_agent=0))
    assert orch.notifier.notify_task_outcome.call_count == 5
    assert db.get_job_completion("job_x")["notification_state"] == "dead"
    orch.notifier.notify_task_outcome = AsyncMock()
    await orch._process_terminal_job(_job("later", notify_agent=0))
    assert db.get_job_completion("later")["notification_state"] == "done"


@pytest.mark.asyncio
async def test_active_lease_excludes_second_consumer_and_stale_ack(tmp_path, monkeypatch) -> None:
    db, _orch = _env(tmp_path, monkeypatch)
    db.stage_job_completion(_job())
    first = db.claim_job_completion("job_x")
    assert first and db.claim_job_completion("job_x") is None
    _expire(db)
    second = db.claim_job_completion("job_x")
    assert second and first["lease_token"] != second["lease_token"]
    assert not db.finish_job_effect("job_x", first["lease_token"], "action", "done")
    assert db.finish_job_effect("job_x", second["lease_token"], "action", "done")


@pytest.mark.asyncio
async def test_completion_before_gateway_start_is_found_without_wall_clock(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    _register(db, "pre-start", agent=True)
    db.complete_job("pre-start", exit_code=0)
    with db._write() as conn:
        conn.execute("UPDATE jobs SET updated_at='2000-01-01', started_epoch=1")
    restarted = _restart(orch)
    await restarted._poll_job_completions_once()
    await restarted._poll_job_completions_once()
    assert restarted.notifier.calls == ["pre-start"]
    assert [r["id"] for r in _kind(db, "watched_job")] == [producer_turn_id("watched:pre-start", "sess-1", 1, prefix="jturn")]


@pytest.mark.asyncio
async def test_actual_notification_layers_propagate_only_opt_in_errors(tmp_path, monkeypatch) -> None:
    from src.services.notification_service import NotificationService
    from src.telegram.interface import TelegramInterface

    db, orch = _env(tmp_path, monkeypatch)
    session = orch.session_store.get("sess-1")
    session.telegram_chat_id = 42
    orch.session_store.save(session)
    tg = TelegramInterface.__new__(TelegramInterface)
    tg.app = object()
    tg.is_running = True
    tg._find_session_for_task = lambda *args, **kwargs: None
    tg._send_long_message = AsyncMock(side_effect=RuntimeError("Telegram send failed"))
    orch.telegram_interface = tg
    orch.notifier = NotificationService(orch)
    monkeypatch.setattr(orch.notifier, "_maybe_push_outcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(orch.notifier, "_maybe_push_cost_alerts", lambda: None)
    await orch._process_terminal_job(_job(notify_agent=0))
    assert db.get_job_completion("job_x")["notification_state"] == "pending"
    tg._send_long_message = AsyncMock()
    await _restart_with_notifier(orch)._process_terminal_job(_job(notify_agent=0))
    assert tg._send_long_message.call_count == 1
    assert db.get_job_completion("job_x")["notification_state"] == "done"
    # Every unrelated caller keeps the established no-raise behavior.
    tg._send_long_message = AsyncMock(side_effect=RuntimeError("still down"))
    await tg.notify_completion("ordinary", "reply", chat_id=42)
    with pytest.raises(RuntimeError, match="Telegram send failed|still down"):
        await tg.notify_completion("watched", "reply", chat_id=42, reliable=True)


def _restart_with_notifier(orch):
    restarted = _restart(orch)
    restarted.notifier = orch.notifier
    return restarted


@pytest.mark.asyncio
async def test_cancelled_send_recovers_from_durable_intent_after_restart(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    orch.notifier.notify_task_outcome = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await orch._process_terminal_job(_job())
    assert db.get_job_completion("job_x")["notification_state"] == "pending"
    assert db.claim_job_completion("job_x") is None  # cannot race the in-flight lease
    _expire(db)
    restarted = _restart(orch)
    await restarted._process_terminal_job(_job())
    assert restarted.notifier.calls == ["job_x"]
    assert len(_kind(db, "watched_job")) == 1


@pytest.mark.asyncio
async def test_notice_timeout_is_unknown_and_retryable_without_duplicate_admission(tmp_path, monkeypatch) -> None:
    from src.control import job_completion

    db, orch = _env(tmp_path, monkeypatch)
    sends: list[str] = []

    async def unknown_send(task_id, *args, **kwargs) -> None:
        sends.append(task_id)  # transport may have accepted the message
        await asyncio.Future()

    monkeypatch.setattr(job_completion, "JOB_NOTICE_TIMEOUT_SEC", 0.01)
    orch.notifier.notify_task_outcome = unknown_send
    await orch._process_terminal_job(_job())
    await _restart_with_notifier(orch)._process_terminal_job(_job())
    assert sends == ["job_x", "job_x"]
    assert db.get_job_completion("job_x")["notification_state"] == "pending"
    assert len(_kind(db, "watched_job")) == 1


@pytest.mark.asyncio
async def test_storage_failure_does_not_spend_admission_retry_budget(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    original = orch.submit_instruction
    orch.submit_instruction = AsyncMock(side_effect=sqlite3.OperationalError("database is locked"))
    for _ in range(7):
        await orch._process_terminal_job(_job())
    receipt = db.get_job_completion("job_x")
    assert receipt["action_state"] == "pending" and receipt["action_attempts"] == 0
    assert orch.notifier.calls == ["job_x"]
    orch.submit_instruction = original
    await orch._process_terminal_job(_job())
    assert len(_kind(db, "watched_job")) == 1


@pytest.mark.asyncio
async def test_last_attempt_committed_before_crash_is_reconciled_before_exhaustion(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    original = db.finish_job_effect

    def no_action_ack(job_id, token, effect, state, error="") -> bool:
        if effect == "action":
            raise sqlite3.OperationalError("crashed after commit")
        return original(job_id, token, effect, state, error)

    monkeypatch.setattr(db, "finish_job_effect", no_action_ack)
    await orch._process_terminal_job(_job())
    with db._write() as conn:
        conn.execute("UPDATE job_completion_effects SET action_attempts=5, lease_until=0")
    monkeypatch.setattr(db, "finish_job_effect", original)
    await _restart(orch)._process_terminal_job(_job())
    assert db.get_job_completion("job_x")["action_state"] == "done"
    assert len(_kind(db, "watched_job")) == 1


@pytest.mark.asyncio
async def test_two_live_consumers_cannot_send_or_admit_twice(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    notices: list[str] = []

    async def blocked(task_id, *args, **kwargs) -> None:
        notices.append(task_id)
        entered.set()
        await release.wait()

    orch.notifier.notify_task_outcome = blocked
    first = asyncio.create_task(orch._process_terminal_job(_job()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.gather(*(orch._process_terminal_job(_job()) for _ in range(10)))
        assert notices == ["job_x"]
    finally:
        release.set()
        await first
    assert len(_kind(db, "watched_job")) == 1


@pytest.mark.asyncio
async def test_permanent_closed_session_refusal_is_honest_and_not_retried(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    assert orch.session_service.close_session("sess-1", backends=orch._backends).ok
    await orch._process_terminal_job(_job())
    await _restart(orch)._process_terminal_job(_job())
    receipt = db.get_job_completion("job_x")
    assert receipt["action_state"] == "dead" and receipt["action_attempts"] == 1
    assert "action failed" in db.get_task("job_x")["reply_text"]
    assert _kind(db, "watched_job") == []


@pytest.mark.asyncio
async def test_full_poll_to_durable_turn_execution_replay_preserves_one_identity(tmp_path, monkeypatch) -> None:
    from tests.test_turn_queue_4b import _pass, _run

    db, orch = _env(tmp_path, monkeypatch)
    _register(db, "end-to-end", agent=True)
    db.complete_job("end-to-end", 0)
    await orch._poll_job_completions_once()
    turn = _kind(db, "watched_job")[0]
    # These scheduler helpers use asyncio.run, so invoke them in a worker.
    await asyncio.to_thread(_pass, db, orch)
    token = await asyncio.to_thread(_run, db, turn["id"])
    db.complete_turn(turn["id"], token, {"success": True})
    restarted = _restart(orch)
    await restarted._poll_job_completions_once()
    assert [r["id"] for r in _kind(db, "watched_job")] == [turn["id"]]
    assert db.get_task(turn["id"])["status"] == "completed"
    assert restarted.notifier.calls == []


@pytest.mark.asyncio
async def test_spent_uncertain_notice_budget_stops_sending_after_restart(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    db.stage_job_completion(_job())
    with db._write() as conn:
        conn.execute("UPDATE job_completion_effects SET notification_attempts=5")
    await orch._process_terminal_job(_job())
    assert orch.notifier.calls == []
    assert db.get_job_completion("job_x")["notification_state"] == "dead"
    assert len(_kind(db, "watched_job")) == 1


@pytest.mark.asyncio
async def test_soft_pass_budget_leaves_unattempted_jobs_durably_pending(tmp_path, monkeypatch) -> None:
    from src.control import job_completion

    db, orch = _env(tmp_path, monkeypatch)
    for i in range(3):
        _register(db, f"budget-{i}")
        db.complete_job(f"budget-{i}", 0)
    monkeypatch.setattr(job_completion, "JOB_COMPLETION_PASS_BUDGET_SEC", 0)
    assert await orch._poll_job_completions_once() == 1
    assert len(db.pending_job_completions()) == 2
    monkeypatch.setattr(job_completion, "JOB_COMPLETION_PASS_BUDGET_SEC", 20)
    assert await orch._poll_job_completions_once() == 2
    assert len(orch.notifier.calls) == 3


@pytest.mark.asyncio
async def test_invalid_legacy_owner_cannot_poison_discovery_or_be_truncated(tmp_path, monkeypatch) -> None:
    db, orch = _env(tmp_path, monkeypatch)
    db.register_job(job_id="bad-owner", node_id="worker-a", label="bad", session_id="s" * 129)
    db.complete_job("bad-owner", 0)
    _register(db, "valid-owner")
    db.complete_job("valid-owner", 0)
    await orch._poll_job_completions_once()
    assert orch.notifier.calls == ["valid-owner"]
    receipt = db.get_job_completion("bad-owner")
    assert receipt["notification_state"] == receipt["action_state"] == "dead"
    assert db.get_job("bad-owner")["session_id"] == "s" * 129  # original evidence retained
