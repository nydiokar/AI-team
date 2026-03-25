from src.backends.claude_code import ClaudeCodeBackend


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
