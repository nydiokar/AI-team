"""
Unit tests for CodexBackend — validates command shape and NDJSON output parsing.
No real Codex CLI invocations; all tests are offline.
"""
import json
import pytest
from src.backends.codex import CodexBackend


# ---------------------------------------------------------------------------
# Command shape
# ---------------------------------------------------------------------------

def test_build_cmd_first_turn_no_cwd():
    backend = CodexBackend()
    cmd = backend._build_cmd(resume_id=None, cwd=None)
    assert cmd[:3] == [backend._exe, "exec", "--json"]
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert cmd[-1] == "-"
    assert "resume" not in cmd
    assert "-C" not in cmd


def test_build_cmd_first_turn_with_cwd():
    backend = CodexBackend()
    cmd = backend._build_cmd(resume_id=None, cwd="/repo")
    assert "-C" in cmd
    assert "/repo" in cmd
    assert "resume" not in cmd


def test_build_cmd_resume_turn():
    backend = CodexBackend()
    session_id = "019d0000-0000-0000-0000-000000000000"
    cmd = backend._build_cmd(resume_id=session_id, cwd="/repo")
    # resume subcommand shape: codex exec resume <id> ...
    assert cmd[1] == "exec"
    assert cmd[2] == "resume"
    assert cmd[3] == session_id
    assert "--json" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert cmd[-1] == "-"
    # -C should NOT appear in resume (the session carries its own cwd)
    assert "-C" not in cmd


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

SAMPLE_NDJSON = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "019d1111-aaaa-bbbb-cccc-000000000001"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Hello there."}}),
    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}),
])


def test_parse_extracts_thread_id_as_backend_session_id():
    result = CodexBackend._parse(SAMPLE_NDJSON, "", 0, 1.2)
    assert result.backend_session_id == "019d1111-aaaa-bbbb-cccc-000000000001"


def test_parse_extracts_agent_message_text_as_output():
    result = CodexBackend._parse(SAMPLE_NDJSON, "", 0, 1.2)
    assert result.output == "Hello there."


def test_parse_success_on_zero_returncode():
    result = CodexBackend._parse(SAMPLE_NDJSON, "", 0, 1.0)
    assert result.success is True
    assert result.errors == []


def test_parse_failure_on_nonzero_returncode():
    result = CodexBackend._parse("", "something went wrong", 1, 0.5)
    assert result.success is False
    assert any("something went wrong" in e for e in result.errors)


def test_parse_failure_adds_generic_error_when_no_stderr():
    result = CodexBackend._parse("", "", 2, 0.5)
    assert result.success is False
    assert any("2" in e for e in result.errors)


def test_parse_preserves_raw_stdout():
    result = CodexBackend._parse(SAMPLE_NDJSON, "", 0, 1.0)
    assert result.raw_stdout == SAMPLE_NDJSON


def test_parse_multiple_agent_messages_concatenated():
    ndjson = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "019d2222-0000-0000-0000-000000000002"}),
        json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "First part."}}),
        json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "Second part."}}),
        json.dumps({"type": "turn.completed"}),
    ])
    result = CodexBackend._parse(ndjson, "", 0, 1.0)
    assert "First part." in result.output
    assert "Second part." in result.output


def test_parse_falls_back_to_raw_stdout_when_no_agent_message():
    ndjson = json.dumps({"type": "thread.started", "thread_id": "019d3333-0000-0000-0000-000000000003"})
    result = CodexBackend._parse(ndjson, "", 0, 1.0)
    assert result.output  # fallback to raw stdout, which is the single JSON line
    assert result.backend_session_id == "019d3333-0000-0000-0000-000000000003"


def test_parse_skips_non_json_lines_gracefully():
    ndjson = "\n".join([
        "not json at all",
        json.dumps({"type": "thread.started", "thread_id": "019d4444-0000-0000-0000-000000000004"}),
        "also not json",
        json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "OK."}}),
    ])
    result = CodexBackend._parse(ndjson, "", 0, 1.0)
    assert result.output == "OK."
    assert result.backend_session_id == "019d4444-0000-0000-0000-000000000004"
