"""A82 Stage 6 follow-up — Telegram parity with the web "Stop active" (§10).

Stop on an ENROLLED session sets the same PERSISTENT operator queue pause as
the web stop and cancels only the active turn; ``/session_resume`` is the
explicit release (the same service the web resume route uses); while paused, a
Telegram admission reply says the turn is queued but paused and how to resume
(never a silent stall). Unenrolled sessions keep the legacy behaviour.

Real ``TelegramInterface`` handlers over the real bound orchestrator and a real
file-backed ``MeshDB`` (helpers shared with the Stage-4b suite). No CLI.
"""
from __future__ import annotations

import asyncio
from typing import Any, List

from src.core.interfaces import SessionStatus
from tests.test_turn_queue_4b import (  # noqa: F401
    _Ctx, _Upd, _bot, _fresh_allowance, _no_cli_spawn, _pass, _run, _sess, _setup, _submit,
    _wire,
)


def _paused(db: Any, sid: str = "sess-1") -> bool:
    return bool(db.session_turn_queue_states([sid])[sid]["paused"])


def test_TG_R1_stop_pauses_persistently_and_resume_releases(tmp_path: Any, monkeypatch: Any) -> None:
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1: str = str(_submit(o, operation_id="a"))
    t2: str = str(_submit(o, operation_id="b"))
    _pass(db, o)
    tok: str = _run(db, t1)
    bot = _bot(o)
    upd = _Upd()
    asyncio.run(bot._handle_session_cancel(upd, _Ctx(["sess-1"])))
    assert t1 in upd.message.replies[-1] and "/session_resume" in upd.message.replies[-1]
    assert db.get_task(t1)["cancel_token"] == tok and db.get_task(t2)["status"] == "queued"
    assert _paused(db) is True
    db.complete_turn(t1, tok, {"success": False}, status="cancelled")
    # A later Telegram message does NOT silently resume; the reply says so.
    upd = _Upd()
    asyncio.run(bot._queue_instruction(upd, "and then this", _sess()))
    reply: str = upd.message.replies[-1]
    assert "Queued" in reply and "paused" in reply.lower() and "/session_resume" in reply
    _pass(db, o)
    assert db.get_task(t2)["status"] == "queued", "stop launched the next queued instruction"
    # Explicit resume (same service as the web route) releases pause AND hold.
    upd = _Upd()
    asyncio.run(bot._handle_session_resume(upd, _Ctx(["sess-1"])))
    assert "resumed" in upd.message.replies[-1].lower()
    assert _paused(db) is False and db.get_session("sess-1")["turn_queue_hold"] is None
    assert _sess().status != SessionStatus.CANCELLED
    _pass(db, o)
    assert db.get_task(t2)["status"] == "pending"
    # Not paused any more: the admission reply is the plain queued receipt.
    upd = _Upd()
    asyncio.run(bot._queue_instruction(upd, "one more thing", _sess()))
    assert "paused" not in upd.message.replies[-1].lower()


def test_TG_R2_session_scoped_cancel_pauses_like_stop(tmp_path: Any, monkeypatch: Any) -> None:
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    t1: str = str(_submit(o, operation_id="a"))
    _pass(db, o)
    _run(db, t1)
    bot = _bot(o)
    monkeypatch.setattr(o.session_store, "get_active", lambda _chat: _sess())  # no bindings file
    upd = _Upd()
    asyncio.run(bot._handle_cancel_command(upd, _Ctx([])))
    assert t1 in upd.message.replies[-1] and "/session_resume" in upd.message.replies[-1]
    assert _paused(db) is True


def test_TG_R3_stop_without_active_turn_still_pauses(tmp_path: Any, monkeypatch: Any) -> None:
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    bot = _bot(o)
    upd = _Upd()
    asyncio.run(bot._handle_session_cancel(upd, _Ctx(["sess-1"])))
    assert "No active turn" in upd.message.replies[-1]
    assert _paused(db) is True  # web parity: S6-05b


def test_TG_R4_unenrolled_sessions_keep_legacy_behaviour(tmp_path: Any, monkeypatch: Any) -> None:
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    _wire(o)
    bot = _bot(o)
    upd = _Upd()
    asyncio.run(bot._handle_session_cancel(upd, _Ctx(["sess-1"])))
    assert upd.message.replies[-1] == "No task is associated with that session yet."
    upd = _Upd()
    asyncio.run(bot._handle_session_resume(upd, _Ctx(["sess-1"])))
    assert "not on the managed turn queue" in upd.message.replies[-1]
    row = db.get_session("sess-1")
    assert not row.get("turn_queue_paused") and row.get("turn_queue_hold") is None


def test_TG_R5_resume_is_authorized_and_registered(tmp_path: Any, monkeypatch: Any) -> None:
    from src.telegram.interface import TelegramInterface

    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    db.set_turn_queue_paused("sess-1", True)
    bot = TelegramInterface("", o, allowed_users=[2])  # user 1 is not allowed
    bot.session_store = o.session_store
    upd = _Upd()
    asyncio.run(bot._handle_session_resume(upd, _Ctx(["sess-1"])))
    assert "Access denied" in upd.message.replies[-1] and _paused(db) is True
    names: List[str] = [c.command for c in TelegramInterface._bot_commands()]
    assert "session_resume" in names
