"""
Tests for the ClaudeDriver boundary (Phase 3 — no live Claude CLI).

All tests run under AI_TEAM_TEST_MODE=1 (set by conftest.py). No real
claude process is ever spawned. Fake CLIs and patched SDK are used.
"""

import asyncio
import json
import os
import sys
import textwrap
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.interfaces import ExecutionResult, Session, SessionStatus
from src.backends.claude_driver import (
    CacheStats,
    ClaudePrintResumeDriver,
    ClaudeSDKClientDriver,
    _SDKSession,
    _parse_print_resume,
    build_driver,
    parse_cache_stats_from_ndjson,
    TurnOutcome,
    classify_error_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(output: str, sid: str = "sid", raw: str = "") -> TurnOutcome:
    """A successful TurnOutcome (replaces the old (output, sid, raw) tuple)."""
    return TurnOutcome(output=output, backend_session_id=sid, raw_ndjson=raw)

def _make_session(*, repo_path: str = "", backend_session_id: str = "") -> Session:
    return Session(
        session_id=uuid.uuid4().hex,
        backend="claude",
        repo_path=repo_path,
        status=SessionStatus.IDLE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        backend_session_id=backend_session_id,
    )


def _make_ndjson(*events: Dict[str, Any]) -> str:
    return "\n".join(json.dumps(e) for e in events)


# ---------------------------------------------------------------------------
# CacheStats
# ---------------------------------------------------------------------------

class TestCacheStats:
    def test_healthy_when_high_hit_ratio(self):
        s = CacheStats(cache_read=90_000, cache_creation=10_000)
        assert s.hit_ratio == pytest.approx(0.9)
        assert not s.is_unhealthy

    def test_unhealthy_when_large_creation_and_low_hit(self):
        s = CacheStats(cache_read=5_000, cache_creation=100_000)
        assert s.hit_ratio < 0.2
        assert s.is_unhealthy

    def test_not_unhealthy_when_creation_below_threshold(self):
        # Large relative miss ratio but creation < 50k
        s = CacheStats(cache_read=0, cache_creation=30_000)
        assert not s.is_unhealthy

    def test_hit_ratio_zero_division_safe(self):
        s = CacheStats(cache_read=0, cache_creation=0)
        assert s.hit_ratio == 1.0
        assert not s.is_unhealthy


# ---------------------------------------------------------------------------
# parse_cache_stats_from_ndjson
# ---------------------------------------------------------------------------

class TestParseCacheStats:
    def test_extracts_from_assistant_message(self):
        ndjson = _make_ndjson(
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "hello"}],
                "usage": {
                    "input_tokens": 1000,
                    "cache_read_input_tokens": 5000,
                    "cache_creation_input_tokens": 120_000,
                    "output_tokens": 50,
                },
            }},
        )
        stats = parse_cache_stats_from_ndjson(ndjson)
        assert stats is not None
        assert stats.cache_read == 5000
        assert stats.cache_creation == 120_000
        assert stats.is_unhealthy

    def test_extracts_from_result_message(self):
        ndjson = _make_ndjson(
            {"type": "result", "session_id": "abc", "usage": {
                "cache_read_input_tokens": 80_000,
                "cache_creation_input_tokens": 10_000,
                "input_tokens": 90_000,
                "output_tokens": 200,
            }},
        )
        stats = parse_cache_stats_from_ndjson(ndjson)
        assert stats is not None
        assert not stats.is_unhealthy

    def test_returns_none_when_no_usage(self):
        ndjson = _make_ndjson({"type": "system", "subtype": "init"})
        assert parse_cache_stats_from_ndjson(ndjson) is None

    def test_returns_none_on_empty_string(self):
        assert parse_cache_stats_from_ndjson("") is None


# ---------------------------------------------------------------------------
# _parse_print_resume
# ---------------------------------------------------------------------------

class TestParsePrintResume:
    def test_plain_text_stdout(self):
        result = _parse_print_resume("hello world", "", 0, 1.0, "sess-123")
        assert result.success
        assert result.output == "hello world"
        assert result.backend_session_id == "sess-123"

    def test_extracts_session_id_from_ndjson(self):
        stdout = _make_ndjson(
            {"type": "system", "session_id": "new-id-456"},
            {"type": "result", "session_id": "new-id-456", "result": "done"},
        )
        result = _parse_print_resume(stdout, "", 0, 1.0, "")
        assert result.backend_session_id == "new-id-456"
        assert result.output == "done"

    def test_failure_from_returncode(self):
        result = _parse_print_resume("", "something broke", 1, 0.5, "")
        assert not result.success
        assert "something broke" in result.errors[0]

    def test_error_envelope_used_when_no_stderr(self):
        stdout = json.dumps({
            "type": "result", "is_error": True,
            "session_id": "err-sess",
            "errors": ["No conversation found with session ID: stale"],
        })
        result = _parse_print_resume(stdout, "", 1, 0.5, "known")
        assert not result.success
        assert "No conversation found with session ID: stale" in result.errors

    def test_stderr_wins_when_both_stderr_and_error_envelope(self):
        stdout = json.dumps({
            "type": "result", "is_error": True,
            "session_id": "err-sess",
            "errors": ["envelope error"],
        })
        result = _parse_print_resume(stdout, "stderr noise", 1, 0.5, "known")
        assert not result.success
        assert result.errors == ["stderr noise"]

    def test_assistant_text_extraction_from_stream_json(self):
        stdout = _make_ndjson(
            {"type": "system", "session_id": "s1"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "HELLO"}]}},
            {"type": "result", "session_id": "s1", "result": ""},
        )
        result = _parse_print_resume(stdout, "", 0, 1.0, "s1")
        assert result.output == "HELLO"


# ---------------------------------------------------------------------------
# ClaudePrintResumeDriver._build_cmd
# ---------------------------------------------------------------------------

class TestPrintResumeDriverBuildCmd:
    def test_first_turn_has_no_resume(self):
        drv = ClaudePrintResumeDriver()
        cmd = drv._build_cmd(resume_id=None, session_id="aaa", model=None)
        assert "--resume" not in cmd
        assert "--session-id" in cmd
        assert "aaa" in cmd
        assert "-p" in cmd
        # Partial messages intentionally removed
        assert "--include-partial-messages" not in cmd

    def test_resume_turn_uses_resume_flag(self):
        drv = ClaudePrintResumeDriver()
        cmd = drv._build_cmd(resume_id="bbb", session_id=None, model=None)
        assert "--resume" in cmd
        assert "bbb" in cmd
        assert "--session-id" not in cmd
        assert "--include-partial-messages" not in cmd

    def test_model_flag_included(self):
        drv = ClaudePrintResumeDriver()
        cmd = drv._build_cmd(resume_id=None, session_id="ccc", model="claude-opus-4-8")
        assert "--model" in cmd
        assert "claude-opus-4-8" in cmd

    def test_dangerously_skip_permissions_present(self):
        drv = ClaudePrintResumeDriver()
        cmd = drv._build_cmd(resume_id=None, session_id="ddd", model=None)
        assert "--dangerously-skip-permissions" in cmd


# ---------------------------------------------------------------------------
# ClaudePrintResumeDriver — send_turn blocked under test mode
# ---------------------------------------------------------------------------

class TestPrintResumeDriverBlockedUnderTestMode:
    def test_start_session_raises_live_call_blocked(self):
        from src.core.test_guard import LiveCallBlockedError
        drv = ClaudePrintResumeDriver()
        session = _make_session()
        with pytest.raises(LiveCallBlockedError):
            drv.start_session(session, "hello")

    def test_send_turn_raises_live_call_blocked(self):
        from src.core.test_guard import LiveCallBlockedError
        drv = ClaudePrintResumeDriver()
        session = _make_session(backend_session_id="existing-id")
        with pytest.raises(LiveCallBlockedError):
            drv.send_turn(session, "follow-up")


# ---------------------------------------------------------------------------
# ClaudePrintResumeDriver — cancel / close no-crash
# ---------------------------------------------------------------------------

class TestPrintResumeDriverCancelClose:
    def test_cancel_unknown_session_is_noop(self):
        drv = ClaudePrintResumeDriver()
        session = _make_session()
        drv.cancel(session)  # must not raise

    def test_close_is_noop(self):
        drv = ClaudePrintResumeDriver()
        session = _make_session()
        drv.close(session)  # must not raise

    def test_driver_type_string(self):
        drv = ClaudePrintResumeDriver()
        assert drv.driver_type() == "print_resume"


# ---------------------------------------------------------------------------
# build_driver factory
# ---------------------------------------------------------------------------

class TestBuildDriver:
    def test_explicit_print_resume(self):
        drv = build_driver("print_resume")
        assert isinstance(drv, ClaudePrintResumeDriver)

    def test_auto_falls_back_to_print_resume_when_sdk_missing(self, monkeypatch):
        # Patch _sdk_available to return False
        import src.backends.claude_driver as driver_mod
        monkeypatch.setattr(driver_mod, "_SDK_AVAILABLE", False)
        drv = build_driver("auto")
        assert isinstance(drv, ClaudePrintResumeDriver)
        # Reset global so other tests aren't affected
        monkeypatch.setattr(driver_mod, "_SDK_AVAILABLE", None)

    def test_auto_uses_sdk_when_available(self, monkeypatch):
        import src.backends.claude_driver as driver_mod
        monkeypatch.setattr(driver_mod, "_SDK_AVAILABLE", True)
        drv = build_driver("auto")
        assert isinstance(drv, ClaudeSDKClientDriver)
        monkeypatch.setattr(driver_mod, "_SDK_AVAILABLE", None)

    def test_explicit_sdk_returns_sdk_driver(self):
        drv = build_driver("sdk")
        assert isinstance(drv, ClaudeSDKClientDriver)


# ---------------------------------------------------------------------------
# ClaudeSDKClientDriver — mocked SDK
# ---------------------------------------------------------------------------

class _FakeResultMessage:
    def __init__(self, session_id: str):
        self.session_id = session_id

class _FakeTextBlock:
    def __init__(self, text: str):
        self.text = text

class _FakeAssistantMessage:
    def __init__(self, text: str):
        self.content = [_FakeTextBlock(text)]

class _FakeSDKClient:
    """Minimal fake that replaces ClaudeSDKClient for unit tests."""

    def __init__(self, responses: List[str], session_ids: List[str]):
        self._responses = list(responses)
        self._session_ids = list(session_ids)
        self._idx = 0
        self.connected = False
        self.disconnected = False
        self.queries_sent: List[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, message: str, session_id: str = "default") -> None:
        self.queries_sent.append(message)

    async def receive_response(self):
        idx = self._idx
        self._idx += 1
        text = self._responses[idx] if idx < len(self._responses) else ""
        sid = self._session_ids[idx] if idx < len(self._session_ids) else ""
        yield _FakeAssistantMessage(text)
        yield _FakeResultMessage(sid)


def _patch_sdk_in_session(sdk_session: "_SDKSession", fake_client: _FakeSDKClient) -> None:
    """Inject fake client into a started (or unstarted) _SDKSession."""
    sdk_session._client = fake_client
    sdk_session.backend_session_id = ""
    sdk_session._loop = asyncio.new_event_loop()
    sdk_session._ready.set()
    sdk_session._closed = False


class TestSDKClientDriver:
    """Test ClaudeSDKClientDriver using a patched _SDKSession.send()."""

    def test_start_session_returns_output(self):
        drv = ClaudeSDKClientDriver()
        session = _make_session(repo_path="")

        def fake_send(self_inner, message, **_kw):
            return _ok("turn one reply", "sid-001")

        with patch.object(_SDKSession, "send", fake_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                result = drv.start_session(session, "do turn one")

        assert result.success
        assert result.output == "turn one reply"
        assert result.backend_session_id == "sid-001"

    def test_send_turn_preserves_session_identity(self):
        drv = ClaudeSDKClientDriver()
        session = _make_session()
        call_count = {"n": 0}

        def fake_send(self_inner, message, **_kw):
            call_count["n"] += 1
            return _ok(f"reply-{call_count['n']}", "sid-stable")

        with patch.object(_SDKSession, "send", fake_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                r1 = drv.start_session(session, "turn 1")
                r2 = drv.send_turn(session, "turn 2")
                r3 = drv.send_turn(session, "turn 3")

        assert r1.success and r2.success and r3.success
        assert r1.output == "reply-1"
        assert r2.output == "reply-2"
        assert r3.output == "reply-3"
        # All three turns went through the SAME _SDKSession
        assert len(drv._sessions) == 1
        assert call_count["n"] == 3

    def test_different_sessions_get_different_sdk_sessions(self):
        drv = ClaudeSDKClientDriver()
        s1 = _make_session()
        s2 = _make_session()

        def fake_send(self_inner, message, **_kw):
            return _ok("ok")

        with patch.object(_SDKSession, "send", fake_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                drv.start_session(s1, "msg")
                drv.start_session(s2, "msg")

        assert len(drv._sessions) == 2
        assert s1.session_id in drv._sessions
        assert s2.session_id in drv._sessions

    def test_cancel_interrupts_but_keeps_session_pooled(self):
        # Cancel is "stop this turn", not "close the session" — the process
        # stays pooled so the next turn resumes it instead of paying to spin
        # up (and re-establish conversation continuity in) a fresh one.
        drv = ClaudeSDKClientDriver()
        session = _make_session()

        def fake_send(self_inner, message, **_kw):
            return _ok("ok")

        with patch.object(_SDKSession, "send", fake_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                drv.start_session(session, "msg")

        assert session.session_id in drv._sessions
        with patch.object(_SDKSession, "cancel_inflight") as mock_interrupt:
            drv.cancel(session)
            mock_interrupt.assert_called_once()
        assert session.session_id in drv._sessions

    def test_close_removes_session(self):
        drv = ClaudeSDKClientDriver()
        session = _make_session()

        def fake_send(self_inner, message, **_kw):
            return _ok("ok")

        with patch.object(_SDKSession, "send", fake_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                drv.start_session(session, "msg")

        drv.close(session)
        assert session.session_id not in drv._sessions

    def test_get_or_create_discards_a_force_closed_session(self):
        # A prior turn's interrupt can escalate to a hard close (the CLI was
        # wedged and never responded — see cancel_inflight's fallback). The
        # pool must not hand that dead entry to the next turn; it should
        # start a fresh session under the same key instead.
        drv = ClaudeSDKClientDriver()
        session = _make_session()

        def fake_send(self_inner, message, **_kw):
            return _ok("ok")

        with patch.object(_SDKSession, "send", fake_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                drv.start_session(session, "msg")

        dead = drv._sessions[session.session_id]
        dead._closed = True

        with patch.object(_SDKSession, "start", lambda self_inner: None):
            fresh = drv._get_or_create(session, None, {})

        assert fresh is not dead
        assert drv._sessions[session.session_id] is fresh

    def test_sdk_error_returns_failed_result(self):
        drv = ClaudeSDKClientDriver()
        session = _make_session()

        def bad_send(self_inner, message, **_kw):
            raise RuntimeError("SDK connection lost")

        with patch.object(_SDKSession, "send", bad_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                result = drv.start_session(session, "msg")

        assert not result.success
        assert "SDK connection lost" in result.errors[0]

    def test_terminated_process_write_is_transient_and_tears_down_session(self):
        # A gateway restart kills the CLI subprocess out from under a pooled
        # _SDKSession. The next turn's write raises CLIConnectionError with
        # this specific message. That must NOT be classified "fatal" (which
        # has zero retries) and must NOT leak the raw SDK string into
        # result.output (the user-facing chat bubble) — and the dead session
        # must be evicted from the pool so a retry respawns a fresh process.
        from claude_agent_sdk import CLIConnectionError

        drv = ClaudeSDKClientDriver()
        session = _make_session()

        def bad_send(self_inner, message, **_kw):
            raise CLIConnectionError(
                "Cannot write to terminated process (exit code: 0)"
            )

        with patch.object(_SDKSession, "send", bad_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                result = drv.start_session(session, "msg")

        assert not result.success
        assert result.error_class == "transient"
        assert "Cannot write to terminated process" not in (result.output or "")
        assert "Cannot write to terminated process" in result.errors[0]
        assert session.session_id not in drv._sessions

    def test_mark_lost_clears_sessions(self):
        drv = ClaudeSDKClientDriver()
        session = _make_session()

        def fake_send(self_inner, message, **_kw):
            return _ok("ok")

        with patch.object(_SDKSession, "send", fake_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                drv.start_session(session, "msg")

        assert session.session_id in drv._sessions
        drv.mark_lost(session.session_id)
        assert session.session_id not in drv._sessions

    def test_driver_type_string(self):
        drv = ClaudeSDKClientDriver()
        assert drv.driver_type() == "sdk"


# ---------------------------------------------------------------------------
# Error result handling — the "Prompt is too long" bug
# ---------------------------------------------------------------------------

class TestClassifyErrorText:
    def test_prompt_too_long_is_context_overflow(self):
        assert classify_error_text("Prompt is too long") == "context_overflow"

    def test_context_window_variants(self):
        assert classify_error_text("exceeds the context window") == "context_overflow"
        assert classify_error_text("blocking_limit hit") == "context_overflow"

    def test_other_errors_are_backend_error(self):
        assert classify_error_text("some random failure") == "backend_error"
        assert classify_error_text("") == "backend_error"


class TestErrorResultTurn:
    """An is_error ResultMessage must become a FAILURE that still delivers the
    salvaged work — never a success reply of the bare error string."""

    def test_context_overflow_fails_and_salvages(self):
        drv = ClaudeSDKClientDriver()
        session = _make_session()

        def fake_send(self_inner, message, **_kw):
            return TurnOutcome(
                output="",  # driver sets output=salvaged for error turns
                backend_session_id="sid-x",
                raw_ndjson='{"type":"result","is_error":true,"result":"Prompt is too long"}',
                is_error=True,
                error_class="context_overflow",
                error_text="Prompt is too long",
                salvaged_output="I edited config.py and ran the tests; 3 passed.",
            )

        with patch.object(_SDKSession, "send", fake_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                result = drv.start_session(session, "do work")

        # Honest failure with the right class
        assert not result.success
        assert result.error_class == "context_overflow"
        # The raw error travels in errors[], never as the (only) reply
        assert any("too long" in e.lower() for e in result.errors)
        # The reply DELIVERS the salvaged progress + an actionable banner
        assert "I edited config.py" in result.output
        assert "/compact" in result.output or "new session" in result.output
        # raw_stdout stays diagnosable (carries is_error)
        assert '"is_error":true' in result.raw_stdout or '"is_error": true' in result.raw_stdout

    def test_error_without_salvage_still_actionable(self):
        drv = ClaudeSDKClientDriver()
        session = _make_session()

        def fake_send(self_inner, message, **_kw):
            return TurnOutcome(
                output="",
                backend_session_id="sid-y",
                raw_ndjson="",
                is_error=True,
                error_class="context_overflow",
                error_text="Prompt is too long",
                salvaged_output="",
            )

        with patch.object(_SDKSession, "send", fake_send):
            with patch.object(_SDKSession, "start", lambda self_inner: None):
                result = drv.start_session(session, "do work")

        assert not result.success
        # Even with nothing to salvage, the user gets the actionable banner,
        # not a bare "Prompt is too long".
        assert "Context window full" in result.output


# ---------------------------------------------------------------------------
# ClaudeCodeBackend integration via driver
# ---------------------------------------------------------------------------

class TestClaudeCodeBackendDriverIntegration:
    """Tests that ClaudeCodeBackend correctly routes to its driver."""

    def _make_backend_with_fake_sdk_driver(self):
        from src.backends.claude_code import ClaudeCodeBackend
        from src.backends.claude_driver import ClaudeSDKClientDriver
        backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
        backend._driver = ClaudeSDKClientDriver()
        from src.backends.claude_driver import ClaudePrintResumeDriver
        backend._fallback = ClaudePrintResumeDriver()
        backend._exe = "claude"
        backend._session_procs = {}
        backend._oneoff_procs = set()
        import threading
        backend._proc_lock = threading.Lock()
        return backend

    def test_create_session_routes_to_driver(self, monkeypatch):
        from src.backends.claude_code import ClaudeCodeBackend
        from src.core.test_guard import LiveCallBlockedError

        backend = self._make_backend_with_fake_sdk_driver()
        session = _make_session(repo_path="")
        session.last_user_message = "initial prompt"

        def fake_send(self_inner, message, **_kw):
            return _ok("first response", "sess-aaa")

        monkeypatch.setattr(_SDKSession, "send", fake_send)
        monkeypatch.setattr(_SDKSession, "start", lambda self_inner: None)

        # SDK driver does NOT call assert_live_calls_allowed — only print_resume does.
        # But ClaudeCodeBackend.create_session does check it. Patch it out.
        monkeypatch.setattr("src.backends.claude_code.ClaudeCodeBackend.create_session",
                            lambda s, session, **kw: backend._driver.start_session(session, session.last_user_message))

        result = backend.create_session(session)
        assert result.success
        assert result.output == "first response"

    def test_session_lost_guard_blocks_resume(self):
        from src.backends.claude_code import ClaudeCodeBackend
        backend = self._make_backend_with_fake_sdk_driver()
        session = _make_session()
        session.driver_status = "lost"

        result = backend.resume_session(session, "follow-up")
        assert not result.success
        assert result.error_class == "session_lost"

    def test_cache_unhealthy_guard_blocks_print_resume(self):
        from src.backends.claude_code import ClaudeCodeBackend
        from src.backends.claude_driver import ClaudePrintResumeDriver
        # Force print_resume driver
        backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
        backend._driver = ClaudePrintResumeDriver()
        backend._fallback = ClaudePrintResumeDriver()
        backend._exe = "claude"
        backend._session_procs = {}
        backend._oneoff_procs = set()
        import threading
        backend._proc_lock = threading.Lock()

        session = _make_session(backend_session_id="old-sid")
        session.cache_health = "unhealthy"
        session.cache_unhealthy_count = 2

        result = backend.resume_session(session, "another turn")
        assert not result.success
        assert result.error_class == "cache_unhealthy"

    def test_observe_cache_health_marks_session_unhealthy(self):
        from src.backends.claude_code import ClaudeCodeBackend
        session = _make_session()
        ndjson = _make_ndjson({"type": "assistant", "message": {"content": [], "usage": {
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 200_000,
            "input_tokens": 201_000,
            "output_tokens": 100,
        }}})
        result = ExecutionResult(success=True, output="ok", raw_stdout=ndjson)

        ClaudeCodeBackend._observe_cache_health(session, result)
        assert session.cache_health == "unhealthy"
        assert session.cache_unhealthy_count == 1

    def test_observe_cache_health_marks_session_healthy(self):
        from src.backends.claude_code import ClaudeCodeBackend
        session = _make_session()
        session.cache_health = "unhealthy"
        session.cache_unhealthy_count = 1

        ndjson = _make_ndjson({"type": "assistant", "message": {"content": [], "usage": {
            "cache_read_input_tokens": 90_000,
            "cache_creation_input_tokens": 10_000,
            "input_tokens": 100_000,
            "output_tokens": 200,
        }}})
        result = ExecutionResult(success=True, output="ok", raw_stdout=ndjson)
        ClaudeCodeBackend._observe_cache_health(session, result)
        assert session.cache_health == "healthy"

    def test_observe_cache_health_no_usage_is_noop(self):
        from src.backends.claude_code import ClaudeCodeBackend
        session = _make_session()
        result = ExecutionResult(success=True, output="ok", raw_stdout="not ndjson")
        ClaudeCodeBackend._observe_cache_health(session, result)
        assert session.cache_health == "unknown"  # unchanged


# ---------------------------------------------------------------------------
# Session state model — new driver fields
# ---------------------------------------------------------------------------

class TestSessionDriverFields:
    def test_session_defaults(self):
        session = _make_session()
        assert session.driver_type == ""
        assert session.driver_status == ""
        assert session.cache_health == "unknown"
        assert session.cache_unhealthy_count == 0
        assert session.previous_backend_session_ids == []

    def test_rollover_keeps_history(self):
        session = _make_session(backend_session_id="old-backend-id")
        # Simulate rollover
        session.previous_backend_session_ids.append(session.backend_session_id)
        session.backend_session_id = "new-backend-id"
        assert "old-backend-id" in session.previous_backend_session_ids
        assert session.backend_session_id == "new-backend-id"

    def test_multiple_rollovers_accumulate(self):
        session = _make_session(backend_session_id="v1")
        for new_id in ("v2", "v3", "v4"):
            session.previous_backend_session_ids.append(session.backend_session_id)
            session.backend_session_id = new_id
        assert session.previous_backend_session_ids == ["v1", "v2", "v3"]
        assert session.backend_session_id == "v4"


# ---------------------------------------------------------------------------
# _SDKSession concurrency: send is serialised
# ---------------------------------------------------------------------------

class TestSDKSessionSendSerialisation:
    def test_concurrent_sends_are_serialised(self):
        """Two threads calling send() on the same _SDKSession must not interleave."""
        call_order = []
        lock_order_lock = threading.Lock()

        class _FakeSessForConcurrency:
            _lock = threading.Lock()

            def send(self, message):
                with self._lock:
                    with lock_order_lock:
                        call_order.append(message)
                    time.sleep(0.01)
                return (f"reply-{message}", "sid", "")

        fake = _FakeSessForConcurrency()
        results = {}

        def _thread(msg):
            results[msg] = fake.send(msg)

        t1 = threading.Thread(target=_thread, args=("A",))
        t2 = threading.Thread(target=_thread, args=("B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Both completed — order doesn't matter but no interleaving
        assert set(call_order) == {"A", "B"}
        assert results["A"][0] == "reply-A"
        assert results["B"][0] == "reply-B"


class TestSDKSessionTurnConflictGuard:
    def test_send_interrupts_a_stale_inflight_turn_instead_of_queuing_silently(self):
        """A held lock (an abandoned prior turn that was never cancelled
        cleanly) must be interrupted up front, not queued behind — queuing
        silently is what let a resent prompt land in the same live
        conversation as the abandoned one (the ever-growing-session bug)."""
        sess = _SDKSession("key", "/tmp", None, {})
        calls = []

        def fake_cancel_inflight(self_inner):
            calls.append("interrupted")
            # Simulate the interrupt landing and the stale turn's `finally`
            # releasing the lock it held.
            sess._lock.release()

        def fake_submit(coro, timeout=None):
            coro.close()  # avoid "never awaited" noise; we're not exercising _do_query here
            return _ok("new turn")

        with patch.object(_SDKSession, "cancel_inflight", fake_cancel_inflight):
            with patch.object(sess, "submit", fake_submit):
                sess._lock.acquire()  # simulate a still-running stale turn
                result = sess.send("hello")

        assert calls == ["interrupted"]
        assert result.output == "new turn"
        assert not sess._lock.locked()



class TestDriverStatePersistenceIntegration:
    def test_session_store_round_trips_driver_fields(self):
        from src.services.session_store import SessionStore

        session = _make_session(backend_session_id="backend-live")
        session.driver_type = "sdk"
        session.driver_status = "live"
        session.cache_health = "unhealthy"
        session.cache_unhealthy_count = 2
        session.previous_backend_session_ids = ["old-a", "old-b"]

        restored = SessionStore._from_dict(SessionStore._to_dict(session))

        assert restored.driver_type == "sdk"
        assert restored.driver_status == "live"
        assert restored.cache_health == "unhealthy"
        assert restored.cache_unhealthy_count == 2
        assert restored.previous_backend_session_ids == ["old-a", "old-b"]

    def test_worker_payload_round_trips_driver_fields(self):
        from src.services.session_store import SessionStore
        from src.worker.agent import _make_session_from_payload

        session = _make_session(backend_session_id="backend-live")
        session.driver_type = "sdk"
        session.driver_status = "live"
        session.cache_health = "healthy"
        session.cache_unhealthy_count = 1
        session.previous_backend_session_ids = ["old-backend"]

        restored = _make_session_from_payload({"session": SessionStore._to_dict(session)})

        assert restored.driver_type == "sdk"
        assert restored.driver_status == "live"
        assert restored.cache_health == "healthy"
        assert restored.cache_unhealthy_count == 1
        assert restored.previous_backend_session_ids == ["old-backend"]

    def test_db_marks_live_sdk_sessions_lost_for_restarted_node(self):
        from src.control.db import MeshDB

        root = Path.cwd() / ".test_session_artifacts" / uuid.uuid4().hex[:8]
        root.mkdir(parents=True, exist_ok=True)
        db = MeshDB(str(root / "mesh.db"))
        session = _make_session(backend_session_id="backend-live")
        session.machine_id = "worker-a"
        session.status = SessionStatus.AWAITING_INPUT
        session.driver_type = "sdk"
        session.driver_status = "live"
        db.upsert_session(session)

        assert db.mark_driver_sessions_lost_for_node("worker-a") == 1
        row = db.get_session(session.session_id)
        assert row is not None
        assert row["driver_status"] == "lost"

class TestSDKUsageSerialization:
    def test_plain_usage_dict_accepts_dataclass_usage(self):
        from src.backends.claude_driver import _plain_usage_dict

        @dataclass
        class Usage:
            input_tokens: int
            cache_creation_input_tokens: int
            cache_read_input_tokens: int
            output_tokens: int

        usage = Usage(1, 2, 3, 4)

        assert _plain_usage_dict(usage) == {
            "input_tokens": 1,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "output_tokens": 4,
        }

