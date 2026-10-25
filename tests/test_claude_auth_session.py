from __future__ import annotations

import time
import os

import pytest

from src.worker.claude_auth import ClaudeAuthError, ClaudeAuthSession


@pytest.mark.parametrize("backend,output,code,state", [
    ("claude", '{"loggedIn":true}', 0, "signed_in"),
    ("claude", '{"loggedIn":false}', 1, "signed_out"),
    ("claude", '{"loggedIn":"false"}', 0, "unavailable"),
    ("claude", '[]', 0, "unavailable"),
    ("claude", 'broken', 1, "unavailable"),
    ("codex", 'Logged in using ChatGPT', 0, "signed_in"),
    ("codex", 'Not logged in', 1, "signed_out"),
    ("codex", 'network failure', 1, "unavailable"),
    ("codex", '', 0, "unavailable"),
])
def test_provider_status(tmp_path, monkeypatch, backend, output, code, state) -> None:
    cli = tmp_path / backend
    cli.write_text(f"#!/bin/sh\nprintf '%s' '{output}'\nexit {code}\n")
    cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _: str(cli))
    assert ClaudeAuthSession().status(backend)["state"] == state


def test_status_missing_unknown_timeout_and_output_bound(tmp_path, monkeypatch) -> None:
    session = ClaudeAuthSession()
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _: None)
    assert session.status("claude")["state"] == "unavailable"
    with pytest.raises(ClaudeAuthError, match="backend_unsupported"):
        session.start("shell")
    cli = tmp_path / "fake"
    cli.write_text("#!/bin/sh\nexec sleep 10\n")
    cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _: str(cli))
    monkeypatch.setattr("src.worker.claude_auth._STATUS_TIMEOUT_SEC", 0.05)
    assert session.status("claude")["state"] == "unavailable"
    cli.write_text("#!/bin/sh\nhead -c 70000 /dev/zero\n")
    assert session.status("claude")["state"] == "unavailable"


def test_codex_device_flow_and_provider_binding(tmp_path, monkeypatch) -> None:
    cli = tmp_path / "codex"
    cli.write_text('#!/bin/sh\nif [ "$2" = "status" ]; then echo "Logged in using ChatGPT"; else test "$2" = "--device-auth" || exit 2; echo "https://auth.openai.com/codex/device DEVICE-CODE"; fi\n')
    cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _: str(cli))
    session = ClaudeAuthSession()
    try:
        result = session.start("codex")
        assert result["input_supported"] is False
        login_id = result["login_id"]
        assert "DEVICE-CODE" in _wait_for_text(session, login_id, "DEVICE-CODE")["output"]
        with pytest.raises(ClaudeAuthError, match="backend_mismatch"):
            session.validate_backend(login_id, "claude")
        with pytest.raises(ClaudeAuthError, match="input_not_supported"):
            session.send(login_id, "no\n")
    finally:
        session.shutdown()


def test_start_reattaches_to_same_provider_without_replacing_process(tmp_path, monkeypatch) -> None:
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\nexec cat\n")
    cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _: str(cli))
    session = ClaudeAuthSession()
    try:
        first = session.start("claude")
        process = session._login.process
        assert session.start("claude")["login_id"] == first["login_id"]
        assert session._login.process is process
        with pytest.raises(ClaudeAuthError, match="login_in_progress"):
            session.start("codex")
    finally:
        session.shutdown()


def test_partial_and_blocked_writes_expiry_and_cleanup(tmp_path, monkeypatch) -> None:
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\nexec cat\n")
    cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _: str(cli))
    session = ClaudeAuthSession()
    try:
        login_id = session.start()["login_id"]
        real_write = os.write
        calls = []
        def partial(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                raise BlockingIOError()
            return real_write(fd, data[:2])
        monkeypatch.setattr("src.worker.claude_auth.os.write", partial)
        session.send(login_id, "hello\n")
        assert len(calls) == 4
        monkeypatch.setattr("src.worker.claude_auth.select.select", lambda *_: ([], [], []))
        with pytest.raises(ClaudeAuthError, match="login_input_timeout"):
            session.send(login_id, "x")
        process = session._login.process
        session._expire(login_id)
        assert process.poll() is not None
        with pytest.raises(ClaudeAuthError, match="login_not_found"):
            session.poll(login_id, 0)
    finally:
        session.shutdown()


def test_output_truncation_offsets_and_shutdown(tmp_path, monkeypatch) -> None:
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\nhead -c 90000 /dev/zero\necho END\nexec cat\n")
    cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _: str(cli))
    session = ClaudeAuthSession()
    try:
        login_id = session.start()["login_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = session.poll(login_id, 0)
            if "END" in result["output"]:
                break
            time.sleep(0.01)
        assert result["offset"] > 90000
        assert len(result["output"].encode()) <= 65536
        assert result["truncated"] is True
        assert session.poll(login_id, result["offset"])["output"] == ""
        process = session._login.process
        session.shutdown()
        assert process.poll() is not None
    finally:
        session.shutdown()


def test_claude_code_paste_completes_and_status_is_checked(tmp_path, monkeypatch) -> None:
    cli = tmp_path / "claude"
    marker = tmp_path / "authenticated"
    cli.write_text(f'''#!/bin/sh
if [ "$2" = "status" ]; then
  if [ -f '{marker}' ]; then echo '{{"loggedIn":true}}'; else echo '{{"loggedIn":false}}'; fi
else
  echo 'Open https://claude.ai/oauth/authorize then paste code'
  read code
  if [ "$code" = "TEST-CODE" ]; then touch '{marker}'; fi
fi
''')
    cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _: str(cli))
    session = ClaudeAuthSession()
    try:
        assert session.status("claude")["state"] == "signed_out"
        login_id = session.start()["login_id"]
        _wait_for_text(session, login_id, "paste code")
        session.send(login_id, "TEST-CODE\n")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = session.poll(login_id, 0)
            if result["status"] == "exited": break
            time.sleep(0.01)
        assert result["authenticated"] is True
        assert session.status("claude")["state"] == "signed_in"
    finally:
        session.shutdown()


def _wait_for_text(session: ClaudeAuthSession, login_id: str, needle: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    offset = 0
    result: dict[str, object] = {}
    received = ""
    while time.monotonic() < deadline:
        result = session.poll(login_id, offset)
        offset = int(result["offset"])
        received += str(result["output"])
        if needle in received:
            return {**result, "output": received}
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for fake CLI output: {needle}; last={result!r}; received={received!r}")


def test_claude_auth_session_relays_remote_code_without_general_shell(tmp_path, monkeypatch) -> None:
    fake_cli = tmp_path / "claude"
    fake_cli.write_text(
        "#!/bin/sh\n"
        "printf 'Open https://claude.example.test/login\\nPaste code: '\n"
        "exec cat\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _name: str(fake_cli))
    session = ClaudeAuthSession()

    started = session.start()
    login_id = started["login_id"]
    assert _wait_for_text(session, login_id, "Paste code:")["status"] == "running"
    assert session.start()["login_id"] == login_id

    session.send(login_id, "TEST-CODE\n")
    result = _wait_for_text(session, login_id, "TEST-CODE")
    assert result["status"] == "running"
    session.cancel(login_id)
    assert session.start()["status"] == "running"
    session.shutdown()


def test_claude_auth_session_rejects_invalid_inputs(tmp_path, monkeypatch) -> None:
    fake_cli = tmp_path / "claude"
    fake_cli.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    fake_cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _name: str(fake_cli))
    session = ClaudeAuthSession()
    login_id = session.start()["login_id"]
    with pytest.raises(ClaudeAuthError, match="invalid_input"):
        session.send(login_id, "x" * 4097)
    session.send(login_id, "\n")  # The CLI may ask the user to press Enter.
    with pytest.raises(ClaudeAuthError, match="invalid_offset"):
        session.poll(login_id, -1)
    session.shutdown()


def test_completed_login_reports_authenticated_state(tmp_path, monkeypatch) -> None:
    fake_cli = tmp_path / "claude"
    fake_cli.write_text('#!/bin/sh\nif [ "$2" = "status" ]; then echo \'{"loggedIn":true}\'; fi\nexit 0\n', encoding="utf-8")
    fake_cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _name: str(fake_cli))
    session = ClaudeAuthSession()
    login_id = session.start()["login_id"]

    deadline = time.monotonic() + 3
    result: dict[str, object] = {}
    while time.monotonic() < deadline:
        result = session.poll(login_id, 0)
        if result["status"] == "exited":
            break
        time.sleep(0.02)
    assert result["status"] == "exited"
    assert result["authenticated"] is True
    session.cancel(login_id)
