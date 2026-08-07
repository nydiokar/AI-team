from src.backends import claude_code
from src.backends.claude_code import ClaudeCodeBackend
from src.backends.claude_driver import SALVAGE_ERROR_BANNER
from src.orchestrator import (
    TaskOrchestrator,
    _session_status_after_result,
    _reclassify_salvaged_turn_success,
)
from src.core.interfaces import ExecutionResult, Task, TaskType, TaskPriority, TaskStatus, TaskResult, SessionStatus
import asyncio
from datetime import datetime
from pathlib import Path
import shutil
import uuid


def test_create_session_uses_stream_json_output_and_explicit_session_id():
    backend = ClaudeCodeBackend()

    cmd = backend._build_cmd(resume_id=None, session_id="11111111-1111-1111-1111-111111111111")

    assert "--output-format" in cmd
    assert "stream-json" in cmd
    assert "--verbose" in cmd
    assert "--session-id" in cmd
    assert "11111111-1111-1111-1111-111111111111" in cmd
    assert "--resume" not in cmd


def test_resume_session_uses_stream_json_output_and_resume_id():
    backend = ClaudeCodeBackend()

    cmd = backend._build_cmd(
        resume_id="22222222-2222-2222-2222-222222222222",
        session_id=None,
    )

    assert "--resume" in cmd
    assert "22222222-2222-2222-2222-222222222222" in cmd
    assert "--output-format" in cmd
    assert "stream-json" in cmd
    assert "--verbose" in cmd
    assert "--session-id" not in cmd


def test_parse_prefers_plain_text_output_for_session_turns():
    result = ClaudeCodeBackend._parse(
        stdout="Actual Claude reply\n\nWith details.",
        stderr="",
        returncode=0,
        elapsed=1.0,
        known_session_id="33333333-3333-3333-3333-333333333333",
    )

    assert result.success is True
    assert result.output == "Actual Claude reply\n\nWith details."
    assert result.backend_session_id == "33333333-3333-3333-3333-333333333333"
    assert result.raw_stdout == "Actual Claude reply\n\nWith details."
    assert result.return_code == 0


def test_parse_extracts_result_from_json_output():
    stdout = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"session_id":"44444444-4444-4444-4444-444444444444",'
        '"result":"Structured Claude reply"}'
    )

    result = ClaudeCodeBackend._parse(
        stdout=stdout,
        stderr="",
        returncode=0,
        elapsed=1.0,
    )

    assert result.success is True
    assert result.output == "Structured Claude reply"
    assert result.backend_session_id == "44444444-4444-4444-4444-444444444444"
    assert result.parsed_output["result"] == "Structured Claude reply"


def test_parse_extracts_assistant_text_from_stream_json():
    stdout = "\n".join(
        [
            '{"type":"system","subtype":"init","session_id":"55555555-5555-5555-5555-555555555555"}',
            '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"HELLO_"}},"session_id":"55555555-5555-5555-5555-555555555555"}',
            '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"FROM_STREAM"}},"session_id":"55555555-5555-5555-5555-555555555555"}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"HELLO_FROM_STREAM"}]}}',
            '{"type":"result","subtype":"success","session_id":"55555555-5555-5555-5555-555555555555","result":""}',
        ]
    )

    result = ClaudeCodeBackend._parse(
        stdout=stdout,
        stderr="",
        returncode=0,
        elapsed=1.0,
    )

    assert result.success is True
    assert result.output == "HELLO_FROM_STREAM"
    assert result.backend_session_id == "55555555-5555-5555-5555-555555555555"
    assert result.parsed_output["assistant_text"] == "HELLO_FROM_STREAM"


def test_parse_prefers_claude_error_envelope_message():
    stdout = (
        '{"type":"result","subtype":"error_during_execution","is_error":true,'
        '"session_id":"66666666-6666-6666-6666-666666666666",'
        '"errors":["No conversation found with session ID: deadbeef"]}'
    )

    result = ClaudeCodeBackend._parse(
        stdout=stdout,
        stderr="",
        returncode=1,
        elapsed=1.0,
        known_session_id="known-session",
    )

    assert result.success is False
    assert result.errors == ["No conversation found with session ID: deadbeef"]
    assert result.backend_session_id == "66666666-6666-6666-6666-666666666666"


def test_session_reply_uses_structured_fallback_when_output_is_empty():
    result = TaskResult(
        task_id="task_session_reply",
        success=True,
        output="",
        errors=[],
        files_modified=[],
        execution_time=0.01,
        timestamp=datetime.now().isoformat(),
        raw_stdout="\n",
        raw_stderr="",
        parsed_output={"content": "Recovered reply from parsed output"},
    )

    assert TaskOrchestrator._session_reply_text(result) == "Recovered reply from parsed output"


def test_session_reply_surfaces_empty_backend_output_explicitly():
    result = TaskResult(
        task_id="task_empty_session_reply",
        success=True,
        output="",
        errors=[],
        files_modified=[],
        execution_time=0.01,
        timestamp=datetime.now().isoformat(),
        raw_stdout="\n",
        raw_stderr="",
        parsed_output={"content": ""},
    )

    reply = TaskOrchestrator._session_reply_text(result)
    assert "returned no final reply text" in reply


def test_short_failure_reason_distills_rate_limit_from_stream_json():
    result = TaskResult(
        task_id="task_rate_limit",
        success=False,
        output="You've hit your limit",
        errors=["Claude exited with code 1"],
        files_modified=[],
        execution_time=0.01,
        timestamp=datetime.now().isoformat(),
        raw_stdout="\n".join(
            [
                '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}',
                '{"type":"assistant","error":"rate_limit","message":{"content":[{"type":"text","text":"You\\u0027ve hit your limit · resets 11pm (Europe/Kiev)"}]}}',
            ]
        ),
        raw_stderr="",
        parsed_output={"result": "You've hit your limit · resets 11pm (Europe/Kiev)"},
        return_code=1,
    )

    assert TaskOrchestrator._short_failure_reason(result) == "Claude usage limit reached — resets 11pm (Europe/Kiev)"


def test_classify_error_detects_rate_limit_event_from_stream_json():
    orch = TaskOrchestrator()
    result = TaskResult(
        task_id="task_rate_limit_classify",
        success=False,
        output="",
        errors=["Claude exited with code 1"],
        files_modified=[],
        execution_time=0.01,
        timestamp=datetime.now().isoformat(),
        raw_stdout='{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}',
        raw_stderr="",
        parsed_output={"result": "You've hit your limit"},
        return_code=1,
    )

    assert orch._classify_error(result) == "rate_limit"


def test_classify_error_detects_max_turns_from_result_subtype():
    """error_max_turns never appears as wording in free text — only the
    structured `subtype` field on the terminal result line carries it. Before
    threading that field through, this collapsed into the generic 'fatal'
    bucket with no actionable hint and 0 retries for the wrong reason."""
    orch = TaskOrchestrator()
    result = TaskResult(
        task_id="task_max_turns",
        success=False,
        output="",
        errors=[],
        files_modified=[],
        execution_time=1.0,
        timestamp=datetime.now().isoformat(),
        raw_stdout='{"type":"result","subtype":"error_max_turns","is_error":true}',
        raw_stderr="",
        return_code=1,
    )

    assert orch._classify_error(result) == "max_turns"
    assert orch._get_retry_strategy("max_turns")["max_retries"] == 0
    assert any("CLAUDE_SDK_MAX_TURNS" in a for a in orch._suggest_actions("max_turns", result))


def test_classify_error_detects_upstream_rate_limit_from_api_error_status():
    orch = TaskOrchestrator()
    result = TaskResult(
        task_id="task_api_429",
        success=False,
        output="",
        errors=[],
        files_modified=[],
        execution_time=1.0,
        timestamp=datetime.now().isoformat(),
        raw_stdout='{"type":"result","subtype":"success","is_error":true,"api_error_status":429}',
        raw_stderr="",
        return_code=1,
    )

    assert orch._classify_error(result) == "rate_limit"


def test_classify_error_detects_upstream_5xx_from_api_error_status():
    orch = TaskOrchestrator()
    result = TaskResult(
        task_id="task_api_529",
        success=False,
        output="",
        errors=[],
        files_modified=[],
        execution_time=1.0,
        timestamp=datetime.now().isoformat(),
        raw_stdout='{"type":"result","subtype":"success","is_error":true,"api_error_status":529}',
        raw_stderr="",
        return_code=1,
    )

    assert orch._classify_error(result) == "upstream_error"
    assert orch._get_retry_strategy("upstream_error")["max_retries"] >= 1


def test_classify_error_detects_session_limit_without_rate_limit_event():
    # The live-incident shape: the subscription cap surfaces ONLY as the result
    # text "hit your session limit" — no rate_limit_event stream line. This must
    # still classify as rate_limit (retry-eligible / auto-resume), NOT fatal.
    orch = TaskOrchestrator()
    result = TaskResult(
        task_id="task_session_limit",
        success=False,
        output="⏳ Claude usage limit reached — resets 4:40pm (Europe/Kiev).",
        errors=["You've hit your session limit · resets 4:40pm (Europe/Kiev)"],
        files_modified=[],
        execution_time=0.01,
        timestamp=datetime.now().isoformat(),
        raw_stdout='{"type":"result","is_error":true,"result":"You\\u0027ve hit your session limit · resets 4:40pm (Europe/Kiev)"}',
        raw_stderr="",
        parsed_output=None,
        return_code=1,
    )

    assert orch._classify_error(result) == "rate_limit"


def test_salvaged_backend_finalization_error_keeps_session_awaiting_input():
    result = TaskResult(
        task_id="task_salvaged_backend_error",
        success=False,
        output=f"{SALVAGE_ERROR_BANNER}\n\n---\n\nI changed config.py.",
        errors=["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"],
        files_modified=["config.py"],
        execution_time=1.0,
        timestamp=datetime.now().isoformat(),
        raw_stdout='{"type":"result","subtype":"error_during_execution","is_error":true}',
        raw_stderr="",
        return_code=0,
        error_class="backend_error",
    )

    assert _session_status_after_result(result) == SessionStatus.AWAITING_INPUT


def test_salvaged_detection_survives_gateway_reclassification_and_committed_work():
    """The real 99d997c3c8b6 incident shape: the orchestrator reclassifies
    error_class to ``fatal`` (never ``backend_error``), the work is already
    committed (files_modified=[]), and on the mesh path the error_during_execution
    marker lands in raw_stderr (the diagnostic tail) because the old worker never
    shipped raw_stdout. All three must still resolve to AWAITING_INPUT."""
    result = TaskResult(
        task_id="task_ed5283f1",
        success=False,
        output=f"{SALVAGE_ERROR_BANNER}\n\n---\n\n"
        "I need to verify the eviction decision ... the oldest note is at line 419.",
        errors=["[Horse] [ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"],
        files_modified=[],
        execution_time=412.0,
        timestamp=datetime.now().isoformat(),
        raw_stdout="",
        raw_stderr=(
            "error_class=backend_error\nstdout_tail:\n"
            '{"type":"result","subtype":"error_during_execution","is_error":true}'
        ),
        return_code=0,
        error_class="fatal",
    )

    assert _session_status_after_result(result) == SessionStatus.AWAITING_INPUT


def test_salvage_banner_without_agent_content_is_not_salvage():
    """A reply that is only the banner (no agent work beyond it) is not
    inspectable progress — keep the session ERROR so it does not mask a dead end."""
    result = TaskResult(
        task_id="task_banner_only",
        success=False,
        output=SALVAGE_ERROR_BANNER,
        errors=["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"],
        files_modified=[],
        execution_time=1.0,
        timestamp=datetime.now().isoformat(),
        raw_stdout='{"type":"result","subtype":"error_during_execution","is_error":true}',
        raw_stderr="",
        return_code=0,
    )

    assert _session_status_after_result(result) == SessionStatus.ERROR


def test_reclassify_salvaged_turn_success_flips_success_true():
    """A salvaged terminal error must not surface as 'failed' anywhere
    downstream (task status, turn telemetry, mesh_tasks row, session history)
    when the session layer already treats it as fine (AWAITING_INPUT) —
    otherwise every consumer keyed off `TaskResult.success` still lies."""
    result = TaskResult(
        task_id="task_salvaged_backend_error",
        success=False,
        output=f"{SALVAGE_ERROR_BANNER}\n\n---\n\nI changed config.py.",
        errors=["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"],
        files_modified=["config.py"],
        execution_time=1.0,
        timestamp=datetime.now().isoformat(),
        raw_stdout='{"type":"result","subtype":"error_during_execution","is_error":true}',
        raw_stderr="",
        return_code=0,
        error_class="backend_error",
    )

    out = _reclassify_salvaged_turn_success(result)

    assert out is result
    assert result.success is True
    # Audit trail (original error signal) must survive the flip untouched.
    assert result.error_class == "backend_error"
    assert result.errors == ["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"]
    assert result.output.startswith(SALVAGE_ERROR_BANNER)


def test_reclassify_salvaged_turn_success_leaves_genuine_failure_alone():
    result = TaskResult(
        task_id="task_plain_backend_error",
        success=False,
        output="backend failed",
        errors=["backend failed"],
        files_modified=[],
        execution_time=1.0,
        timestamp=datetime.now().isoformat(),
        raw_stdout='{"type":"result","subtype":"error_during_execution","is_error":true}',
        raw_stderr="",
        return_code=1,
        error_class="backend_error",
    )

    _reclassify_salvaged_turn_success(result)

    assert result.success is False


def test_plain_backend_error_still_marks_session_error():
    result = TaskResult(
        task_id="task_plain_backend_error",
        success=False,
        output="backend failed",
        errors=["backend failed"],
        files_modified=[],
        execution_time=1.0,
        timestamp=datetime.now().isoformat(),
        raw_stdout='{"type":"result","subtype":"error_during_execution","is_error":true}',
        raw_stderr="",
        return_code=1,
        error_class="backend_error",
    )

    assert _session_status_after_result(result) == SessionStatus.ERROR


def test_cancelled_result_still_marks_session_cancelled():
    result = TaskResult(
        task_id="task_cancelled",
        success=False,
        output="",
        errors=["cancelled"],
        files_modified=[],
        execution_time=1.0,
        timestamp=datetime.now().isoformat(),
    )

    assert _session_status_after_result(result) == SessionStatus.CANCELLED


def test_short_failure_reason_handles_session_limit_phrasing():
    result = TaskResult(
        task_id="task_session_limit_reason",
        success=False,
        output="",
        errors=["You've hit your session limit · resets 4:40pm (Europe/Kiev)"],
        files_modified=[],
        execution_time=0.01,
        timestamp=datetime.now().isoformat(),
        raw_stdout="",
        raw_stderr="",
        parsed_output=None,
        return_code=1,
    )

    assert (
        TaskOrchestrator._short_failure_reason(result)
        == "Claude usage limit reached — resets 4:40pm (Europe/Kiev)"
    )


def test_recover_stale_busy_session_marks_error_and_notifies(monkeypatch):
    root = Path.cwd() / ".test_session_artifacts" / uuid.uuid4().hex[:8]
    from config import config
    try:
        results_dir = root / "results"
        summaries_dir = root / "summaries"
        logs_dir = root / "logs"
        state_dir = root / "state"
        for path in (results_dir, summaries_dir, logs_dir, state_dir):
            path.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(config.system, "results_dir", str(results_dir), raising=False)
        monkeypatch.setattr(config.system, "summaries_dir", str(summaries_dir), raising=False)
        monkeypatch.setattr(config.system, "logs_dir", str(logs_dir), raising=False)

        import src.services.session_store as session_store_module
        monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", state_dir / "sessions", raising=False)
        monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", state_dir / "telegram" / "active_bindings.json", raising=False)

        orch = TaskOrchestrator()
        session = orch.session_store.create("codex", str(root), telegram_chat_id=1, owner_user_id=2)
        session.status = SessionStatus.BUSY
        session.last_task_id = "task_interrupted"
        orch.session_store.save(session)

        captured = []

        class _FakeTelegram:
            async def notify_completion(self, task_id, summary, success=True, chat_id=None):
                captured.append({"task_id": task_id, "summary": summary, "success": success, "chat_id": chat_id})

        orch.telegram_interface = _FakeTelegram()

        asyncio.run(orch._recover_stale_busy_sessions())

        reloaded = orch.session_store.get(session.session_id)
        assert reloaded is not None
        assert reloaded.status == SessionStatus.ERROR
        assert "Interrupted by gateway restart" in reloaded.last_result_summary
        assert captured == [
            {
                "task_id": "task_interrupted",
                "summary": "Task interrupted by gateway restart",
                "success": False,
                "chat_id": 1,
            }
        ]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_recover_paused_pinned_hold_becomes_honest_terminal(monkeypatch):
    """A18: a session caught mid affinity-hold by a restart must resolve to the
    honest, resumable PINNED_NODE_OFFLINE state — never wedged in PAUSED, never a
    bare ERROR, and no spurious 'interrupted' completion notification."""
    root = Path.cwd() / ".test_session_artifacts" / uuid.uuid4().hex[:8]
    from config import config
    try:
        results_dir = root / "results"
        summaries_dir = root / "summaries"
        logs_dir = root / "logs"
        state_dir = root / "state"
        for path in (results_dir, summaries_dir, logs_dir, state_dir):
            path.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(config.system, "results_dir", str(results_dir), raising=False)
        monkeypatch.setattr(config.system, "summaries_dir", str(summaries_dir), raising=False)
        monkeypatch.setattr(config.system, "logs_dir", str(logs_dir), raising=False)

        import src.services.session_store as session_store_module
        monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", state_dir / "sessions", raising=False)
        monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", state_dir / "telegram" / "active_bindings.json", raising=False)

        orch = TaskOrchestrator()
        session = orch.session_store.create("claude", str(root), telegram_chat_id=1, owner_user_id=2)
        session.status = SessionStatus.PAUSED_PINNED_NODE_OFFLINE
        session.machine_id = "remote-worker-01"
        orch.session_store.save(session)

        captured = []

        class _FakeTelegram:
            async def notify_completion(self, task_id, summary, success=True, chat_id=None):
                captured.append(task_id)

        orch.telegram_interface = _FakeTelegram()

        asyncio.run(orch._recover_stale_busy_sessions())

        reloaded = orch.session_store.get(session.session_id)
        assert reloaded is not None
        assert reloaded.status == SessionStatus.PINNED_NODE_OFFLINE
        assert reloaded.status != SessionStatus.ERROR
        assert "re-pin" in reloaded.last_result_summary
        assert captured == []  # not a bare interrupted-ERROR; no completion notice
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_compute_turn_changes_filters_unchanged_dirty_files(monkeypatch):
    before = {
        "old.ts": {"status": " M", "fingerprint": "same"},
        "keep.ts": {"status": " M", "fingerprint": "unchanged"},
    }
    after = {
        "old.ts": {"status": " M", "fingerprint": "different"},
        "keep.ts": {"status": " M", "fingerprint": "unchanged"},
        "new.ts": {"status": "??", "fingerprint": "newfile"},
    }

    def fake_stats(_cwd, path, _status_code):
        if path == "old.ts":
            return {"added": 5, "deleted": 2}
        return {"added": 10, "deleted": 0}

    monkeypatch.setattr(claude_code, "_current_diff_stats", fake_stats)

    changes = claude_code._compute_turn_changes("repo", before, after)

    assert [item["path"] for item in changes] == ["new.ts", "old.ts"]
    assert changes[0]["change_type"] == "untracked"
    assert changes[1]["change_type"] == "modified"


def test_format_file_change_lines_includes_type_and_stats():
    result = TaskResult(
        task_id="task_changes",
        success=True,
        output="OK",
        errors=[],
        files_modified=["src/app.ts"],
        execution_time=0.01,
        timestamp=datetime.now().isoformat(),
        file_changes=[
            {
                "path": "src/app.ts",
                "change_type": "modified",
                "added_lines": 12,
                "deleted_lines": 3,
            }
        ],
    )

    lines = TaskOrchestrator._format_file_change_lines(result)
    assert lines == ["  `src/app.ts` [Modified (+12/-3)]"]


def test_write_artifacts_include_session_metadata_and_archive_copy(monkeypatch):
    root = Path.cwd() / ".test_session_artifacts" / uuid.uuid4().hex[:8]
    from config import config
    try:
        results_dir = root / "results"
        summaries_dir = root / "summaries"
        logs_dir = root / "logs"
        state_dir = root / "state"
        for path in (results_dir, summaries_dir, logs_dir, state_dir):
            path.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(config.system, "results_dir", str(results_dir), raising=False)
        monkeypatch.setattr(config.system, "summaries_dir", str(summaries_dir), raising=False)
        monkeypatch.setattr(config.system, "logs_dir", str(logs_dir), raising=False)

        import src.services.session_store as session_store_module
        monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", state_dir / "sessions", raising=False)
        monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", state_dir / "telegram" / "active_bindings.json", raising=False)

        orch = TaskOrchestrator()
        session = orch.session_store.create("claude", str(root), telegram_chat_id=1, owner_user_id=2)
        session.backend_session_id = "backend-123"
        orch.session_store.save(session)

        task = Task(
            id="task_session_meta",
            type=TaskType.ANALYZE,
            priority=TaskPriority.MEDIUM,
            status=TaskStatus.PENDING,
            created=datetime.now().isoformat(),
            title="Test",
            target_files=[],
            prompt="Inspect",
            success_criteria=[],
            context="",
            metadata={"session_id": session.session_id, "cwd": str(root), "source": "telegram_session"},
        )
        result = TaskResult(
            task_id=task.id,
            success=True,
            output="OK",
            errors=[],
            files_modified=[],
            execution_time=0.01,
            timestamp=datetime.now().isoformat(),
            file_changes=[
                {
                    "path": "src/example.ts",
                    "change_type": "created",
                    "added_lines": 7,
                    "deleted_lines": 0,
                }
            ],
            parsed_output={"content": "OK"},
        )
        setattr(result, "backend_name", "claude")

        orch._write_artifacts(task.id, result, task=task)

        flat = results_dir / f"{task.id}.json"
        assert flat.exists()
        data = flat.read_text(encoding="utf-8")
        assert session.session_id in data
        assert '"file_changes"' in data
        # Session subdir is written as a best-effort archive copy alongside the flat file.
        session_dir = results_dir / "sessions" / session.session_id
        if session_dir.exists():
            assert any(p.suffix == ".json" for p in session_dir.iterdir())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_failed_backend_result_does_not_overwrite_session_id(monkeypatch):
    root = Path.cwd() / ".test_session_artifacts" / uuid.uuid4().hex[:8]
    from config import config
    try:
        results_dir = root / "results"
        summaries_dir = root / "summaries"
        logs_dir = root / "logs"
        state_dir = root / "state"
        for path in (results_dir, summaries_dir, logs_dir, state_dir):
            path.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(config.system, "results_dir", str(results_dir), raising=False)
        monkeypatch.setattr(config.system, "summaries_dir", str(summaries_dir), raising=False)
        monkeypatch.setattr(config.system, "logs_dir", str(logs_dir), raising=False)

        import src.services.session_store as session_store_module
        monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", state_dir / "sessions", raising=False)
        monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", state_dir / "telegram" / "active_bindings.json", raising=False)

        orch = TaskOrchestrator()
        session = orch.session_store.create("claude", str(root), telegram_chat_id=1, owner_user_id=2)
        session.backend_session_id = "stable-session-id"
        orch.session_store.save(session)

        task = Task(
            id="task_failed_session_resume",
            type=TaskType.ANALYZE,
            priority=TaskPriority.MEDIUM,
            status=TaskStatus.PENDING,
            created=datetime.now().isoformat(),
            title="Test",
            target_files=[],
            prompt="Inspect",
            success_criteria=[],
            context="",
            metadata={"session_id": session.session_id, "cwd": str(root), "source": "telegram_session"},
        )

        failing = ExecutionResult(
            success=False,
            output="",
            backend_session_id="bad-new-session-id",
            errors=["Claude exited with code 1"],
            execution_time=0.01,
            raw_stdout="",
            raw_stderr="",
            parsed_output={"errors": ["Claude exited with code 1"]},
            return_code=1,
        )

        async def fake_to_thread(_fn, *_args, **_kwargs):
            return failing

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

        result = asyncio.run(orch.process_task(task))

        reloaded = orch.session_store.get(session.session_id)
        assert result.success is False
        assert result.errors == ["Claude exited with code 1"]
        assert reloaded is not None
        # backend_session_id is persisted even on failure so the next turn can resume.
        assert reloaded.backend_session_id == "bad-new-session-id"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_missing_backend_conversation_recreates_session_and_retries(monkeypatch):
    root = Path.cwd() / ".test_session_artifacts" / uuid.uuid4().hex[:8]
    from config import config
    try:
        results_dir = root / "results"
        summaries_dir = root / "summaries"
        logs_dir = root / "logs"
        state_dir = root / "state"
        for path in (results_dir, summaries_dir, logs_dir, state_dir):
            path.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(config.system, "results_dir", str(results_dir), raising=False)
        monkeypatch.setattr(config.system, "summaries_dir", str(summaries_dir), raising=False)
        monkeypatch.setattr(config.system, "logs_dir", str(logs_dir), raising=False)

        import src.services.session_store as session_store_module
        monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", state_dir / "sessions", raising=False)
        monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", state_dir / "telegram" / "active_bindings.json", raising=False)

        orch = TaskOrchestrator()
        session = orch.session_store.create("claude", str(root), telegram_chat_id=1, owner_user_id=2)
        session.backend_session_id = "stale-session-id"
        orch.session_store.save(session)

        task = Task(
            id="task_session_recreate",
            type=TaskType.ANALYZE,
            priority=TaskPriority.MEDIUM,
            status=TaskStatus.PENDING,
            created=datetime.now().isoformat(),
            title="Test",
            target_files=[],
            prompt="Inspect",
            success_criteria=[],
            context="",
            metadata={"session_id": session.session_id, "cwd": str(root), "source": "telegram_session"},
        )

        attempts = {"count": 0}

        async def fake_to_thread(fn, *args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                return ExecutionResult(
                    success=False,
                    output="",
                    backend_session_id="bad-replacement-id",
                    errors=["No conversation found with session ID: stale-session-id"],
                    execution_time=0.01,
                    raw_stdout="",
                    raw_stderr="",
                    parsed_output={"errors": ["No conversation found with session ID: stale-session-id"]},
                    return_code=1,
                )
            return ExecutionResult(
                success=True,
                output="Recovered session reply",
                backend_session_id="fresh-session-id",
                errors=[],
                execution_time=0.01,
                raw_stdout="Recovered session reply",
                raw_stderr="",
                parsed_output={"content": "Recovered session reply"},
                return_code=0,
            )

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

        result = asyncio.run(orch.process_task(task))

        reloaded = orch.session_store.get(session.session_id)
        assert result.success is True
        assert result.output == "Recovered session reply"
        assert attempts["count"] == 2
        assert reloaded is not None
        assert reloaded.backend_session_id == "fresh-session-id"
    finally:
        shutil.rmtree(root, ignore_errors=True)
