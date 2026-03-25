from src.backends.claude_code import ClaudeCodeBackend
from src.orchestrator import TaskOrchestrator
from src.core.interfaces import Task, TaskType, TaskPriority, TaskStatus, TaskResult
from datetime import datetime
from pathlib import Path
import shutil
import uuid



def test_create_session_uses_text_output_and_explicit_session_id():
    backend = ClaudeCodeBackend()

    cmd = backend._build_cmd(resume_id=None, session_id="11111111-1111-1111-1111-111111111111")

    assert "--output-format" in cmd
    assert "text" in cmd
    assert "--session-id" in cmd
    assert "11111111-1111-1111-1111-111111111111" in cmd
    assert "--resume" not in cmd


def test_resume_session_uses_text_output_and_resume_id():
    backend = ClaudeCodeBackend()

    cmd = backend._build_cmd(
        resume_id="22222222-2222-2222-2222-222222222222",
        session_id=None,
    )

    assert "--resume" in cmd
    assert "22222222-2222-2222-2222-222222222222" in cmd
    assert "--output-format" in cmd
    assert "text" in cmd
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
            parsed_output={"content": "OK"},
        )
        setattr(result, "backend_name", "claude")

        orch._write_artifacts(task.id, result, task=task)

        flat = results_dir / f"{task.id}.json"
        assert flat.exists()
        data = flat.read_text(encoding="utf-8")
        assert session.session_id in data
        session_dir = results_dir / "sessions" / session.session_id
        assert session_dir.exists()
        assert any(p.suffix == ".json" for p in session_dir.iterdir())
    finally:
        shutil.rmtree(root, ignore_errors=True)
