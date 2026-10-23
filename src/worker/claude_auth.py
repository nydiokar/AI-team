"""Bounded interactive Claude Code login owned by a worker process.

The login flow is the only command this module starts. It uses a PTY so the
operator can complete Claude's remote browser/code-paste flow without exposing
a general shell through the control API.
"""
from __future__ import annotations

import os
import pty
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass

_MAX_OUTPUT = 64 * 1024
_MAX_INPUT = 4096
_MAX_LIFETIME_SEC = 600


@dataclass
class _Login:
    login_id: str
    executable: str
    process: subprocess.Popen[bytes]
    master_fd: int
    started_at: float
    output: bytearray
    output_offset: int
    auth_checked: bool
    authenticated: bool | None


class ClaudeAuthError(RuntimeError):
    """Stable failure for the worker's Claude login handoff."""


class ClaudeAuthSession:
    """Own at most one bounded `claude auth login` PTY at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._login: _Login | None = None
        self._timer: threading.Timer | None = None

    def start(self) -> dict[str, str]:
        with self._lock:
            self._collect_locked()
            if self._login is not None and self._login.process.poll() is not None:
                self._dispose_locked(self._login)
            if self._login is not None:
                raise ClaudeAuthError("login_in_progress")
            executable = shutil.which("claude")
            if not executable:
                raise ClaudeAuthError("claude_missing")
            try:
                master_fd, slave_fd = pty.openpty()
            except OSError as exc:
                raise ClaudeAuthError("pty_unavailable") from exc
            try:
                process = subprocess.Popen(
                    [executable, "auth", "login"],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception as exc:
                os.close(master_fd)
                os.close(slave_fd)
                raise ClaudeAuthError("claude_start_failed") from exc
            os.close(slave_fd)
            try:
                os.set_blocking(master_fd, False)
            except OSError as exc:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                os.close(master_fd)
                raise ClaudeAuthError("pty_unavailable") from exc
            login_id = uuid.uuid4().hex
            self._login = _Login(
                login_id=login_id,
                executable=executable,
                process=process,
                master_fd=master_fd,
                started_at=time.monotonic(),
                output=bytearray(),
                output_offset=0,
                auth_checked=False,
                authenticated=None,
            )
            self._timer = threading.Timer(_MAX_LIFETIME_SEC, self._expire, args=(login_id,))
            self._timer.daemon = True
            self._timer.start()
            return {"login_id": login_id, "status": "running"}

    def poll(self, login_id: str, offset: int) -> dict[str, object]:
        if offset < 0 or offset > 2**31 - 1:
            raise ClaudeAuthError("invalid_offset")
        with self._lock:
            login = self._current(login_id)
            self._collect_locked()
            login = self._current(login_id)
            first_offset = login.output_offset - len(login.output)
            output = bytes(login.output[max(0, offset - first_offset):])
            if login.process.poll() is not None and not login.auth_checked:
                try:
                    result = subprocess.run(
                        [login.executable, "auth", "status"],
                        capture_output=True,
                        timeout=2.5,
                        check=False,
                    )
                    login.authenticated = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    login.authenticated = False
                login.auth_checked = True
            return {
                "login_id": login_id,
                "status": "running" if login.process.poll() is None else "exited",
                "output": output.decode("utf-8", errors="replace"),
                "offset": login.output_offset,
                "exit_code": login.process.poll(),
                "authenticated": login.authenticated,
            }

    def send(self, login_id: str, value: str) -> None:
        encoded = value.encode("utf-8")
        if len(encoded) > _MAX_INPUT or "\x00" in value:
            raise ClaudeAuthError("invalid_input")
        with self._lock:
            login = self._current(login_id)
            self._collect_locked()
            if login.process.poll() is not None:
                raise ClaudeAuthError("login_finished")
            try:
                os.write(login.master_fd, encoded)
            except OSError as exc:
                raise ClaudeAuthError("login_unavailable") from exc

    def cancel(self, login_id: str) -> None:
        with self._lock:
            login = self._current(login_id)
            self._terminate(login)
            self._dispose_locked(login)

    def shutdown(self) -> None:
        with self._lock:
            if self._login is not None:
                self._terminate(self._login)
                self._dispose_locked(self._login)

    def _expire(self, login_id: str) -> None:
        with self._lock:
            login = self._login
            if login is not None and login.login_id == login_id:
                self._terminate(login)
                self._dispose_locked(login)

    def _terminate(self, login: _Login) -> None:
        if login.process.poll() is not None:
            return
        try:
            os.killpg(login.process.pid, signal.SIGTERM)
            login.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(login.process.pid, signal.SIGKILL)
            except OSError:
                pass

    def _current(self, login_id: str) -> _Login:
        login = self._login
        if login is None or login.login_id != login_id:
            raise ClaudeAuthError("login_not_found")
        if time.monotonic() - login.started_at > _MAX_LIFETIME_SEC:
            self._terminate(login)
            self._dispose_locked(login)
            raise ClaudeAuthError("login_expired")
        return login

    def _collect_locked(self) -> None:
        login = self._login
        if login is None:
            return
        try:
            while True:
                chunk = os.read(login.master_fd, 4096)
                if not chunk:
                    break
                login.output.extend(chunk)
                login.output_offset += len(chunk)
                if len(login.output) > _MAX_OUTPUT:
                    del login.output[:-_MAX_OUTPUT]
        except (BlockingIOError, OSError):
            pass
        if time.monotonic() - login.started_at > _MAX_LIFETIME_SEC:
            self._terminate(login)
            self._dispose_locked(login)

    def _dispose_locked(self, login: _Login) -> None:
        if self._login is login:
            self._login = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        try:
            os.close(login.master_fd)
        except OSError:
            pass


claude_auth_session = ClaudeAuthSession()
