from __future__ import annotations

import time

import pytest

from src.worker.claude_auth import ClaudeAuthError, ClaudeAuthSession


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
    with pytest.raises(ClaudeAuthError, match="login_in_progress"):
        session.start()

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
    fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_cli.chmod(0o755)
    monkeypatch.setattr("src.worker.claude_auth.shutil.which", lambda _name: str(fake_cli))
    monkeypatch.setattr(
        "src.worker.claude_auth.subprocess.run",
        lambda *_args, **_kwargs: type("AuthStatus", (), {"returncode": 0})(),
    )
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
