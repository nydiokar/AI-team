"""
Unit tests for OpenCodeBackend — command shape, output parsing, session ID
extraction, dirty-repo rejection, concurrent-lock rejection, and diff collection.

No real opencode binary is required; all subprocess calls are mocked.
"""
import json
import threading
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from src.backends.opencode import OpenCodeBackend, OpenCodeServerBackend, _get_repo_lock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(
    session_id: str = "gw-session-1",
    repo_path: str = "/repo",
    backend_session_id: str = "",
    last_user_message: str = "do the thing",
) -> MagicMock:
    s = MagicMock()
    s.session_id = session_id
    s.repo_path = repo_path
    s.backend_session_id = backend_session_id
    s.last_user_message = last_user_message
    s.task_history = []
    return s


def _ndjson(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

def test_build_cmd_new_session_with_title():
    b = OpenCodeBackend()
    cmd = b._build_cmd(cwd="/repo", message="do the thing", session_id=None, title="my-title", model=None, agent=None)
    assert cmd[:4] == [b._exe, "run", "--dir", "/repo"]
    assert "--format" in cmd and "json" in cmd
    assert "--title" in cmd and "my-title" in cmd
    assert "--session" not in cmd
    assert cmd[-1] == "do the thing"


def test_build_cmd_resume_uses_session_flag():
    b = OpenCodeBackend()
    cmd = b._build_cmd(cwd="/repo", message="follow up", session_id="ses_abc123", title=None, model=None, agent=None)
    assert "--session" in cmd
    idx = cmd.index("--session")
    assert cmd[idx + 1] == "ses_abc123"
    assert "--title" not in cmd
    assert cmd[-1] == "follow up"


def test_build_cmd_includes_model_and_agent():
    b = OpenCodeBackend()
    cmd = b._build_cmd(cwd="/repo", message="prompt", session_id=None, title="t", model="anthropic/claude-3-5-sonnet", agent="coding")
    assert "--model" in cmd and "anthropic/claude-3-5-sonnet" in cmd
    assert "--agent" in cmd and "coding" in cmd
    assert cmd[-1] == "prompt"


def test_build_cmd_never_contains_continue_flag():
    b = OpenCodeBackend()
    for cmd in [
        b._build_cmd(cwd="/r", message="x", session_id=None, title="x", model=None, agent=None),
        b._build_cmd(cwd="/r", message="x", session_id="ses_1", title=None, model=None, agent=None),
    ]:
        assert "--continue" not in cmd


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

SAMPLE_EVENTS = _ndjson(
    {"type": "session", "sessionID": "ses_deadbeef0001"},
    {"type": "message", "content": "Done! I updated the code."},
)


def test_parse_extracts_session_id_from_sessionID_field():
    result = OpenCodeBackend._parse(SAMPLE_EVENTS, "", 0, 1.5)
    assert result.backend_session_id == "ses_deadbeef0001"


def test_parse_extracts_output_from_content_field():
    result = OpenCodeBackend._parse(SAMPLE_EVENTS, "", 0, 1.5)
    assert "Done!" in result.output


def test_parse_success_on_zero_exit():
    result = OpenCodeBackend._parse(SAMPLE_EVENTS, "", 0, 1.0)
    assert result.success is True
    assert result.errors == []


def test_parse_failure_on_nonzero_exit():
    result = OpenCodeBackend._parse("", "fatal: not a git repo", 1, 0.5)
    assert result.success is False
    assert any("fatal" in e for e in result.errors)


def test_parse_generic_error_when_no_stderr():
    result = OpenCodeBackend._parse("", "", 2, 0.5)
    assert result.success is False
    assert any("2" in e for e in result.errors)


def test_parse_raw_stdout_preserved():
    result = OpenCodeBackend._parse(SAMPLE_EVENTS, "", 0, 1.0)
    assert result.raw_stdout == SAMPLE_EVENTS


def test_parse_session_id_nested_in_session_object():
    ndjson = _ndjson({"type": "init", "session": {"id": "ses_nested_99"}})
    result = OpenCodeBackend._parse(ndjson, "", 0, 0.5)
    assert result.backend_session_id == "ses_nested_99"


def test_parse_json_parse_failure_falls_back_to_raw_stdout():
    bad_stdout = "this is not json at all\nalso not json"
    result = OpenCodeBackend._parse(bad_stdout, "", 0, 0.5)
    assert result.success is True  # exit 0 → success
    assert bad_stdout.strip() in result.output  # falls back to raw stdout


def test_parse_skips_non_json_lines():
    mixed = "not json\n" + json.dumps({"sessionID": "ses_ok_001"}) + "\nmore garbage"
    result = OpenCodeBackend._parse(mixed, "", 0, 0.5)
    assert result.backend_session_id == "ses_ok_001"


# ---------------------------------------------------------------------------
# Suspect-run / dead-end detection (false-success gate)
# Regression for session 3383428cbe2a / task_8652c07a: opencode exited 0 with
# only intent-only text after auto-rejecting an external_directory permission,
# and the result was scored success=True. It must now be a failure.
# ---------------------------------------------------------------------------

def _intent_only_with_block():
    """Reconstruct the real dead-end: intent text + a rejected permission, exit 0."""
    stdout = _ndjson(
        {"type": "text", "part": {"type": "text",
            "text": "Understood. Working autonomously to get the full pipeline running. "
                    "Starting with the opencode server failure."}},
        {"type": "tool_use", "part": {"type": "tool", "tool": "glob", "state": {
            "status": "error", "error": "The user rejected permission to use this specific tool call."}}},
        {"type": "step_finish", "part": {"type": "step-finish", "reason": "tool-calls"}},
    )
    stderr = "! permission requested: external_directory (C:\\Users\\x\\.config\\opencode\\*); auto-rejecting"
    return stdout, stderr


def test_intent_only_after_permission_block_is_failure():
    stdout, stderr = _intent_only_with_block()
    result = OpenCodeBackend._parse(stdout, stderr, 0, 15.2)
    assert result.success is False
    assert result.error_class == "permission_block"
    assert any("dead-end" in e.lower() or "auto-rejected" in e.lower() for e in result.errors)


def test_real_work_after_permission_block_is_not_failed():
    """A substantive reply that ends naturally must NOT be flagged, even if a
    permission was rejected somewhere mid-run."""
    stdout = _ndjson(
        {"type": "text", "part": {"type": "text",
            "text": "I fixed the root cause: the agent config had edit:deny. " * 30}},
        {"type": "tool_use", "part": {"type": "tool", "tool": "glob", "state": {
            "status": "error", "error": "The user rejected permission to use this specific tool call."}}},
        {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}},
    )
    result = OpenCodeBackend._parse(stdout, "auto-rejecting", 0, 120.0)
    assert result.success is True


def test_normal_success_unaffected_by_gate():
    """No permission block → ordinary success is untouched."""
    result = OpenCodeBackend._parse(SAMPLE_EVENTS, "", 0, 1.0)
    assert result.success is True
    assert result.error_class == ""


def test_short_reply_starting_with_let_me_is_not_flagged():
    """Regression: bare openers like 'Let me ...' must NOT be treated as
    intent-only, even with a permission marker present. They commonly begin
    real, completed short replies."""
    stdout = _ndjson(
        {"type": "text", "part": {"type": "text",
            "text": "Let me know if you want the diff — the fix is applied and tests pass."}},
        {"type": "step_finish", "part": {"type": "step-finish", "reason": "tool-calls"}},
    )
    result = OpenCodeBackend._parse(stdout, "auto-rejecting", 0, 30.0)
    assert result.success is True
    assert result.error_class == ""


def test_external_directory_marker_alone_does_not_flag():
    """Regression: opencode prints 'external_directory' in permission PROMPTS
    even for allowed calls; that token alone must not flip success."""
    stdout = _ndjson(
        {"type": "text", "part": {"type": "text", "text": "Understood. Starting with the fix."}},
        {"type": "step_finish", "part": {"type": "step-finish", "reason": "tool-calls"}},
    )
    # stderr mentions external_directory but NO actual rejection occurred.
    result = OpenCodeBackend._parse(stdout, "permission requested: external_directory (x); allowing", 0, 5.0)
    assert result.success is True
    assert result.error_class == ""


def test_suspect_flag_cleared_when_files_modified(tmp_path):
    """A run flagged as a dead-end in _parse must be un-flagged in _run_locked
    if it actually modified files (real work happened)."""
    b = OpenCodeBackend()
    stdout, stderr = _intent_only_with_block()
    stdout_bytes = [(l + "\n").encode() for l in stdout.splitlines()]
    stderr_bytes = [(stderr + "\n").encode()]

    class _FakeProc:
        pid = 999
        returncode = 0
        stdout = MagicMock()
        stderr = MagicMock()

        def wait(self, timeout=None):
            pass

    def _fake_popen(cmd, **kwargs):
        p = _FakeProc()
        p.stdout.__iter__ = lambda self: iter(stdout_bytes)
        p.stderr.__iter__ = lambda self: iter(stderr_bytes)
        return p

    with (
        patch.object(b, "_pre_run_git_check", return_value=None),
        patch.object(b, "_recover_session_id", return_value="ses_x"),
        patch("src.backends.opencode.subprocess.Popen", side_effect=_fake_popen),
        patch("src.backends.opencode._git_changed_files", return_value=["src/foo.py"]),
        patch("src.backends.opencode._run_git", return_value="diff --git a/src/foo.py b/src/foo.py"),
        patch.object(OpenCodeBackend, "_auto_commit", staticmethod(lambda cwd, label: None)),
        patch("src.core.test_guard.assert_live_calls_allowed", return_value=None),
        patch("config.config") as mock_cfg,
    ):
        mock_cfg.system.inactivity_timeout_sec = 600
        mock_cfg.opencode.collect_diff = True
        result = b._run(cwd=str(tmp_path), message="go", session_id="ses_x",
                        title=None, model=None, agent=None, session_key="k")

    assert result.success is True, f"expected un-flag, got errors={result.errors}"
    assert result.error_class == ""


# ---------------------------------------------------------------------------
# Concurrent same-repo lock rejection
# ---------------------------------------------------------------------------

def test_concurrent_same_repo_lock_rejected(tmp_path):
    b = OpenCodeBackend()
    repo = str(tmp_path)
    lock = _get_repo_lock(repo)
    lock.acquire()
    try:
        with patch.object(b, "_pre_run_git_check", return_value=None):
            result = b._run(
                cwd=repo,
                message="prompt",
                session_id=None,
                title="t",
                model=None,
                agent=None,
                session_key=None,
            )
        assert result.success is False
        assert any("already running" in e or "Concurrent" in e for e in result.errors)
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Missing session ID → needs_manual_attention
# ---------------------------------------------------------------------------

def test_missing_session_id_marks_needs_manual_attention(tmp_path):
    b = OpenCodeBackend()

    class _FakeProc:
        pid = 1234
        returncode = 0
        stdout = MagicMock()
        stderr = MagicMock()

        def wait(self, timeout=None):
            pass

    def _fake_popen(cmd, **kwargs):
        p = _FakeProc()
        p.stdout.__iter__ = lambda self: iter([])
        p.stderr.__iter__ = lambda self: iter([])
        return p

    with (
        patch.object(b, "_pre_run_git_check", return_value=None),
        patch("src.backends.opencode.subprocess.Popen", side_effect=_fake_popen),
        patch("src.backends.opencode._git_changed_files", return_value=[]),
        patch.object(b, "_recover_session_id", return_value=None),
        patch("src.core.test_guard.assert_live_calls_allowed", return_value=None),
        patch("config.config") as mock_cfg,
    ):
        mock_cfg.system.inactivity_timeout_sec = 600
        mock_cfg.opencode.collect_diff = False
        result = b._run(
            cwd=str(tmp_path),
            message="prompt",
            session_id=None,
            title="t",
            model=None,
            agent=None,
            session_key=None,
        )
    assert result.success is False
    assert any("needs_manual_attention" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Continuation with no session ID stored
# ---------------------------------------------------------------------------

def test_resume_session_falls_back_to_create_when_no_backend_session_id(tmp_path):
    """When no backend_session_id is stored, resume falls back to create_session (fresh start)."""
    b = OpenCodeBackend()
    session = _make_session(backend_session_id="", repo_path=str(tmp_path))

    class _FakeProc:
        pid = 11
        returncode = 0
        stdout = MagicMock()
        stderr = MagicMock()

        def wait(self, timeout=None):
            pass

    stdout_line = (
        json.dumps({"sessionID": "ses_new_from_fallback", "type": "message", "content": "started fresh"}).encode()
        + b"\n"
    )

    def _fake_popen(cmd, **kwargs):
        p = _FakeProc()
        p.stdout.__iter__ = lambda self: iter([stdout_line])
        p.stderr.__iter__ = lambda self: iter([])
        return p

    with (
        patch.object(b, "_pre_run_git_check", return_value=None),
        patch("src.backends.opencode.subprocess.Popen", side_effect=_fake_popen),
        patch("src.backends.opencode._git_changed_files", return_value=[]),
        patch("src.core.test_guard.assert_live_calls_allowed", return_value=None),
        patch("config.config") as mock_cfg,
    ):
        mock_cfg.system.inactivity_timeout_sec = 600
        mock_cfg.opencode.collect_diff = False
        result = b.resume_session(session, "follow-up prompt")

    assert result.success is True
    assert result.backend_session_id == "ses_new_from_fallback"


def test_resume_session_uses_explicit_session_id():
    b = OpenCodeBackend()
    session = _make_session(backend_session_id="ses_explicit_42")
    captured = []

    class _FakeProc:
        pid = 55
        returncode = 0
        stdout = MagicMock()
        stderr = MagicMock()

        def wait(self, timeout=None):
            pass

    def _fake_popen(cmd, **kwargs):
        captured.extend(cmd)
        p = _FakeProc()
        stdout_event = json.dumps({"sessionID": "ses_explicit_42", "type": "message", "content": "ok"}).encode() + b"\n"
        p.stdout.__iter__ = lambda self: iter([stdout_event])
        p.stderr.__iter__ = lambda self: iter([])
        return p

    with (
        patch.object(b, "_pre_run_git_check", return_value=None),
        patch("src.backends.opencode.subprocess.Popen", side_effect=_fake_popen),
        patch("src.backends.opencode._git_changed_files", return_value=[]),
        patch("src.core.test_guard.assert_live_calls_allowed", return_value=None),
        patch("config.config") as mock_cfg,
    ):
        mock_cfg.system.inactivity_timeout_sec = 600
        mock_cfg.opencode.collect_diff = False
        b.resume_session(session, "follow up")

    assert "--session" in captured
    idx = captured.index("--session")
    assert captured[idx + 1] == "ses_explicit_42"
    assert "--continue" not in captured


# ---------------------------------------------------------------------------
# Nonzero exit code
# ---------------------------------------------------------------------------

def test_nonzero_exit_produces_failure_result(tmp_path):
    b = OpenCodeBackend()

    class _FakeProc:
        pid = 77
        returncode = 1
        stdout = MagicMock()
        stderr = MagicMock()

        def wait(self, timeout=None):
            pass

    def _fake_popen(cmd, **kwargs):
        p = _FakeProc()
        p.stdout.__iter__ = lambda self: iter([])
        err_line = b"error: opencode exploded\n"
        p.stderr.__iter__ = lambda self: iter([err_line])
        return p

    with (
        patch.object(b, "_pre_run_git_check", return_value=None),
        patch("src.backends.opencode.subprocess.Popen", side_effect=_fake_popen),
        patch("src.core.test_guard.assert_live_calls_allowed", return_value=None),
        patch("config.config") as mock_cfg,
    ):
        mock_cfg.system.inactivity_timeout_sec = 600
        mock_cfg.opencode.collect_diff = False
        result = b._run(
            cwd=str(tmp_path),
            message="x",
            session_id=None,
            title="t",
            model=None,
            agent=None,
            session_key=None,
        )
    assert result.success is False
    assert result.return_code == 1
    assert any("opencode exploded" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Diff collection
# ---------------------------------------------------------------------------

def test_diff_collected_after_successful_run(tmp_path):
    b = OpenCodeBackend()

    class _FakeProc:
        pid = 88
        returncode = 0
        stdout = MagicMock()
        stderr = MagicMock()

        def wait(self, timeout=None):
            pass

    stdout_line = json.dumps({"sessionID": "ses_diff_test", "type": "message", "content": "done"}).encode() + b"\n"

    def _fake_popen(cmd, **kwargs):
        p = _FakeProc()
        p.stdout.__iter__ = lambda self: iter([stdout_line])
        p.stderr.__iter__ = lambda self: iter([])
        return p

    with (
        patch.object(b, "_pre_run_git_check", return_value=None),
        patch("src.backends.opencode.subprocess.Popen", side_effect=_fake_popen),
        patch("src.backends.opencode._git_changed_files", return_value=["src/foo.py"]),
        patch("src.backends.opencode._run_git", side_effect=lambda cwd, args, **kw: "1 file changed" if "--stat" in args else "diff output"),
        patch.object(b, "_auto_commit"),
        patch("src.core.test_guard.assert_live_calls_allowed", return_value=None),
        patch("config.config") as mock_cfg,
    ):
        mock_cfg.system.inactivity_timeout_sec = 600
        mock_cfg.opencode.collect_diff = True
        result = b._run(
            cwd=str(tmp_path),
            message="prompt",
            session_id=None,
            title="t",
            model=None,
            agent=None,
            session_key=None,
        )

    assert "src/foo.py" in result.files_modified
    assert isinstance(result.parsed_output, dict)
    assert result.parsed_output.get("git_diff_stat") == "1 file changed"
    assert result.parsed_output.get("git_diff") == "diff output"


# ---------------------------------------------------------------------------
# Session list fallback
# ---------------------------------------------------------------------------

def test_session_list_fallback_recovers_session_id(tmp_path):
    b = OpenCodeBackend()

    class _FakeProc:
        pid = 99
        returncode = 0
        stdout = MagicMock()
        stderr = MagicMock()

        def wait(self, timeout=None):
            pass

    def _fake_popen(cmd, **kwargs):
        p = _FakeProc()
        # No session ID in stdout
        p.stdout.__iter__ = lambda self: iter([])
        p.stderr.__iter__ = lambda self: iter([])
        return p

    session_list_json = json.dumps([
        {"id": "ses_recovered_001", "title": "my-task-title", "cwd": str(tmp_path)},
    ])

    def _fake_session_list_run(cmd, **kwargs):
        r = MagicMock()
        r.returncode = 0
        r.stdout = session_list_json
        return r

    with (
        patch.object(b, "_pre_run_git_check", return_value=None),
        patch("src.backends.opencode.subprocess.Popen", side_effect=_fake_popen),
        patch("src.backends.opencode.subprocess.run", side_effect=_fake_session_list_run),
        patch("src.backends.opencode._git_changed_files", return_value=[]),
        patch("src.core.test_guard.assert_live_calls_allowed", return_value=None),
        patch("config.config") as mock_cfg,
    ):
        mock_cfg.system.inactivity_timeout_sec = 600
        mock_cfg.opencode.collect_diff = False
        result = b._run(
            cwd=str(tmp_path),
            message="prompt",
            session_id=None,
            title="my-task-title",
            model=None,
            agent=None,
            session_key=None,
        )

    assert result.backend_session_id == "ses_recovered_001"
    assert result.success is True


# ---------------------------------------------------------------------------
# Timeout (inactivity)
# ---------------------------------------------------------------------------

def test_inactivity_timeout_kills_process_and_returns_failure(tmp_path):
    """Verify that inactivity timeout kills the process and returns an error.

    We trigger the timeout by making queue.Queue.get raise queue.Empty, which
    is exactly what happens when the process produces no stdout for inactivity_sec.
    """
    import queue as _queue_mod
    b = OpenCodeBackend()

    class _HangingProc:
        pid = 1111
        returncode = None
        stdout = MagicMock()
        stderr = MagicMock()

        def wait(self, timeout=None):
            self.returncode = -9

    def _fake_popen(cmd, **kwargs):
        p = _HangingProc()
        p.stdout.__iter__ = lambda self: iter([])
        p.stderr.__iter__ = lambda self: iter([])
        return p

    terminated = []
    _call_count = [0]
    _real_queue_get = _queue_mod.Queue.get

    def _patched_queue_get(self, block=True, timeout=None):
        # On the first blocking call (the inactivity wait) raise Empty
        if block and timeout is not None:
            _call_count[0] += 1
            if _call_count[0] == 1:
                raise _queue_mod.Empty
        return _real_queue_get(self, block=block, timeout=timeout)

    with (
        patch.object(b, "_pre_run_git_check", return_value=None),
        patch("src.backends.opencode.subprocess.Popen", side_effect=_fake_popen),
        patch("src.backends.opencode.terminate_many_popen", side_effect=lambda procs: terminated.extend(p.pid for p in procs)),
        patch("src.backends.opencode.queue.Queue.get", _patched_queue_get),
        patch("src.core.test_guard.assert_live_calls_allowed", return_value=None),
        patch("config.config") as mock_cfg,
    ):
        mock_cfg.system.inactivity_timeout_sec = 600
        mock_cfg.opencode.collect_diff = False
        result = b._run(
            cwd=str(tmp_path),
            message="hang",
            session_id=None,
            title="t",
            model=None,
            agent=None,
            session_key=None,
        )

    assert result.success is False
    assert any("inactivity" in e.lower() for e in result.errors)
    assert 1111 in terminated


# ---------------------------------------------------------------------------
# Starting a task end-to-end (happy path smoke test)
# ---------------------------------------------------------------------------

def test_start_task_successfully(tmp_path):
    """Full create_session path with mocked subprocess."""
    b = OpenCodeBackend()
    session = _make_session(repo_path=str(tmp_path), last_user_message="refactor the code")

    stdout_line = json.dumps({
        "sessionID": "ses_new_happy",
        "type": "message",
        "content": "Refactored successfully.",
    }).encode() + b"\n"

    class _FakeProc:
        pid = 200
        returncode = 0
        stdout = MagicMock()
        stderr = MagicMock()

        def wait(self, timeout=None):
            pass

    def _fake_popen(cmd, **kwargs):
        p = _FakeProc()
        p.stdout.__iter__ = lambda self: iter([stdout_line])
        p.stderr.__iter__ = lambda self: iter([])
        return p

    with (
        patch.object(b, "_pre_run_git_check", return_value=None),
        patch("src.backends.opencode.subprocess.Popen", side_effect=_fake_popen),
        patch("src.backends.opencode._git_changed_files", return_value=[]),
        patch("src.backends.opencode._run_git", return_value=""),
        patch("src.core.test_guard.assert_live_calls_allowed", return_value=None),
        patch("config.config") as mock_cfg,
    ):
        mock_cfg.system.inactivity_timeout_sec = 600
        mock_cfg.opencode.collect_diff = True
        result = b.create_session(session)

    assert result.success is True
    assert result.backend_session_id == "ses_new_happy"
    assert "Refactored" in result.output


# ---------------------------------------------------------------------------
# OpenCode server transport failures
# ---------------------------------------------------------------------------

def test_server_http_timeout_kills_cached_server_process():
    b = OpenCodeServerBackend()
    key = "/repo"

    class _Proc:
        pid = 4242

    proc = _Proc()
    b._procs[key] = proc
    b._base_urls[key] = "http://127.0.0.1:4096"
    terminated = []

    with (
        patch("src.backends.opencode.urllib.request.urlopen", side_effect=TimeoutError("timed out")),
        patch(
            "src.backends.opencode.terminate_many_popen",
            side_effect=lambda procs: terminated.extend(procs),
        ),
    ):
        response, err = b._http(key, "POST", "/session/ses_1/message", {"parts": []}, timeout=7)

    assert response == {}
    assert "timed out" in err
    assert "will restart on next call" in err
    assert key not in b._procs
    assert key not in b._base_urls
    assert terminated == [proc]


def test_server_resume_transport_failure_clears_backend_session_id(tmp_path):
    b = OpenCodeServerBackend()
    session = _make_session(
        repo_path=str(tmp_path),
        backend_session_id="ses_old",
        last_user_message="previous",
    )

    with (
        patch.object(b, "_ensure_server", return_value=None),
        patch.object(
            b,
            "_http",
            side_effect=[
                ({"id": "ses_old"}, None),
                (
                    {},
                    "opencode server timed out (POST /session/ses_old/message) after 7s "
                    "— killed server; will restart on next call",
                ),
            ],
        ),
        patch.object(b, "_parse_model", return_value=(None, None)),
    ):
        result = b.resume_session(session, "continue")

    assert result.success is False
    assert session.backend_session_id == ""
    assert result.backend_session_id == ""


def test_server_create_transport_failure_does_not_persist_backend_session_id(tmp_path):
    b = OpenCodeServerBackend()
    session = _make_session(repo_path=str(tmp_path), last_user_message="start")

    with (
        patch.object(b, "_ensure_server", return_value=None),
        patch.object(
            b,
            "_http",
            side_effect=[
                ({"id": "ses_new"}, None),
                (
                    {},
                    "opencode server timed out (POST /session/ses_new/message) after 7s "
                    "— killed server; will restart on next call",
                ),
            ],
        ),
        patch.object(b, "_parse_model", return_value=(None, None)),
    ):
        result = b.create_session(session)

    assert result.success is False
    assert session.backend_session_id == ""
    assert result.backend_session_id == ""
