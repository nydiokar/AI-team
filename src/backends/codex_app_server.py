"""Private stdio runtime for the one Codex backend.

Owns exactly one ``codex app-server`` process per ``CodexBackend`` carrier:
process-tree lifetime, JSON-RPC framing, request correlation, bounded buffers,
and event routing. It has no Session, Task, database, retry, or business-policy
knowledge. Unknown backend notifications are intentionally ignored here.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading

from pydantic import JsonValue

MAX_FRAME = 4 * 1024 * 1024
MAX_BUFFER = 16 * 1024 * 1024
MAX_PENDING = 32
RPC_TIMEOUT = 30.0
REQUEST_METHODS = frozenset({
    "initialize", "thread/start", "thread/resume", "thread/unsubscribe",
    "thread/compact/start", "turn/start", "turn/interrupt", "model/list",
})
CONSUMED_NOTIFICATIONS = frozenset({
    "turn/started", "turn/completed", "thread/tokenUsage/updated",
    "item/started", "item/completed",
})


def _create_windows_job(process: subprocess.Popen[bytes]) -> int:
    """Own the full native runtime tree until this client closes it."""
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [("per_process_user_time_limit", ctypes.c_longlong),
                    ("per_job_user_time_limit", ctypes.c_longlong),
                    ("limit_flags", wintypes.DWORD),
                    ("minimum_working_set_size", ctypes.c_size_t),
                    ("maximum_working_set_size", ctypes.c_size_t),
                    ("active_process_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t),
                    ("priority_class", wintypes.DWORD),
                    ("scheduling_class", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "read_operation_count", "write_operation_count", "other_operation_count",
            "read_transfer_count", "write_transfer_count", "other_transfer_count")]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [("basic_limit_information", BasicLimitInformation),
                    ("io_info", IoCounters),
                    ("process_memory_limit", ctypes.c_size_t),
                    ("job_memory_limit", ctypes.c_size_t),
                    ("peak_process_memory_used", ctypes.c_size_t),
                    ("peak_job_memory_used", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = ExtendedLimitInformation()
    info.basic_limit_information.limit_flags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(job, process._handle):
        kernel32.CloseHandle(job)
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    return int(job)


def _close_windows_job(job: int) -> None:
    import ctypes
    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)


class CodexProtocolError(RuntimeError):
    """Protocol/transport failure; possibly executed calls must not be replayed."""


class CodexRPCError(CodexProtocolError):
    def __init__(self, error: dict[str, JsonValue]) -> None:
        super().__init__("codex_rpc_rejected")
        self.error = error


class EventChannel:
    def __init__(self, client: CodexAppServerClient) -> None:
        self.client = client
        self.events: queue.Queue[tuple[dict[str, JsonValue], int]] = queue.Queue(256)

    def get(self, timeout: float = 0.1) -> dict[str, JsonValue]:
        try:
            event, size = self.events.get(timeout=timeout)
        except queue.Empty:
            self.client.check()
            raise
        with self.client.lock:
            self.client.buffered -= size
        return event


class CodexAppServerClient:
    def __init__(self, executable: str, env: dict[str, str]) -> None:
        self.executable = executable
        self.env = env
        self.lock = threading.RLock()
        self.shutdown_lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.failure: str = ""
        self.pending: dict[int, queue.Queue[dict[str, JsonValue]]] = {}
        self.channels: dict[str, EventChannel] = {}
        self.buffered: int = 0
        self.sequence: int = 0
        self.writes: queue.Queue[bytes | None] = queue.Queue(MAX_PENDING)
        self.readers: list[threading.Thread] = []
        self.stderr: bytearray = bytearray()
        self.windows_job: int | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--stdio"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        if os.name == "nt":
            try:
                self.windows_job = _create_windows_job(self.process)
            except OSError as exc:
                self.process.terminate()
                self.process.wait(timeout=2)
                self.process = None
                raise CodexProtocolError("codex_runtime_ownership_unavailable") from exc
        for name, target in (("reader", self._read), ("writer", self._write),
                             ("stderr", self._read_stderr)):
            thread = threading.Thread(target=target, name=f"codex-app-server-{name}", daemon=True)
            self.readers.append(thread)
            thread.start()
        try:
            self.request("initialize", {"clientInfo": {"name": "ai-team", "version": "1"},
                                        "capabilities": {"experimentalApi": False}})
            self.writes.put_nowait(b'{"method":"initialized","params":{}}\n')
        except Exception:
            self.close()
            raise

    def check(self) -> None:
        if self.failure:
            raise CodexProtocolError(self.failure)
        if self.process is None or self.process.poll() is not None:
            raise CodexProtocolError("codex_runtime_lost")

    def attach_thread(self, thread_id: str, cwd: str, model: str | None,
                      config: dict[str, JsonValue]) -> dict[str, JsonValue]:
        params: dict[str, JsonValue] = {"cwd": cwd, "model": model,
            "approvalPolicy": "never", "sandbox": "danger-full-access", "config": config}
        if thread_id:
            params.update({"threadId": thread_id, "excludeTurns": True})
        return self.request("thread/resume" if thread_id else "thread/start", params)

    def start_turn(self, thread_id: str, message: str, cwd: str,
                   model: str | None, effort: str | None) -> dict[str, JsonValue]:
        return self.request("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": message}], "cwd": cwd,
            "model": model, "effort": effort})

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5)

    def compact(self, thread_id: str) -> None:
        self.request("thread/compact/start", {"threadId": thread_id})

    def unload(self, thread_id: str) -> None:
        self.request("thread/unsubscribe", {"threadId": thread_id})

    def list_models(self) -> dict[str, JsonValue]:
        return self.request("model/list", {"includeHidden": False, "limit": 1000})

    def subscribe(self, thread_id: str) -> EventChannel:
        with self.lock:
            self.check()
            if thread_id in self.channels:
                raise CodexProtocolError("codex_thread_busy")
            if len(self.channels) >= 8:
                raise CodexProtocolError("codex_capacity_exceeded")
            channel = EventChannel(self)
            self.channels[thread_id] = channel
            return channel

    def unsubscribe(self, thread_id: str) -> None:
        with self.lock:
            channel = self.channels.pop(thread_id, None)
            if channel:
                while True:
                    try:
                        _, size = channel.events.get_nowait()
                        self.buffered -= size
                    except queue.Empty:
                        break

    def request(self, method: str, params: dict[str, JsonValue],
                timeout: float = RPC_TIMEOUT) -> dict[str, JsonValue]:
        with self.lock:
            self.check()
            if method not in REQUEST_METHODS or not isinstance(params, dict):
                raise CodexProtocolError("codex_invalid_client_request")
            if len(self.pending) >= MAX_PENDING:
                raise CodexProtocolError("codex_rpc_capacity_exceeded")
            self.sequence += 1
            request_id = self.sequence
            message = {"id": request_id, "method": method, "params": params}
            encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode()
            if len(encoded) > MAX_FRAME:
                raise CodexProtocolError("codex_request_too_large")
            reply: queue.Queue[dict[str, JsonValue]] = queue.Queue(1)
            self.pending[request_id] = reply
            try:
                self.writes.put_nowait(encoded)
            except queue.Full as exc:
                self.pending.pop(request_id)
                raise CodexProtocolError("codex_rpc_capacity_exceeded") from exc
        try:
            response = reply.get(timeout=timeout)
            self.check()
            if "error" in response:
                error = response["error"]
                if not isinstance(error, dict) or not isinstance(error.get("code"), int):
                    raise CodexProtocolError("codex_invalid_rpc_error")
                raise CodexRPCError(error)
            result = response.get("result")
            if not isinstance(result, dict):
                raise CodexProtocolError("codex_invalid_rpc_result")
            return result
        except queue.Empty as exc:
            self._fail("codex_rpc_deadline_exceeded")
            raise CodexProtocolError(self.failure) from exc
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def _fail(self, reason: str) -> None:
        with self.lock:
            self.failure = self.failure or reason
            for reply in self.pending.values():
                try:
                    reply.put_nowait({})
                except queue.Full:
                    pass

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        event: object = None
        try:
            while True:
                raw = self.process.stdout.readline(MAX_FRAME + 1)
                if not raw:
                    raise CodexProtocolError("codex_runtime_lost")
                if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                    raise CodexProtocolError("codex_invalid_frame_size")
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise CodexProtocolError("codex_invalid_envelope")
                with self.lock:
                    if "id" in event:
                        if "method" in event:
                            # No adapter authority to grant approval or answer user input.
                            raise CodexProtocolError("codex_unsupported_server_request")
                        request_id = event["id"]
                        if type(request_id) is not int or request_id not in self.pending:
                            raise CodexProtocolError("codex_unmatched_response")
                        self.pending[request_id].put_nowait(event)
                        continue
                    method = event.get("method")
                    if method not in CONSUMED_NOTIFICATIONS:
                        continue
                    params = event.get("params", {})
                    if not isinstance(params, dict) or not isinstance(params.get("threadId"), str):
                        raise CodexProtocolError(f"codex_invalid_native_event:{method}")
                    channel = self.channels.get(params["threadId"])
                    if channel:
                        if self.buffered + len(raw) > MAX_BUFFER:
                            raise CodexProtocolError("codex_event_buffer_exceeded")
                        channel.events.put_nowait((event, len(raw)))
                        self.buffered += len(raw)
        except Exception as exc:
            if isinstance(exc, CodexProtocolError):
                self._fail(str(exc))
            else:
                method = event.get("method") if isinstance(event, dict) else None
                suffix = method if isinstance(method, str) else "unknown"
                self._fail(f"codex_invalid_native_event:{suffix}")

    def _write(self) -> None:
        assert self.process is not None and self.process.stdin is not None
        try:
            while True:
                data = self.writes.get()
                if data is None:
                    return
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (OSError, ValueError):
            self._fail("codex_runtime_write_failed")

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while data := self.process.stderr.read(4096):
                with self.lock:
                    self.stderr.extend(data)
                    del self.stderr[:-65536]
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        with self.shutdown_lock:
            self._fail("codex_runtime_closed")
            process = self.process
            if process is not None:
                # Own process group includes native tool/MCP descendants, even if
                # app-server itself already died. Never signal a foreign runtime.
                try:
                    if process.stdin:
                        process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                if os.name == "nt":
                    if process.poll() is None:
                        try:
                            process.send_signal(signal.CTRL_BREAK_EVENT)
                        except (OSError, ProcessLookupError, SystemError):
                            pass
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
                elif process.poll() is None:
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        try:
                            os.killpg(process.pid, sig)
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            continue
                try:
                    self.writes.put_nowait(None)
                except queue.Full:
                    while not self.writes.empty():
                        self.writes.get_nowait()
                    self.writes.put_nowait(None)
                for reader in self.readers:
                    if reader is not threading.current_thread():
                        reader.join(timeout=3)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe:
                        try:
                            pipe.close()
                        except BrokenPipeError:
                            # The server may have exited after its process group
                            # was signalled; its stdin is no longer writable.
                            pass
                if any(reader.is_alive() for reader in self.readers):
                    raise CodexProtocolError("codex_reader_shutdown_incomplete")
                if self.windows_job is not None:
                    _close_windows_job(self.windows_job)
                    self.windows_job = None
                self.process = None
