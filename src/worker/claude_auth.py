"""Bounded, allowlisted backend login sessions owned by a worker process.

Only fixed provider login and status commands are started. PTYs let an operator
complete each provider's remote browser/device flow without exposing a shell.
"""
from __future__ import annotations

import json
import os
import pty
import select
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
_STATUS_TIMEOUT_SEC = 4.0

_BACKENDS: dict[str, tuple[tuple[str, ...], tuple[str, ...], bool]] = {
    "claude": (("auth", "login"), ("auth", "status"), True),
    "codex": (("login", "--device-auth"), ("login", "status"), False),
}


@dataclass
class _Login:
    backend: str
    login_id: str
    executable: str
    process: subprocess.Popen[bytes]
    master_fd: int
    started_at: float
    output: bytearray
    output_offset: int
    auth_checked: bool
    authenticated: bool | None


class BackendAuthError(RuntimeError):
    """Stable failure for a worker-owned provider login handoff."""


class BackendAuthSession:
    """Own at most one bounded, allowlisted backend login PTY at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status_slots = threading.BoundedSemaphore(2)
        self._login: _Login | None = None
        self._timer: threading.Timer | None = None

    def status(self, backend: str) -> dict[str, str]:
        spec = _BACKENDS.get(backend)
        if spec is None:
            raise BackendAuthError("backend_unsupported")
        executable = shutil.which(backend)
        if not executable:
            return {"backend": backend, "state": "unavailable"}
        if not self._status_slots.acquire(blocking=False):
            return {"backend": backend, "state": "unavailable"}
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [executable, *spec[1]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )
            assert process.stdout is not None
            output = bytearray()
            deadline = time.monotonic() + _STATUS_TIMEOUT_SEC
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    return {"backend": backend, "state": "unavailable"}
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > _MAX_OUTPUT:
                    return {"backend": backend, "state": "unavailable"}
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            status_output = output.decode("utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return {"backend": backend, "state": "unavailable"}
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()
            self._status_slots.release()

        if backend == "claude":
            try:
                payload = json.loads(status_output)
                logged_in = payload.get("loggedIn") if isinstance(payload, dict) else None
                if isinstance(logged_in, bool):
                    return {"backend": backend, "state": "signed_in" if logged_in else "signed_out"}
            except (ValueError, TypeError):
                pass
        elif returncode == 0 and status_output.strip().casefold().startswith("logged in using "):
            return {"backend": backend, "state": "signed_in"}
        elif returncode != 0 and status_output.strip().casefold() == "not logged in":
            return {"backend": backend, "state": "signed_out"}
        return {"backend": backend, "state": "unavailable"}

    def start(self, backend: str = "claude") -> dict[str, str | bool]:
        with self._lock:
            spec = _BACKENDS.get(backend)
            if spec is None:
                raise BackendAuthError("backend_unsupported")
            self._collect_locked()
            if self._login is not None and self._login.process.poll() is not None:
                self._dispose_locked(self._login)
            if self._login is not None:
                if self._login.backend == backend:
                    return {"login_id": self._login.login_id, "backend": backend,
                            "status": "running", "input_supported": spec[2]}
                raise BackendAuthError("login_in_progress")
            executable = shutil.which(backend)
            if not executable:
                raise BackendAuthError("backend_missing")
            try:
                master_fd, slave_fd = pty.openpty()
            except OSError as exc:
                raise BackendAuthError("pty_unavailable") from exc
            try:
                process = subprocess.Popen(
                    [executable, *spec[0]],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception as exc:
                os.close(master_fd)
                os.close(slave_fd)
                raise BackendAuthError("backend_start_failed") from exc
            os.close(slave_fd)
            try:
                os.set_blocking(master_fd, False)
            except OSError as exc:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait(timeout=2)
                os.close(master_fd)
                raise BackendAuthError("pty_unavailable") from exc
            login_id = uuid.uuid4().hex
            self._login = _Login(
                backend=backend,
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
            return {"login_id": login_id, "backend": backend, "status": "running",
                    "input_supported": spec[2]}

    def poll(self, login_id: str, offset: int) -> dict[str, object]:
        if offset < 0 or offset > 2**31 - 1:
            raise BackendAuthError("invalid_offset")
        with self._lock:
            login = self._current(login_id)
            self._collect_locked()
            login = self._current(login_id)
            if login.process.poll() is not None and not login.auth_checked:
                self._terminate(login)
                self._collect_locked()
                try:
                    result = self.status(login.backend)
                    login.authenticated = None if result["state"] == "unavailable" else result["state"] == "signed_in"
                except (OSError, subprocess.TimeoutExpired):
                    login.authenticated = None
                login.auth_checked = True
            first_offset = login.output_offset - len(login.output)
            output = bytes(login.output[max(0, offset - first_offset):])
            return {
                "login_id": login_id,
                "backend": login.backend,
                "status": "running" if login.process.poll() is None else "exited",
                "output": output.decode("utf-8", errors="replace"),
                "offset": login.output_offset,
                "truncated": offset < first_offset,
                "exit_code": login.process.poll(),
                "authenticated": login.authenticated,
                "input_supported": _BACKENDS[login.backend][2],
            }

    def send(self, login_id: str, value: str) -> None:
        encoded = value.encode("utf-8")
        if len(encoded) > _MAX_INPUT or "\x00" in value:
            raise BackendAuthError("invalid_input")
        with self._lock:
            login = self._current(login_id)
            self._collect_locked()
            if not _BACKENDS[login.backend][2]:
                raise BackendAuthError("input_not_supported")
            if login.process.poll() is not None:
                raise BackendAuthError("login_finished")
            try:
                view = memoryview(encoded)
                deadline = time.monotonic() + 2.0
                while view:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [login.master_fd], [], remaining)[1]:
                        raise BackendAuthError("login_input_timeout")
                    try:
                        written = os.write(login.master_fd, view)
                    except BlockingIOError:
                        continue
                    if written == 0:
                        raise BackendAuthError("login_unavailable")
                    view = view[written:]
            except OSError as exc:
                raise BackendAuthError("login_unavailable") from exc

    def cancel(self, login_id: str) -> None:
        with self._lock:
            login = self._current(login_id)
            self._dispose_locked(login)

    def validate_backend(self, login_id: str, backend: str) -> None:
        with self._lock:
            if self._current(login_id).backend != backend:
                raise BackendAuthError("backend_mismatch")

    def shutdown(self) -> None:
        with self._lock:
            if self._login is not None:
                self._dispose_locked(self._login)

    def _expire(self, login_id: str) -> None:
        with self._lock:
            login = self._login
            if login is not None and login.login_id == login_id:
                self._dispose_locked(login)

    def _terminate(self, login: _Login) -> None:
        try:
            os.killpg(login.process.pid, signal.SIGTERM)
            login.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            # A CLI can exit before a child which ignored SIGTERM. Reap the
            # whole owned group even when the group leader is already gone.
            try:
                os.killpg(login.process.pid, signal.SIGKILL)
            except OSError:
                pass
            login.process.wait(timeout=2)

    def _current(self, login_id: str) -> _Login:
        login = self._login
        if login is None or login.login_id != login_id:
            raise ClaudeAuthError("login_not_found")
        if time.monotonic() - login.started_at > _MAX_LIFETIME_SEC:
            self._dispose_locked(login)
            raise ClaudeAuthError("login_expired")
        return login

    def _collect_locked(self) -> None:
        login = self._login
        if login is None:
            return
        try:
            for _ in range(32):
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
            self._dispose_locked(login)

    def _dispose_locked(self, login: _Login) -> None:
        self._terminate(login)
        if self._login is login:
            self._login = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        try:
            os.close(login.master_fd)
        except OSError:
            pass


backend_auth_session = BackendAuthSession()

# Compatibility aliases for older clients/tests while the home account surface
# and generic `/backend-auth` routes roll out.
ClaudeAuthError = BackendAuthError
ClaudeAuthSession = BackendAuthSession
claude_auth_session = backend_auth_session
