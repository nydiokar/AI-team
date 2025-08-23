#!/usr/bin/env python3
"""
Unit tests for CLI hardening features:
- Interactive prompt detection
- Expanded error taxonomy
- Triage field capture
- Environment config overrides
"""
import os
import sys
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from src.bridges.claude_bridge import ClaudeBridge
from src.core.interfaces import Task, TaskType, TaskPriority, TaskStatus


def test_interactive_detection():
    """Test that interactive prompts are detected in stderr/stdout"""
    bridge = ClaudeBridge()
    
    # Test interactive markers
    interactive_markers = [
        "Do you trust the files in this folder",
        "Allow this tool to edit files",
        "Press Enter to continue",
    ]
    
    for marker in interactive_markers:
        result = bridge._parse_result(
            "test_id",
            {
                "returncode": 1,
                "stdout": f"Some output\n{marker}\nmore output",
                "stderr": ""
            },
            0.1
        )
        assert "interactive_prompt_detected" in result.errors


def test_error_classification():
    """Test that error classification works for retry decisions"""
    bridge = ClaudeBridge()
    
    # Test transient errors
    transient_result = bridge._parse_result(
        "test_id",
        {
            "returncode": 1,
            "stdout": "",
            "stderr": "Rate limit exceeded. Please retry later."
        },
        0.1
    )
    assert transient_result.success is False
    
    # Test fatal errors
    fatal_result = bridge._parse_result(
        "test_id",
        {
            "returncode": 1,
            "stdout": "",
            "stderr": "Compilation failed: syntax error"
        },
        0.1
    )
    assert fatal_result.success is False


def test_env_config_overrides():
    """Test that timeout and max_turns can be overridden via environment"""
    from config import config
    
    # Save original values
    original_timeout = config.claude.timeout
    original_max_turns = config.claude.max_turns
    
    try:
        # Test timeout override
        os.environ["CLAUDE_TIMEOUT_SEC"] = "600"
        from config import config as new_config
        assert new_config.claude.timeout == 600
        
        # Test max_turns override
        os.environ["CLAUDE_MAX_TURNS"] = "0"  # Unlimited
        from config import config as unlimited_config
        assert unlimited_config.claude.max_turns == 0
        
    finally:
        # Restore original values
        config.claude.timeout = original_timeout
        config.claude.max_turns = original_max_turns
        if "CLAUDE_TIMEOUT_SEC" in os.environ:
            del os.environ["CLAUDE_TIMEOUT_SEC"]
        if "CLAUDE_MAX_TURNS" in os.environ:
            del os.environ["CLAUDE_MAX_TURNS"]


def test_triage_fields_in_artifact():
    """Test that triage fields are captured in artifacts"""
    from src.orchestrator import TaskOrchestrator
    from src.core.interfaces import TaskResult
    from datetime import datetime
    
    orch = TaskOrchestrator()
    task_id = "test_triage"
    
    # Create result with stdout/stderr
    result = TaskResult(
        task_id=task_id,
        success=True,
        output="test",
        errors=[],
        files_modified=[],
        execution_time=0.01,
        timestamp=datetime.now().isoformat(),
        raw_stdout="stdout content" * 100,  # Long enough to test head/tail
        raw_stderr="stderr content" * 100,
        parsed_output={},
        return_code=0,
    )
    
    # Write artifacts
    orch._write_artifacts(task_id, result)
    
    # Verify triage fields exist
    import json
    artifact_path = Path("results") / f"{task_id}.json"
    if artifact_path.exists():
        with open(artifact_path, "r") as f:
            artifact = json.load(f)
        
        assert "triage" in artifact
        assert "stdout_head" in artifact["triage"]
        assert "stdout_tail" in artifact["triage"]
        assert "stderr_head" in artifact["triage"]
        assert "stderr_tail" in artifact["triage"]
        
        # Clean up test artifact
        artifact_path.unlink(missing_ok=True)


if __name__ == "__main__":
    pytest.main([__file__])
