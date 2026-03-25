from src.backends import claude_code
from src.backends.claude_code import ClaudeCodeBackend
from src.orchestrator import TaskOrchestrator
from src.core.interfaces import ExecutionResult, Task, TaskType, TaskPriority, TaskStatus, TaskResult
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
    assert "--include-partial-messages" in cmd
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
    assert "--include-partial-messages" in cmd
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

        import src.core.session_store as session_store_module
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
        session_dir = results_dir / "sessions" / session.session_id
        assert session_dir.exists()
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

        import src.core.session_store as session_store_module
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
            errors=["No conversation found with session ID: stable-session-id"],
            execution_time=0.01,
            raw_stdout="",
            raw_stderr="",
            parsed_output={"errors": ["No conversation found with session ID: stable-session-id"]},
            return_code=1,
        )

        async def fake_to_thread(_fn, *_args, **_kwargs):
            return failing

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

        result = asyncio.run(orch.process_task(task))

        reloaded = orch.session_store.get(session.session_id)
        assert result.success is False
        assert result.errors == ["No conversation found with session ID: stable-session-id"]
        assert reloaded is not None
        assert reloaded.backend_session_id == "stable-session-id"
    finally:
        shutil.rmtree(root, ignore_errors=True)
