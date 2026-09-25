"""
Worker daemon — one per machine, managed by PM2.

Lifecycle:
  1. Read config from env (WORKER_NODE_ID, WORKER_TOKEN, etc.)
  2. POST /nodes/register
  3. Start nudge listener on WORKER_TAILSCALE_IP:WORKER_API_PORT
  4. Poll /tasks/pending with adaptive backoff (5s → 30s on empty, resets on task received)
  5. Claim → execute locally using existing src/backends/ → POST /tasks/{id}/result
  6. Heartbeat every 30s concurrently
  7. On SIGTERM: deregister, drain active tasks (up to 30s), exit

Run locally (no Tailscale required):
    WORKER_NODE_ID=main-pc WORKER_TOKEN=<token> WORKER_TAILSCALE_IP=127.0.0.1 \\
    CONTROLLER_URL=http://127.0.0.1:9002 WORKER_BACKENDS=claude,opencode \\
    python -m src.worker.agent
"""

import asyncio
import functools
import faulthandler
import json
import logging
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.control.turn_queue import CANCEL_MANAGED_ACTION, ManagedTurnOwnership

from src.core.process_utils import (
    WORKER_INCARNATION_ENV,
    WORKER_NODE_ENV,
    reap_stale_worker_children,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_REGISTRATION_TIMEOUT_SECONDS = 30
_REGISTRATION_RETRY_MAX_SECONDS = 30.0
_MODEL_CAPABILITIES_REFRESH_SECONDS = 24 * 60 * 60

logger = logging.getLogger(__name__)

# Safety bound on the output we store in the DB result, NOT a content-truncation
# knob. The old hard `[:4000]` cap silently lost the tail of long results before
# they ever reached the gateway, so the Telegram splitter had nothing to chunk
# (T2). We keep a large bound only so a runaway backend can't write hundreds of
# MB into a single DB row; the gateway's `_split_message` chunks the rest for
# delivery. Configurable via WORKER_MAX_OUTPUT_CHARS (0 / negative = unbounded).
def _max_output_chars() -> int:
    try:
        return int(os.getenv("WORKER_MAX_OUTPUT_CHARS") or 500_000)
    except ValueError:
        return 500_000


def _bound_output(text: str) -> str:
    """Apply the DB-sanity safety bound, marking the truncation when it bites."""
    limit = _max_output_chars()
    if limit <= 0 or len(text) <= limit:
        return text
    marker = f"\n\n[...output truncated at {limit} chars by WORKER_MAX_OUTPUT_CHARS]"
    return text[:limit] + marker


def _usage_from_execution_result(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        from src.services.result_text import extract_usage_from_ndjson
        return extract_usage_from_ndjson(getattr(raw, "raw_stdout", "") or "")
    except Exception:
        return None


def _tail_text(text: str, *, max_chars: int = 4000) -> str:
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= max_chars:
        return text
    return "..." + text[-max_chars:]


def _backend_error_detail(raw: Any) -> str:
    """Build a bounded diagnostic block for normal backend failure results."""
    parts: List[str] = []
    return_code = getattr(raw, "return_code", None)
    if return_code not in (None, 0):
        parts.append(f"exit_code={return_code}")
    error_class = getattr(raw, "error_class", "") or ""
    if error_class:
        parts.append(f"error_class={error_class}")
    stderr_tail = _tail_text(getattr(raw, "raw_stderr", "") or "")
    if stderr_tail:
        parts.append("stderr_tail:\n" + stderr_tail)
    stdout_tail = _tail_text(getattr(raw, "raw_stdout", "") or "")
    if stdout_tail:
        parts.append("stdout_tail:\n" + stdout_tail)
    return "\n\n".join(parts)[:4000]


# ---------------------------------------------------------------------------
# Job watcher helpers (T3)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Check if a process is still running.

    Uses `os.kill(pid, 0)` on Unix (signal 0 = test-only). On Windows,
    `os.kill(pid, 0)` raises OSError for non-existent processes but may also
    raise for access-denied on existing ones, so we fall back to CreateToolhelp32Snapshot.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_INFORMATION = 0x0400
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return ctypes.windll.kernel32.GetLastError() != 0x57  # ERROR_INVALID_PARAMETER
        except Exception:
            # Fallback: try CreateToolhelp32Snapshot
            try:
                import ctypes
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                TH32CS_SNAPPROCESS = 0x00000002
                snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
                if snapshot and snapshot != -1:
                    from ctypes import wintypes
                    class PROCESSENTRY32(ctypes.Structure):
                        _fields_ = [
                            ("dwSize", wintypes.DWORD),
                            ("cntUsage", wintypes.DWORD),
                            ("th32ProcessID", wintypes.DWORD),
                            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                            ("th32ModuleID", wintypes.DWORD),
                            ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD),
                            ("pcPriClassBase", ctypes.c_long),
                            ("dwFlags", wintypes.DWORD),
                            ("szExeFile", ctypes.c_char * 260),
                        ]
                    pe = PROCESSENTRY32()
                    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
                    if kernel32.Process32First(snapshot, ctypes.byref(pe)):
                        while True:
                            if pe.th32ProcessID == pid:
                                kernel32.CloseHandle(snapshot)
                                return True
                            if not kernel32.Process32Next(snapshot, ctypes.byref(pe)):
                                break
                    kernel32.CloseHandle(snapshot)
                return False
            except Exception:
                return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _unix_boot_time() -> Optional[float]:
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except Exception:
        return None
    return None


def _process_identity(pid: int) -> Dict[str, Any]:
    """Return best-effort process identity for a PID.

    The watcher uses this only as a safety check after restarts. PID existence
    alone is not enough because the OS may reuse a PID for an unrelated process.
    """
    if not _pid_alive(pid):
        return {"alive": False, "error": "pid not found"}

    if sys.platform == "win32":
        try:
            ps = (
                "Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" | "
                "Select-Object -First 1 ProcessId,CreationDate,CommandLine | "
                "ConvertTo-Json -Compress"
            ) % pid
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                creationflags=_NO_WINDOW,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return {"alive": True, "error": proc.stderr.strip() or "windows process query returned no data"}
            data = json.loads(proc.stdout)
            creation = data.get("CreationDate")
            started_epoch = None
            if creation:
                # CIM returns ISO-ish text on modern PowerShell; keep parsing
                # conservative and tolerate failure by leaving the field empty.
                try:
                    started_epoch = datetime.fromisoformat(str(creation).replace("Z", "+00:00")).timestamp()
                except Exception:
                    started_epoch = None
            return {
                "alive": True,
                "command": data.get("CommandLine") or "",
                "started_epoch": started_epoch,
                "error": "",
            }
        except Exception as e:
            return {"alive": True, "error": f"windows process query failed: {e}"}

    stat_path = Path(f"/proc/{pid}/stat")
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        stat = stat_path.read_text(encoding="utf-8", errors="replace")
        # Field 2 can contain spaces inside parentheses, so split after the
        # final ") ". starttime is field 22, index 19 in the remaining fields.
        fields = stat.rsplit(") ", 1)[1].split()
        start_ticks = int(fields[19])
        ticks_per_sec = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
        boot_time = _unix_boot_time()
        started_epoch = (boot_time + (start_ticks / ticks_per_sec)) if boot_time is not None else None
        raw_cmd = cmdline_path.read_bytes()
        command = raw_cmd.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        if not command:
            command = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
        return {
            "alive": True,
            "command": command,
            "started_epoch": started_epoch,
            "error": "",
        }
    except Exception as e:
        return {"alive": True, "error": f"unix process query failed: {e}"}


def _job_identity_mismatch(job: Dict[str, Any], identity: Dict[str, Any]) -> str:
    """Return a human-readable mismatch reason, or empty string when acceptable."""
    if not identity.get("alive"):
        return identity.get("error") or "pid not found"

    observed_started = identity.get("started_epoch")
    expected_started = job.get("last_seen_started_epoch")
    if expected_started is not None and observed_started is not None:
        try:
            if abs(float(observed_started) - float(expected_started)) > 1.0:
                return (
                    "pid start time changed: "
                    f"expected {expected_started}, observed {observed_started}"
                )
        except (TypeError, ValueError):
            pass

    expected_command = (job.get("last_seen_command") or "").strip()
    observed_command = (identity.get("command") or "").strip()
    if expected_command and observed_command and expected_command != observed_command:
        return (
            "pid command changed: "
            f"expected {expected_command[:180]!r}, observed {observed_command[:180]!r}"
        )

    return ""


def _read_log_tail(log_path: Optional[str], max_lines: int = 20, max_chars: int = 2000) -> str:
    """Read the last N lines of a log file for the completion notification."""
    if not log_path:
        return ""
    try:
        p = Path(log_path)
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.rstrip("\n").split("\n")
        tail = "\n".join(lines[-max_lines:])
        if len(tail) > max_chars:
            tail = "..." + tail[-max_chars:]
        return tail
    except Exception:
        return ""


def _collect_exit_code(pid: int) -> Optional[int]:
    """Try to get the exit code of a finished process on Windows.

    On Unix, waitpid can collect it. On Windows we use process handle
    if available, else return None (caller defaults to -1).
    """
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_INFORMATION = 0x0400
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
            if handle:
                exit_code = ctypes.c_uint32()
                ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                ctypes.windll.kernel32.CloseHandle(handle)
                return exit_code.value
        except Exception:
            pass
        return None
    try:
        _, status = os.waitpid(pid, os.WNOHANG)
        if status != 0:
            return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
    except (ChildProcessError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# HTTP helpers — stdlib only, no httpx/requests required
# ---------------------------------------------------------------------------

class _HTTP:
    """Minimal synchronous HTTP wrapper using urllib."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, data: Optional[bytes], timeout: int) -> Any:
        request_id = uuid.uuid4().hex[:12]
        headers = self._headers()
        headers["X-AI-Team-Request-ID"] = request_id
        req = urllib.request.Request(
            f"{self._base}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.warning(
                "event=controller_request_failed method=%s path=%s request_id=%s elapsed_ms=%.1f error_type=%s err=%s",
                method,
                path,
                request_id,
                (time.perf_counter() - started) * 1000,
                type(exc).__name__,
                exc,
            )
            raise

    def post(self, path: str, body: Any = None, timeout: int = 10) -> Any:
        data = json.dumps(body).encode() if body is not None else b""
        return self._request("POST", path, data=data, timeout=timeout)

    def get(self, path: str, params: Optional[Dict[str, str]] = None, timeout: int = 10) -> Any:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        request_path = url[len(self._base):]
        return self._request("GET", request_path, data=None, timeout=timeout)

    def get_bytes(self, path: str, timeout: int = 60) -> bytes:
        req = urllib.request.Request(
            f"{self._base}{path}",
            headers=self._headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def delete(self, path: str, timeout: int = 10) -> Any:
        req = urllib.request.Request(
            f"{self._base}{path}",
            headers=self._headers(),
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())


async def _post_result_until_accepted(
    http: _HTTP,
    path: str,
    payload: Dict[str, Any],
    *,
    label: str,
    max_runtime_sec: int = 600,
) -> bool:
    """Retry an idempotent terminal result POST during controller outages.

    Losing a result after successful backend execution leaves the gateway's
    task claimed forever until its dispatch timeout. The task-server terminal
    result endpoint is idempotent, so delivery can safely retry with bounded
    exponential backoff while the worker remains alive.
    """
    deadline = time.monotonic() + max(1, max_runtime_sec)
    delay_sec = 1.0
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            await asyncio.to_thread(http.post, path, payload, timeout=10)
            if attempt > 1:
                logger.info("event=%s_recovered attempts=%d", label, attempt)
            return True
        except Exception as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            logger.warning(
                "event=%s_failed attempt=%d retry_in=%.1fs err=%s",
                label, attempt, min(delay_sec, remaining), exc,
            )
            await asyncio.sleep(min(delay_sec, remaining))
            delay_sec = min(delay_sec * 2, 30.0)
    logger.error("event=%s_exhausted max_runtime_sec=%d", label, max_runtime_sec)
    return False


# ---------------------------------------------------------------------------
# Nudge listener — tiny asyncio HTTP server, just accepts POST /nudge
# ---------------------------------------------------------------------------

def _mark_nudge_received(
    poll_event: asyncio.Event,
    heartbeat_event: Optional[asyncio.Event] = None,
) -> None:
    """Wake worker loops that should react immediately to a gateway nudge."""
    poll_event.set()
    if heartbeat_event is not None:
        heartbeat_event.set()


async def _run_nudge_listener(
    host: str,
    port: int,
    poll_event: asyncio.Event,
    heartbeat_event: Optional[asyncio.Event] = None,
) -> None:
    """Accept POST /nudge and wake polling plus heartbeat state publication."""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.read(512), timeout=2)
            # Minimal method/path validation — avoid treating arbitrary TCP
            # probes (port scanners, health checks) as a real nudge, which
            # would reset the poll backoff and cause spurious tight polling.
            is_nudge = data.startswith(b"POST /nudge")
            if is_nudge:
                response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
                _mark_nudge_received(poll_event, heartbeat_event)
                logger.debug("event=nudge_received")
            else:
                response = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    try:
        server = await asyncio.start_server(_handler, host, port)
        logger.info("event=nudge_listener_started host=%s port=%d", host, port)
        async with server:
            await server.serve_forever()
    except Exception as e:
        logger.warning("event=nudge_listener_failed err=%s", e)


# ---------------------------------------------------------------------------
# Backend instantiation
# ---------------------------------------------------------------------------

def _make_backends() -> Dict[str, Any]:
    from src.backends.registry import build_backends
    return build_backends()


def _discover_node_models(backends: List[str], instances: Optional[Dict[str, Any]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Discover model descriptors from the CLIs installed on this worker.

    The gateway must not invent or merge model names: an empty result for a
    backend the node runs means that backend did not advertise a usable
    catalog and the UI should fall back to the static gateway catalog for it
    (see control_api.api_models). Codex is the only backend with a live,
    node-specific catalog (its account/model set is discovered through the
    local app-server); claude and opencode are global/config-driven, so their
    entries here just mirror the static catalog for parity with the gateway
    fallback and so every backend the node runs gets an advertised key.
    """
    from config.models import options as _static_options

    discovered: Dict[str, List[Dict[str, Any]]] = {}
    if "codex" in backends:
        try:
            runtime = (instances or {}).get("codex")
            rows = runtime.list_models() if runtime is not None else []
            configured_default = (os.getenv("CODEX_DEFAULT_MODEL") or "").strip()
            model_names = {row.get("model") or row.get("id") for row in rows if isinstance(row, dict)}
            use_configured_default = configured_default in model_names
            discovered["codex"] = [
                {"name": name, "is_default": (name == configured_default if use_configured_default else bool(row.get("isDefault"))),
                 "efforts": [entry.get("reasoningEffort") for entry in row.get("supportedReasoningEfforts", [])
                             if isinstance(entry, dict) and isinstance(entry.get("reasoningEffort"), str)]}
                for row in rows
                if isinstance((name := row.get("model") or row.get("id")), str) and name.strip()
            ]
        except Exception:
            logger.warning("event=node_model_discovery_failed backend=codex", exc_info=True)
            discovered["codex"] = []
    for static_backend in ("claude", "opencode", "opencode-server"):
        if static_backend in backends:
            discovered[static_backend] = [
                {"name": o.name, "is_default": o.is_default, "efforts": list(o.supported_efforts or [])}
                for o in _static_options(static_backend)
            ]
    return discovered


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

def _make_session_from_payload(payload: Dict[str, Any]) -> Any:
    """Reconstruct a Session-like object from the task payload."""
    from src.services import SessionStore
    from src.core.interfaces import Session, SessionStatus

    session_dict = payload.get("session")
    if not session_dict:
        return None

    # Build minimal Session
    session = Session(
        session_id=session_dict.get("session_id", ""),
        backend=session_dict.get("backend", "claude"),
        repo_path=session_dict.get("repo_path", ""),
        status=SessionStatus.BUSY,
        created_at=session_dict.get("created_at", datetime.now(tz=timezone.utc).isoformat()),
        updated_at=datetime.now(tz=timezone.utc).isoformat(),
        machine_id=session_dict.get("machine_id", ""),
        backend_session_id=session_dict.get("backend_session_id", ""),
        model=session_dict.get("model") or None,
        effort=session_dict.get("effort") or None,
    )
    # Copy optional fields if present. `case_role` is load-bearing: the claude
    # driver's `_role_boot` reads it to apply the Manager role prompt + scoped
    # manager tools on THIS node — dropping it made a node-pinned Manager boot as
    # a bare, role-less session (the A43 carrier-coupling defect). `role_boot`
    # must likewise travel: it is the Worker-role tier opt-in `_role_boot` reads,
    # so a node-pinned role-ful worker boots with the Worker role instead of
    # role-less (same carrier-coupling class as `case_role`).
    for attr in ("telegram_chat_id", "telegram_thread_id", "owner_user_id", "last_user_message", "driver_type", "driver_status", "cache_health", "cache_unhealthy_count", "previous_backend_session_ids", "case_role", "current_case_id", "role_boot"):
        if attr in session_dict:
            setattr(session, attr, session_dict[attr])
    return session


# ---------------------------------------------------------------------------
# Task executor
# ---------------------------------------------------------------------------

async def _fetch_staged_file(
    staged: Dict[str, Any],
    payload: Dict[str, Any],
    http: "_HTTP",
) -> Optional[str]:
    """Fetch a staged file from the controller and save it into the session's uploads dir.

    Returns the local path string on success, None on failure.
    """
    file_id = staged.get("file_id", "")
    filename = staged.get("filename", "upload")
    if not file_id:
        logger.warning("event=staged_file_skip reason=missing_file_id")
        return None
    session = payload.get("session") or {}
    repo_path = session.get("repo_path", "")
    if not repo_path:
        logger.warning("event=staged_file_skip reason=no_repo_path file_id=%s", file_id)
        return None
    dest_dir = Path(repo_path) / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    try:
        file_bytes = await asyncio.to_thread(http.get_bytes, f"/files/{file_id}")
        dest.write_bytes(file_bytes)
        logger.info("event=staged_file_fetched file_id=%s dest=%s size=%d", file_id, dest, len(file_bytes))
    except Exception as e:
        logger.error("event=staged_file_fetch_failed file_id=%s err=%s", file_id, e)
        return None
    try:
        await asyncio.to_thread(http.delete, f"/files/{file_id}")
    except Exception as e:
        logger.warning("event=staged_file_cleanup_failed file_id=%s err=%s", file_id, e)
    return str(dest)


async def _execute_task(
    task_row: Dict[str, Any],
    backends: Dict[str, Any],
    http: Optional["_HTTP"] = None,
    telemetry_sink: Any = None,
    node_id: str = "",
    ownership: Any = None,
    on_process: Any = None,
) -> Dict[str, Any]:
    """Execute one task row from mesh_tasks. Returns an ExecutionResultPayload-compatible dict.

    [A82 Stage 3] ``ownership`` (a ``turn_queue.ManagedTurnOwnership``) marks a
    claimed protocol-1 row: its session turn goes ONLY through the backend
    interface ``CodingBackend.run_managed_turn`` — no backend-specific branching
    and no legacy fallback (an unsupported backend raises a typed refusal before
    anything runs). ``ownership=None`` is the unchanged legacy path."""
    managed = ownership is not None
    payload = task_row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    action = task_row.get("action", "run_oneoff")
    if managed and action not in ("create_session", "resume_session", "compact_session"):
        return {
            "success": False,
            "output": "",
            "errors": [f"managed turn refused: action {action!r} has no managed execution path"],
            "files_modified": [],
            "execution_time": 0.0,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 1,
            "error_class": "managed_unsupported",
        }

    # Fetch any file staged on the controller before backend execution
    staged = (payload.get("metadata") or {}).get("staged_file")
    if staged and http is not None:
        await _fetch_staged_file(staged, payload, http)

    # Repo inspection tasks: read-only (or commit) ops against the session's
    # repo, which lives on THIS worker. No backend needed — the gateway routes
    # these here precisely because it cannot touch the worker's filesystem.
    if action == "inspect":
        from src.services.inspect_ops import run_inspect_op
        meta = payload.get("metadata") or {}
        op = meta.get("op", "")
        repo_path = meta.get("repo_path", "") or (payload.get("session") or {}).get("repo_path", "")
        op_params = meta.get("params") or {}
        inspect_result = await asyncio.to_thread(run_inspect_op, op, repo_path, op_params)
        return {
            "success": "error" not in inspect_result,
            "output": "",
            "errors": [inspect_result["error"]] if "error" in inspect_result else [],
            "files_modified": [],
            "execution_time": 0.0,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 0 if "error" not in inspect_result else 1,
            "inspect": inspect_result,
        }

    # File-delivery-only tasks: no backend needed
    if action == "fetch_staged_file":
        return {
            "success": True,
            "output": f"File delivered to uploads/{(staged or {}).get('filename', '')}",
            "errors": [],
            "files_modified": [],
            "execution_time": 0.0,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 0,
        }

    if action == "cancel_codex":
        from src.backends.codex_ownership import CodexOwnership
        target = payload.get("target_task_id")
        if not isinstance(target, str) or not target or len(target) > 256:
            return {"success": False, "output": "", "errors": ["Invalid cancellation target"], "execution_time": 0.0}
        def request_cancel() -> None:
            CodexOwnership().request_cancel(target)
        try:
            await asyncio.to_thread(request_cancel)
        except Exception as exc:
            return {"success": False, "output": "", "errors": [str(exc)], "execution_time": 0.0}
        return {"success": True, "output": "Cancellation requested", "errors": [], "execution_time": 0.0}

    # Session close: the gateway dispatches this when a mesh session is /closed
    # so the OWNING worker actually tears down its pooled backend session (frees
    # the claude process). Previously remote /close was a no-op on the worker and
    # the process leaked. No slot needed — handled outside the semaphore.
    if action == "close_session":
        session = _make_session_from_payload(payload)
        closed = False
        if session is not None:
            backend = backends.get(session.backend or "claude")
            closer = getattr(backend, "close", None) if backend is not None else None
            if callable(closer):
                try:
                    await asyncio.to_thread(closer, session)
                    closed = True
                except Exception as e:
                    logger.warning(
                        "event=close_session_backend_failed session_id=%s err=%s",
                        getattr(session, "session_id", ""), e,
                    )
        logger.info(
            "event=close_session_handled session_id=%s closed=%s",
            getattr(session, "session_id", "") if session else "", closed,
        )
        return {
            "success": True,
            "output": "session closed" if closed else "no live session to close",
            "errors": [],
            "files_modified": [],
            "execution_time": 0.0,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 0,
        }

    backend_name = task_row.get("backend", "claude")
    backend = backends.get(backend_name)
    if backend is None:
        return {
            "success": False,
            "errors": [f"Backend {backend_name!r} not available on this worker"],
            "output": "",
            "files_modified": [],
            "execution_time": 0.0,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 1,
        }

    prompt = payload.get("prompt", "")
    start = time.monotonic()
    from src.control.telemetry_sink import NullTelemetrySink
    from src.core.telemetry import (
        EMITTER_PROCESS_INSTANCE_ID,
        TelemetryContext,
        build_event,
    )
    sink = telemetry_sink or NullTelemetrySink()
    session_payload = payload.get("session") or {}
    telemetry_meta = payload.get("telemetry") or {}
    turn_id = str(telemetry_meta.get("turn_id") or task_row.get("id") or "")
    session_id = str(telemetry_meta.get("session_id") or task_row.get("session_id") or "") or None
    # The gateway resolves the model (default → catalog) before dispatch and
    # always sends a concrete value here (see _session_dispatch_payload). Do
    # NOT re-resolve locally: this node's own env/config can diverge from the
    # gateway's, which previously let a node silently pick its own default.
    model = session_payload.get("model") or None
    context = (
        TelemetryContext.create(
            turn_id=turn_id,
            node_id=node_id or str(task_row.get("claimed_by") or "worker"),
            session_id=session_id,
            backend=backend_name,
            model=model,
            source="worker",
            attempt=int(telemetry_meta.get("attempt") or 1),
            spawn_reason=str(telemetry_meta.get("spawn_reason") or "initial"),
            retry_of_invocation_id=telemetry_meta.get("retry_of_invocation_id") or None,
        )
        if turn_id
        else None
    )

    def _emit(name: str, attributes: Dict[str, Any]) -> None:
        if context is None:
            return
        try:
            sink.emit(
                build_event(
                    name,
                    turn_id=context.turn_id,
                    session_id=context.session_id,
                    node_id=context.node_id,
                    emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                    source="worker",
                    invocation_id=context.invocation_id,
                    backend=context.backend,
                    model=context.model,
                    attributes=attributes,
                )
            )
        except Exception:
            logger.warning("event=worker_telemetry_emit_failed", exc_info=True)

    _emit(
        "invocation.created",
        {
            "attempt": context.attempt if context else 1,
            "spawn_reason": context.spawn_reason if context else "initial",
            "action": action,
            "retry_of_invocation_id": context.retry_of_invocation_id if context else None,
        },
    )
    _emit("invocation.started", {"action": action})

    try:
        from src.core.interfaces import ExecutionResult as _ER
        from src.core.backend_call import call_backend

        if managed and action == "compact_session":
            # [A82 Stage 4b] Managed compaction: ONLY through the interface
            # (no backend-name branching, no legacy fallback).
            session = _make_session_from_payload(payload)
            if session is None:
                raise ValueError("Session payload missing for session action")
            raw = await asyncio.to_thread(
                call_backend,
                functools.partial(backend.run_managed_compaction, on_process=on_process),
                session,
                ownership,
                telemetry_context=context,
                telemetry_sink=sink,
            )
        elif action in ("create_session", "resume_session"):
            session = _make_session_from_payload(payload)
            if session is None:
                raise ValueError("Session payload missing for session action")
            if managed:
                raw = await asyncio.to_thread(
                    call_backend,
                    functools.partial(backend.run_managed_turn, on_process=on_process),
                    session,
                    prompt or session.last_user_message or "",
                    ownership,
                    telemetry_context=context,
                    telemetry_sink=sink,
                )
            elif action == "create_session" or not session.backend_session_id:
                raw = await asyncio.to_thread(
                    call_backend,
                    backend.create_session,
                    session,
                    telemetry_context=context,
                    telemetry_sink=sink,
                )
            else:
                raw = await asyncio.to_thread(
                    call_backend,
                    backend.resume_session,
                    session,
                    prompt,
                    telemetry_context=context,
                    telemetry_sink=sink,
                )
        else:
            cwd = payload.get("metadata", {}).get("cwd", "")
            raw = await asyncio.to_thread(
                call_backend,
                backend.run_oneoff,
                cwd,
                prompt,
                telemetry_context=context,
                telemetry_sink=sink,
            )

        elapsed = time.monotonic() - start
        if isinstance(raw, _ER):
            usage = _usage_from_execution_result(raw)
            error_detail = "" if raw.success else _backend_error_detail(raw)
            errors = list(raw.errors or [])
            if not raw.success and (
                not errors
                or all(str(e).strip().lower() in {"task failed", "failed"} for e in errors)
            ):
                summary_bits = []
                if getattr(raw, "return_code", 0):
                    summary_bits.append(f"exit code {getattr(raw, 'return_code', 0)}")
                if error_detail:
                    first_detail_line = error_detail.splitlines()[0].strip()
                    if first_detail_line and first_detail_line not in summary_bits:
                        summary_bits.append(first_detail_line)
                errors = [
                    "Backend task failed"
                    + (f" ({'; '.join(summary_bits)})" if summary_bits else "")
                    + "; see error_detail for stdout/stderr tail"
                ]
            _emit(
                "invocation.completed",
                {
                    "status": "success" if raw.success else "failed",
                    "duration_ms": round(elapsed * 1000),
                    "exit_code": getattr(raw, "return_code", 0),
                    "error_code": getattr(raw, "error_class", "") or None,
                },
            )
            try:
                sink.flush()
            except Exception:
                pass
            return {
                "success": raw.success,
                "output": _bound_output(raw.output or ""),
                "errors": errors,
                "files_modified": list(raw.files_modified or []),
                "execution_time": elapsed,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "return_code": getattr(raw, "return_code", 0),
                "error_detail": error_detail,
                # Ship the FULL backend transcript + stderr, not just the bounded
                # diagnostic: the gateway persists raw_stdout into the artifact,
                # so an error turn must not lose the agent's complete payload
                # (previously only a 4k error_detail made it over the wire and
                # the gateway mirrored raw_stdout=output, hiding the rest).
                "raw_stdout": _bound_output(getattr(raw, "raw_stdout", "") or ""),
                "raw_stderr": _bound_output(getattr(raw, "raw_stderr", "") or ""),
                "error_class": getattr(raw, "error_class", "") or "",
                "backend_session_id": raw.backend_session_id or "",
                "driver_type": getattr(session, "driver_type", "") if action in ("create_session", "resume_session") else "",
                "driver_status": getattr(session, "driver_status", "") if action in ("create_session", "resume_session") else "",
                "cache_health": getattr(session, "cache_health", "unknown") if action in ("create_session", "resume_session") else "unknown",
                "cache_unhealthy_count": int(getattr(session, "cache_unhealthy_count", 0) or 0) if action in ("create_session", "resume_session") else 0,
                "previous_backend_session_ids": list(getattr(session, "previous_backend_session_ids", []) or []) if action in ("create_session", "resume_session") else [],
                "usage": usage,
                "telemetry_invocation_id": context.invocation_id if context else "",
            }
        # Fallback for legacy return types
        _emit(
            "invocation.completed",
            {"status": "success", "duration_ms": round(elapsed * 1000), "exit_code": 0},
        )
        try:
            sink.flush()
        except Exception:
            pass
        return {
            "success": True,
            "output": _bound_output(str(raw)),
            "errors": [],
            "files_modified": [],
            "execution_time": elapsed,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 0,
            "telemetry_invocation_id": context.invocation_id if context else "",
        }
    except Exception as e:
        elapsed = time.monotonic() - start
        import traceback as _tb
        from src.core.observability import emit_event
        detail = _tb.format_exc()
        error_class = type(e).__name__
        concise = f"{error_class}: {e}"
        _emit(
            "invocation.completed",
            {
                "status": "failed",
                "duration_ms": round(elapsed * 1000),
                "exit_code": 1,
                "error_code": error_class,
            },
        )
        try:
            sink.flush()
        except Exception:
            pass
        task_id = task_row.get("id")
        # Full traceback to the worker log (not just str(e)) so failures are
        # actually diagnosable — this is the core D2 fix.
        logger.error("task_failed error=%s\n%s", concise, detail)
        emit_event(
            "task_failed",
            task_id=task_id,
            error=concise,
            error_class=error_class,
            error_detail=detail[:4000],
            backend=backend_name,
        )
        return {
            "success": False,
            "output": "",
            "errors": [concise],
            "error_detail": detail[:4000],
            "files_modified": [],
            "execution_time": elapsed,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 1,
            "telemetry_invocation_id": context.invocation_id if context else "",
            **({"error_class": getattr(e, "code", "") or error_class} if managed else {}),
        }


# ---------------------------------------------------------------------------
# Live activity forwarder (remote worker → gateway SSE)
# ---------------------------------------------------------------------------

class _ActivityForwarder:
    """Best-effort background sender for live ``task_activity`` signals.

    A worker's granular pill labels ("Using Bash", "Thinking…") are written to
    *its own* events.ndjson. Under the controller/worker split (Docker) the two
    run in separate containers with separate volumes, so the controller's SSE
    never sees that file — the ONLY way a label reaches the UI is this explicit
    HTTP forward, which the controller re-emits into the feed the UI tails.

    Single daemon thread + bounded queue: never blocks the SDK stream thread and
    drops live signal under backpressure (durable telemetry is unaffected).
    Delivery is best-effort and deliberately NOT retried — a dropped label
    self-heals on the next event, and not retrying means no duplicate or stale
    labels reach the UI (so no dedupe is needed downstream). Failures are counted
    and logged (with throttling), never silently swallowed.
    """

    def __init__(self, http: "_HTTP", node_id: str, *, max_queue: int = 256) -> None:
        self._http = http
        self._node_id = node_id
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_queue)
        self._stats_lock = threading.Lock()
        self._sent = 0
        self._failed = 0
        self._dropped = 0
        self._consecutive_failures = 0
        self._degraded = False
        self._thread = threading.Thread(
            target=self._run, name="activity-forwarder", daemon=True
        )
        self._thread.start()

    def stats(self) -> Dict[str, int]:
        """Observable counters for tests/health — never silently invisible."""
        with self._stats_lock:
            return {
                "sent": self._sent,
                "failed": self._failed,
                "dropped": self._dropped,
                "consecutive_failures": self._consecutive_failures,
            }

    def offer(self, payload: Dict[str, Any]) -> None:
        """Observability forwarder hook — enqueue a task_activity event, else ignore."""
        if payload.get("event") != "task_activity":
            return
        body = {
            "node_id": self._node_id,
            "session_id": payload.get("session_id"),
            "task_id": payload.get("task_id"),
            "turn_id": payload.get("turn_id"),
            "label": payload.get("label"),
        }
        if not body["session_id"] and not body["task_id"]:
            return
        if not body["label"]:
            return
        try:
            self._q.put_nowait(body)
        except queue.Full:
            # Drop under backpressure; the pill self-heals on the next event.
            # Counted + logged (throttled) so a persistently full queue is visible.
            with self._stats_lock:
                self._dropped += 1
                dropped = self._dropped
            if dropped == 1 or dropped % 100 == 0:
                logger.warning(
                    "event=activity_forward_dropped reason=queue_full node_id=%s dropped_total=%d",
                    self._node_id, dropped,
                )

    def _run(self) -> None:
        while True:
            body = self._q.get()
            try:
                self._http.post("/events/activity", body, timeout=3)
            except Exception as e:
                # A broken control-plane path must be visible, not swallowed. Log
                # the first failure of a streak, then throttle; a full drop count
                # is retained in stats() regardless.
                with self._stats_lock:
                    self._failed += 1
                    self._consecutive_failures += 1
                    streak = self._consecutive_failures
                    failed_total = self._failed
                    self._degraded = True
                if streak == 1 or streak % 50 == 0:
                    logger.warning(
                        "event=activity_forward_failed node_id=%s err_class=%s consecutive=%d failed_total=%d",
                        self._node_id, type(e).__name__, streak, failed_total,
                    )
                continue
            with self._stats_lock:
                self._sent += 1
                sent_total = self._sent
                failed_total = self._failed
                was_degraded = self._degraded
                self._degraded = False
                self._consecutive_failures = 0
            if was_degraded:
                logger.info(
                    "event=activity_forward_recovered node_id=%s sent_total=%d failed_total=%d",
                    self._node_id, sent_total, failed_total,
                )


# ---------------------------------------------------------------------------
# Worker daemon
# ---------------------------------------------------------------------------

class WorkerAgent:
    def __init__(self) -> None:
        from src.worker.config import WorkerConfig
        self.cfg = WorkerConfig.from_env()
        self._http = _HTTP(self.cfg.controller_url, self.cfg.worker_token)
        from src.control.telemetry_sink import build_runtime_telemetry_sink
        self._telemetry_sink = build_runtime_telemetry_sink(
            node_id=self.cfg.node_id,
            base_url=self.cfg.controller_url,
            token=self.cfg.worker_token,
            logs_dir="logs",
            is_gateway=False,
        )
        try:
            replay = getattr(self._telemetry_sink, "replay_spool", None)
            if callable(replay):
                replay()
        except Exception:
            logger.warning("event=telemetry_spool_replay_failed", exc_info=True)
        self._backends = _make_backends()
        self._active: Dict[str, asyncio.Task] = {}   # task_id → asyncio.Task
        self._active_meta: Dict[str, Dict[str, Any]] = {}
        self._shutdown = asyncio.Event()
        self._poll_now = asyncio.Event()
        self._heartbeat_now = asyncio.Event()
        self._semaphore = asyncio.Semaphore(self.cfg.max_concurrent)
        self._codex_control_semaphore = asyncio.Semaphore(4)
        self._slots_used: int = 0  # semaphore-acquired count; differs from len(_active) which includes queued tasks
        self._inflight_sessions: set = set()  # session_ids with a claimed, executing turn task
        self._job_procs: Dict[str, subprocess.Popen] = {}  # job_id → Popen (kept alive for exit-code retrieval)
        self._canary = (os.getenv("WORKER_CANARY") or "").lower() in {"1", "true", "yes"}
        self._incarnation_id = uuid.uuid4().hex
        self._model_capabilities = _discover_node_models(self.cfg.backends, self._backends)
        self._model_capabilities_at = time.monotonic()
        # Stamp our node + incarnation into the environment so every backend child
        # we spawn (the Claude SDK `claude` process inherits os.environ) carries
        # both. A later worker boot reaps children stamped with OUR node id but a
        # different incarnation — node scoping keeps a co-located second worker's
        # children safe.
        os.environ[WORKER_NODE_ENV] = self.cfg.node_id
        os.environ[WORKER_INCARNATION_ENV] = self._incarnation_id
        self._activity_forwarder: Optional[_ActivityForwarder] = None
        # [A82 Stage 3] Managed (protocol-1) result spool + bookkeeping. Stored
        # under the carrier state dir (outside repo source); replayed on boot so a
        # delivered-but-unacked result survives restart (design §6, WRK04).
        # [A82 Stage 3 rework, m5] Lazy: with the managed flag OFF nothing is
        # constructed or created on disk — unless a previous managed run left
        # carrier state behind, which must still drain (design §3.16).
        self._init_managed_state()
        # task_ids currently spooled and awaiting a receipt-matched ack — these
        # retain session ownership even after the backend slot is returned (WRK06).
        self._pending_result_delivery: set = set()
        # [A82 Stage 3 rework] Managed attempt bookkeeping kept OUT of
        # `_active_meta` (which is published in heartbeat `active_task_details`):
        # the claim token is an execution credential and must never reach a
        # telemetry/status view. task_id -> {"claim_token", "status"}.
        self._managed_claims: Dict[str, Dict[str, Any]] = {}
        # Bounded result delivery (design §7: 2 concurrent deliveries) and the
        # set currently being delivered, so replay never double-posts.
        from src.worker.managed_result_spool import MAX_CONCURRENT_DELIVERIES
        self._result_delivery_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DELIVERIES)
        self._delivering: set = set()
        # Envelopes the server refused whose dead-letter move failed: skipped by
        # replay (never re-POSTed forever).
        self._delivery_parked: set = set()
        # Rate limit for re-probing held (no-proof) attempts against the server.
        self._held_probe_at: Dict[str, float] = {}
        self._held_probe_interval_sec: float = 60.0
        # Set when a managed result cannot be reconciled (oversize / disk
        # failure): stop claiming NEW managed turns (design §7).
        self._managed_claims_blocked: Optional[str] = None
        # Separate small bounded capacity for control cancellation (design §7);
        # already provided by `_codex_control_semaphore` — referenced by the
        # shutdown-release guard below.
        self._setup_activity_forwarding()
        self._setup_proactive_delivery()
        try:
            self._replay_result_spool()
        except Exception:
            logger.warning("event=managed_result_spool_replay_failed", exc_info=True)

    def _init_managed_state(self) -> None:
        """[A82 Stage 3 rework, m5] Construct the managed result spool + claim
        store only when the managed flag is ON, or when a previous managed run
        left carrier state that must still drain. Nothing is created on disk
        here (dirs are created lazily on first write)."""
        from src.worker.managed_result_spool import ManagedClaimStore, ManagedResultSpool

        state_dir = self._carrier_state_dir()
        if self._managed_enabled() or os.path.isdir(state_dir):
            self._result_spool = ManagedResultSpool(state_dir)
            self._claim_store = ManagedClaimStore(state_dir)
        else:
            self._result_spool = None
            self._claim_store = None

    def _carrier_state_dir(self) -> str:
        """Absolute path to this carrier's private state dir for the managed
        result spool. Prefers an explicit env override, else a per-node dir under
        the worker's logs root — never inside the repo source tree (design §6)."""
        base = os.getenv("WORKER_STATE_DIR") or os.path.join("logs", "carrier_state")
        node = getattr(self.cfg, "node_id", "") or "node"
        # Created lazily by the spool/claim store on first write (m5).
        return os.path.join(base, node)

    # ------------------------------------------------------------------
    # [A82 Stage 3] Worker scheduling + result bookkeeping (design §§5-6, §7)
    # ------------------------------------------------------------------
    def _is_already_scheduled(self, task_id: str) -> bool:
        """True if a fetched id is ALREADY scheduled / executing / awaiting
        result-delivery — so the poll loop must NOT create a second handler for
        it (WRK01; design §5). Consulted before ``create_task``. The current
        `_poll_loop` bug overwrote ``_active[task_id]`` unconditionally.
        """
        tid = task_id.get("id") if isinstance(task_id, dict) else task_id
        pending_delivery = getattr(self, "_pending_result_delivery", set())
        return (
            tid in self._active
            or tid in self._active_meta
            or tid in pending_delivery
        )

    def _should_schedule(self, task_id: str) -> bool:
        """True iff a fetched id is NOT already tracked and should get a handler
        (WRK01). The negation of :meth:`_is_already_scheduled`; consulted before
        ``create_task`` in the poll loop so an already-scheduled id is skipped."""
        return not self._is_already_scheduled(task_id)

    def _scheduling_capacity_available(self) -> bool:
        """True while scheduled+executing work is below 2x configured execution
        slots (WRK02; design §6). Acquire this bounded scheduling capacity BEFORE
        ``create_task`` — the backend semaphore alone only bounds concurrent
        backend calls, not the number of scheduled handlers.
        """
        try:
            slots = int(getattr(self.cfg, "max_concurrent", 0) or 0)
        except (TypeError, ValueError):
            slots = 0
        return len(self._active) < max(1, slots) * 2

    def _reserve_result_envelope(
        self, task_id: str, claim_token: str, nbytes: int
    ) -> Optional[Any]:
        """Reserve a single managed result-envelope allowance BEFORE start
        (WRK05; design §6). Returns a reservation, or ``None`` when the spool is
        full / the estimate is oversize — in which case the turn is left PENDING
        rather than run-and-discarded. Never truncates to claim success.
        """
        if self._result_spool is None:
            return None
        return self._result_spool.reserve(task_id, claim_token, nbytes)

    def _replay_result_spool(self) -> int:
        """Re-mark every durably-spooled managed result as pending delivery on
        boot (WRK04; design §6); ``run()`` then re-delivers them. Also drops
        crash-orphaned temp files (m4). Returns the count replayed."""
        if self._result_spool is None:
            return 0
        orphans = self._result_spool.clean_orphan_tmps()
        if self._claim_store is not None:
            orphans += self._claim_store.clean_orphan_tmps()
        if orphans:
            logger.warning("event=managed_spool_orphan_tmps_removed count=%d", orphans)
        replayed = 0
        for task_id, _claim_token, _envelope in self._result_spool.list_spooled():
            self._pending_result_delivery.add(task_id)
            replayed += 1
        if replayed:
            logger.info("event=managed_result_spool_replayed count=%d", replayed)
        return replayed

    def _prune_result_spool_on_receipt(
        self, task_id: str, claim_token: str, receipt: Any
    ) -> bool:
        """Remove a spooled managed result ONLY on a durable accepted/stale
        receipt that matches task AND token (WRK04b; design §6). A bare HTTP
        timeout or a 2xx with no matching receipt does NOT prune — the envelope
        survives for replay. On a match the attempt's obligation is complete:
        the delivery marker AND the durable claim record are cleared.
        """
        if self._result_spool is None:
            return False
        pruned = self._result_spool.prune_on_receipt(task_id, claim_token, receipt)
        if pruned:
            self._pending_result_delivery.discard(task_id)
            self._claim_forget(task_id)
        return pruned

    def _managed_shutdown_release_ok(self, task_row: Dict[str, Any]) -> bool:
        """Guard for graceful shutdown (WRK06; design §6/§7): may this task's
        ownership be released on shutdown?

        Only a CLAIMED, never-started managed attempt is releasable. A running
        or recovery-held turn, an attempt whose start outcome is unknown, and a
        result pending delivery all keep ownership (the durable claim record
        lets the next boot move them). Legacy protocol-0 rows keep their
        existing drain behavior.
        """
        tid = task_row.get("id")
        if tid in getattr(self, "_pending_result_delivery", set()):
            return False
        proto = task_row.get("queue_protocol", 0)
        status = str(task_row.get("status", "")).lower()
        if int(proto or 0) == 1:
            return status == "claimed"
        return True

    def _managed_enabled(self) -> bool:
        """[A82 Stage 3] Managed carrier flag (``WORKER_MANAGED_TURNS``, default
        OFF). OFF ⇒ legacy protocol-0 poll/claim only, byte-identical."""
        return bool(getattr(getattr(self, "cfg", None), "managed_turns", False))

    def _managed_backends(self) -> List[str]:
        """[A82 Stage 3 rework] Backends on this worker with a REAL managed
        execution path (``supports_managed_turns()``; today Claude on the SDK
        driver only). Codex/opencode have none, so they are never advertised and
        their managed rows are never polled/claimed here (fail closed)."""
        if not self._managed_enabled():
            return []
        out: List[str] = []
        for name in self.cfg.backends:
            probe = getattr((self._backends or {}).get(name), "supports_managed_turns", None)
            try:
                if callable(probe) and probe():
                    out.append(name)
            except Exception:
                logger.warning("event=managed_capability_probe_failed backend=%s", name, exc_info=True)
        return out

    # --- durable claim records (B2) ------------------------------------- #
    def _claim_record(self, task_id: str, **fields: Any) -> Dict[str, Any]:
        """Write-through update of the attempt record (memory + durable store).
        Raises ``ResultSpoolError`` if it cannot be persisted."""
        rec = dict(self._managed_claims.get(task_id) or {})
        rec.update(fields)
        if self._claim_store is not None:
            self._claim_store.put(task_id, rec)
        self._managed_claims[task_id] = rec
        return rec

    async def _drop_attempt(self, task_id: str) -> None:
        """[A82 Stage 3 rework 6] The server says this attempt is terminal / not
        ours (definitive refusal): drop the durable record AND any in-memory
        backend wait for its turn (abandon-and-forget), so the session can
        become quiescent again without a close/restart."""
        rec = dict(self._managed_claims.get(task_id) or {})
        if not rec and self._claim_store is not None:
            rec = self._claim_store.get(task_id) or {}
        self._claim_forget(task_id)
        turn_uuid = rec.get("turn_uuid")
        backend = (self._backends or {}).get(str(rec.get("backend") or ""))
        forget = getattr(backend, "forget_managed_turn", None)
        if turn_uuid and callable(forget):
            session = self._session_for(str(rec.get("session_id") or ""), str(rec.get("backend") or ""))
            try:
                await asyncio.to_thread(forget, session, turn_uuid)
            except Exception:
                logger.warning("event=managed_forget_turn_failed task_id=%s", task_id, exc_info=True)

    def _claim_forget(self, task_id: str) -> None:
        self._managed_claims.pop(task_id, None)
        if self._claim_store is not None:
            self._claim_store.remove(task_id)

    def _session_for(self, session_id: str, backend: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        session_dict = dict((payload or {}).get("session") or {})
        session_dict.setdefault("session_id", session_id)
        session_dict.setdefault("backend", backend)
        return _make_session_from_payload({"session": session_dict})

    async def _backend_quiescent(self, backend_name: str, session: Any) -> bool:
        backend = (self._backends or {}).get(backend_name)
        probe = getattr(backend, "is_quiescent", None)
        if not callable(probe) or session is None:
            return False
        try:
            return bool(await asyncio.to_thread(probe, session))
        except Exception:
            logger.warning("event=managed_quiescence_probe_failed backend=%s", backend_name, exc_info=True)
            return False

    def _is_definitive_refusal(self, exc: BaseException) -> bool:
        return (
            isinstance(exc, urllib.error.HTTPError)
            and 400 <= exc.code < 500
            and exc.code not in (408, 429)
        )

    async def _claim_and_start_managed(
        self, task_id: str
    ) -> Optional[Tuple[Dict[str, Any], str]]:
        """[A82 Stage 3 rework] Managed claim → persist → reserve → quiescence →
        fenced start.

        1. Claim via ``/claim-managed`` (fresh token; the frozen payload in the
           RESPONSE is what executes).
        2. Persist the attempt record durably BEFORE anything else (B2) — a
           crash from here on can always be reconciled at boot.
        3. Reserve one result envelope (M2 budget); none ⇒ release, stay pending.
        4. Session not quiescent ⇒ release BEFORE start (M2): the prompt stays
           pending and is retried; nothing terminal happens to it.
        5. ``/start-managed`` with bounded retries on transport errors (B1): the
           server returns the same authorization for a repeated start, so a lost
           response is recovered by retrying. Only a DEFINITIVE refusal permits
           release. If the outcome stays unknown the token is NEVER discarded:
           the attempt is released with the write-ahead "not invoked" attestation
           (running→pending allowed), or left durably for the reconciler.
        """
        try:
            claim_response = await asyncio.to_thread(
                self._http.post,
                f"/tasks/{task_id}/claim-managed",
                {
                    "node_id": self.cfg.node_id,
                    "carrier_kind": "worker_daemon",
                    "incarnation_id": self._incarnation_id,
                    "queue_protocols": [1],
                },
            )
        except urllib.error.HTTPError as e:
            if e.code == 409:
                logger.debug("managed_claim_race task_id=%s", task_id)
            else:
                logger.warning("managed_claim_failed task_id=%s err=%s", task_id, e)
            return None
        except Exception as e:
            logger.warning("managed_claim_failed task_id=%s err=%s", task_id, e)
            return None
        task_row = claim_response.get("task") if isinstance(claim_response, dict) else None
        claim_token = claim_response.get("claim_token") if isinstance(claim_response, dict) else None
        if not isinstance(task_row, dict) or not claim_token:
            logger.warning("managed_claim_malformed_response task_id=%s", task_id)
            return None
        from src.worker.managed_result_spool import ResultSpoolError

        try:
            self._claim_record(
                task_id, claim_token=claim_token, status="claimed", invoked=False,
                recovery_acked=False, session_id=str(task_row.get("session_id") or ""),
                backend=str(task_row.get("backend") or ""), incarnation_id=self._incarnation_id,
            )
        except ResultSpoolError:
            logger.error("event=managed_claim_persist_failed task_id=%s — not starting", task_id)
            self._managed_claims[task_id] = {"claim_token": claim_token, "status": "claimed", "invoked": False}
            await self._release_managed_claim(task_id, claim_token)
            return None
        if self._reserve_result_envelope(
            task_id, claim_token, self._result_spool.max_envelope_bytes
        ) is None:
            logger.warning(
                "event=managed_result_budget_exhausted task_id=%s — releasing the "
                "unstarted claim; turn stays pending", task_id,
            )
            await self._release_managed_claim(task_id, claim_token)
            return None
        session = self._session_for(
            str(task_row.get("session_id") or ""), str(task_row.get("backend") or ""),
            task_row.get("payload") if isinstance(task_row.get("payload"), dict) else None,
        )
        if not await self._backend_quiescent(str(task_row.get("backend") or ""), session):
            logger.info(
                "event=managed_session_not_quiescent task_id=%s — releasing before "
                "start; prompt stays pending", task_id,
            )
            await self._release_managed_claim(task_id, claim_token)
            return None
        body = {
            "node_id": self.cfg.node_id,
            "claim_token": claim_token,
            "incarnation_id": self._incarnation_id,
        }
        delays = tuple(getattr(self, "_start_retry_delays", (0.5, 1.0, 2.0)))
        for attempt in range(len(delays) + 1):
            try:
                await asyncio.to_thread(self._http.post, f"/tasks/{task_id}/start-managed", body)
                break
            except Exception as e:
                if self._is_definitive_refusal(e):
                    logger.warning("managed_start_refused task_id=%s err=%s", task_id, e)
                    await self._release_managed_claim(task_id, claim_token)
                    return None
                logger.warning(
                    "managed_start_unconfirmed task_id=%s attempt=%d err=%s",
                    task_id, attempt + 1, e,
                )
                if attempt < len(delays):
                    await asyncio.sleep(delays[attempt])
        else:
            # Start outcome unknown after bounded retries. The backend was never
            # invoked (write-ahead flag still False): release with that
            # attestation; if even that is unreachable the durable record stays
            # for the reconciler. The token is never dropped here.
            self._claim_record(task_id, status="start_unknown")
            await self._release_managed_claim(task_id, claim_token, not_invoked=True)
            return None
        self._claim_record(task_id, status="running")
        return task_row, claim_token

    def _record_backend_process(self, task_id: str, ident: Dict[str, Any]) -> None:
        """[A82 Stage 3 rework 4, B2] Persist the backend process identity for
        this attempt (called by the backend right before the prompt is
        submitted) — the only basis on which a successor carrier may claim the
        backend is gone."""
        if not isinstance(ident, dict) or not isinstance(ident.get("pid"), int):
            return
        try:
            self._claim_record(task_id, backend_pid=ident["pid"], backend_identity=dict(ident))
        except Exception:
            logger.warning("event=managed_backend_identity_persist_failed task_id=%s", task_id)

    async def _release_managed_claim(
        self, task_id: str, claim_token: str, *, not_invoked: bool = False
    ) -> str:
        """Release a managed attempt back to pending (current token only).
        ``not_invoked`` adds the carrier's write-ahead attestation that the
        backend never ran (lets a started row return to pending, prompt kept).
        Returns ``released`` / ``refused`` (definitive — the attempt is not ours
        to hold; record dropped) / ``unknown`` (transport — record KEPT)."""
        if self._result_spool is not None:
            self._result_spool.release_reservation(task_id, claim_token)
        try:
            await asyncio.to_thread(
                self._http.post,
                f"/tasks/{task_id}/release-managed",
                {"node_id": self.cfg.node_id, "claim_token": claim_token,
                 "incarnation_id": self._incarnation_id,
                 "backend_not_invoked": bool(not_invoked)},
            )
        except Exception as e:
            if self._is_definitive_refusal(e):
                logger.warning("managed_release_refused task_id=%s err=%s", task_id, e)
                self._claim_forget(task_id)
                return "refused"
            logger.warning("managed_release_unconfirmed task_id=%s err=%s", task_id, e)
            return "unknown"
        self._claim_forget(task_id)
        return "released"

    async def _enter_managed_recovery(self, task_id: str, claim_token: str, reason: str) -> bool:
        """[A82 Stage 3 rework] Move a STARTED managed turn to
        ``recovery_required`` via ``/enter-recovery`` (token-fenced; Stage-2
        ``enter_recovery``). Ownership stays held (durable record + drain guard)
        until the reconciler posts evidence or an operator resolves it. The
        unused result-envelope reservation is returned. A transport failure keeps
        ``recovery_acked=False`` so the poll-pass reconciler retries until acked;
        a definitive refusal (row already terminal / not ours) drops the record.
        Returns True iff the server acknowledged the recovery hold."""
        if self._result_spool is not None:
            self._result_spool.release_reservation(task_id, claim_token)
        try:
            self._claim_record(task_id, claim_token=claim_token, status="recovery_required",
                               reason=(reason or "")[:500], recovery_acked=False)
        except Exception:
            logger.error("event=managed_recovery_persist_failed task_id=%s", task_id, exc_info=True)
        try:
            await asyncio.to_thread(
                self._http.post,
                f"/tasks/{task_id}/enter-recovery",
                {"node_id": self.cfg.node_id, "claim_token": claim_token,
                 "incarnation_id": self._incarnation_id, "reason": (reason or "")[:500]},
            )
        except Exception as e:
            # MINOR-2: only a fenced 409 or a 404 ("no managed turn") is a
            # definitive answer here; 401/403/other 4xx are transient (auth /
            # proxy trouble) — keep the record and retry (rate-limited).
            if isinstance(e, urllib.error.HTTPError) and e.code in (404, 409):
                logger.warning("event=managed_enter_recovery_refused task_id=%s err=%s", task_id, e)
                if task_id not in self._pending_result_delivery:
                    await self._drop_attempt(task_id)
                return False
            logger.error(
                "event=managed_enter_recovery_failed task_id=%s err=%s — retried by "
                "the reconciler (ownership held)", task_id, e,
            )
            return False
        try:
            self._claim_record(task_id, recovery_acked=True)
        except Exception:
            logger.warning("event=managed_recovery_ack_persist_failed task_id=%s", task_id)
        # MINOR-2: a refused envelope (dead letter / parked) leaves the budget on
        # ANY acknowledged recovery — including a later reconciler pass.
        if self._result_spool is not None:
            self._result_spool.retire_dead_letter(task_id, claim_token)
            if task_id in self._delivery_parked:
                self._result_spool.discard(task_id, claim_token)
                self._delivery_parked.discard(task_id)
        logger.warning("event=managed_turn_recovery_required task_id=%s", task_id)
        return True

    async def _post_managed_result_once(
        self, task_id: str, claim_token: str, envelope: Dict[str, Any]
    ) -> bool:
        """POST one spooled envelope to ``/result-managed`` (atomic
        ``complete_turn`` on the server) and prune ONLY on a task+token-matched
        durable receipt. Transport error / 5xx / unmatched body ⇒ keep the spool
        and the ownership hold (retried next pass). A DEFINITIVE 4xx ⇒ the
        envelope moves to the bounded dead-letter dir and the attempt goes to
        recovery (m3) — never retried forever, never blocking replay order."""
        async with self._result_delivery_semaphore:
            try:
                receipt = await asyncio.to_thread(
                    self._http.post,
                    f"/tasks/{task_id}/result-managed",
                    envelope,
                    timeout=10,
                )
            except Exception as e:
                if self._is_definitive_refusal(e):
                    await self._retire_refused_result(task_id, claim_token, e)
                    return False
                logger.warning(
                    "event=managed_result_post_failed task_id=%s err=%s (spooled, "
                    "retained for replay)", task_id, e,
                )
                return False
        if self._prune_result_spool_on_receipt(task_id, claim_token, receipt):
            logger.info("event=managed_result_acked task_id=%s", task_id)
            return True
        logger.warning(
            "event=managed_result_unacked task_id=%s — receipt did not match "
            "task+token; spool retained for replay", task_id,
        )
        return False

    async def _retire_refused_result(self, task_id: str, claim_token: str, err: BaseException) -> None:
        """[m3/M2] The server DEFINITIVELY refused this envelope. It must never
        be re-POSTed forever: move it to the bounded dead-letter dir (or, if
        that is full/unwritable, park it in memory), stop delivering it, and put
        the attempt in recovery. Once the recovery hold is acknowledged — or the
        attempt is definitively not ours — the envelope leaves the budget."""
        logger.error(
            "event=managed_result_refused task_id=%s err=%s — dead-letter + recovery",
            task_id, err,
        )
        dead = self._result_spool.dead_letter(task_id, claim_token, str(err))
        if not dead:
            self._delivery_parked.add(task_id)
        self._pending_result_delivery.discard(task_id)
        acked = await self._enter_managed_recovery(
            task_id, claim_token, f"managed_result_refused: {err}"[:500],
        )
        if acked or task_id not in self._managed_claims:
            if dead:
                self._result_spool.retire_dead_letter(task_id, claim_token)
            else:
                self._result_spool.discard(task_id, claim_token)
                self._delivery_parked.discard(task_id)

    async def _deliver_managed_result(
        self, task_id: str, claim_token: str, result: Dict[str, Any]
    ) -> bool:
        """Spool a managed result BEFORE POST, deliver it to ``/result-managed``,
        and prune ONLY on a durable receipt matching task+token (design §6).
        Retains session ownership (``_pending_result_delivery``) until the receipt
        matches — a failed delivery is re-sent by :meth:`_redeliver_spooled_results`
        (next poll pass / boot replay). Oversize/disk failure raises, leaving a
        visible recovery obligation and blocking new managed claims (never a
        truncated false success). Returns True iff acknowledged."""
        from src.worker.managed_result_spool import OversizeResultError, ResultSpoolError

        envelope = {"node_id": self.cfg.node_id, "claim_token": claim_token, **result}
        # Mark ownership held for delivery even if the backend slot returns.
        self._pending_result_delivery.add(task_id)
        try:
            self._result_spool.commit(task_id, claim_token, envelope)
        except OversizeResultError:
            # The full backend artifact is preserved by existing artifact storage;
            # keep the ownership hold + a bounded reference, stop truncating.
            self._managed_claims_blocked = f"oversize_result:{task_id}"
            logger.error(
                "event=managed_result_oversize task_id=%s — holding recovery "
                "obligation, NOT claiming success; new managed claims stopped", task_id,
            )
            # [A82 Stage 3 rework] Bounded server-side diagnostic: the attempt
            # moves to recovery_required with a short reason (<=500 chars) —
            # never a truncated "success". Slot stays held.
            self._pending_result_delivery.discard(task_id)
            await self._enter_managed_recovery(
                task_id, claim_token,
                f"managed_result_oversize: serialized envelope exceeds "
                f"{self._result_spool.max_envelope_bytes} bytes; output_chars="
                f"{len(str(result.get('output') or ''))}",
            )
            raise
        except ResultSpoolError as e:
            self._managed_claims_blocked = f"spool_write_failed:{task_id}"
            logger.error(
                "event=managed_result_spool_write_failed task_id=%s — visible "
                "recovery obligation, no ack; new managed claims stopped", task_id,
            )
            self._pending_result_delivery.discard(task_id)
            await self._enter_managed_recovery(
                task_id, claim_token, f"managed_result_spool_write_failed: {e}"[:500],
            )
            raise
        self._delivering.add(task_id)
        try:
            return await self._post_managed_result_once(task_id, claim_token, envelope)
        finally:
            self._delivering.discard(task_id)

    async def _redeliver_spooled_results(self, batch: int = 8) -> int:
        """[A82 Stage 3 rework, M3] Re-deliver durably spooled, unacknowledged
        managed results (boot replay + retry of earlier failed deliveries).

        Bounded: at most ``batch`` envelopes per pass, at most
        ``MAX_CONCURRENT_DELIVERIES`` concurrent POSTs, and never one already in
        flight. Runs regardless of the managed flag — disabling new managed work
        must not abandon an accepted result (design §3.16). Returns acks."""
        if self._result_spool is None:
            return 0
        acked = 0
        items = self._result_spool.list_spooled(
            limit=batch, after=getattr(self, "_redeliver_cursor", None),
        )
        # M2: rotating cursor — a stuck head cannot starve later envelopes.
        self._redeliver_cursor = items[-1][0] if len(items) >= batch else None
        for task_id, claim_token, envelope in items:
            if task_id in self._delivering or task_id in self._delivery_parked:
                continue
            self._pending_result_delivery.add(task_id)
            self._delivering.add(task_id)
            try:
                if await self._post_managed_result_once(task_id, claim_token, envelope):
                    acked += 1
            finally:
                self._delivering.discard(task_id)
        return acked

    def _capture_late_managed_result(self, session_id: str, outcome: Any) -> bool:
        """[A82 Stage 3 rework, M3] The late real reply of a managed turn that
        hit its deadline (driver flags ``late_managed``). Spool it as that held
        attempt's result so the normal delivery path commits it atomically
        (``/result-managed`` → ``complete_turn`` accepts a recovery-held row).
        Runs on the driver's sink thread; returns True iff captured."""
        if self._result_spool is None or self._claim_store is None:
            return False
        # MAJOR-1: bind by the managed TURN identity, never by session — a stale
        # record for an earlier turn on the same session must not steal it.
        turn_uuid = getattr(outcome, "managed_turn_uuid", "") or ""
        if not turn_uuid:
            return False
        for rec in self._claim_store.list():
            if rec.get("turn_uuid") != turn_uuid or not rec.get("invoked"):
                continue
            if rec.get("session_id") and rec.get("session_id") != session_id:
                logger.error("event=managed_late_result_session_mismatch task_id=%s", rec.get("task_id"))
                return False
            tid, tok = str(rec.get("task_id")), str(rec.get("claim_token"))
            is_error = bool(getattr(outcome, "is_error", False))
            envelope = {
                "node_id": self.cfg.node_id,
                "claim_token": tok,
                "success": not is_error,
                "output": _bound_output(getattr(outcome, "output", "") or ""),
                "errors": [getattr(outcome, "error_text", "") or "backend error result"] if is_error else [],
                "error_class": getattr(outcome, "error_class", "") or "",
                "backend_session_id": getattr(outcome, "backend_session_id", "") or None,
                "raw_stdout": _bound_output(getattr(outcome, "raw_ndjson", "") or ""),
            }
            try:
                self._result_spool.commit(tid, tok, envelope)
            except Exception:
                logger.error("event=managed_late_result_spool_failed task_id=%s", tid, exc_info=True)
                return False
            self._pending_result_delivery.add(tid)
            logger.warning("event=managed_late_result_captured task_id=%s", tid)
            return True
        return False

    def _backend_process_gone(self, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """[A82 Stage 3 rework 5, B2/MINOR-3] Proof the recorded backend
        process is gone (``process_utils.process_gone_proof``: boot-relative
        /proc identity on Linux, psutil create_time+cmdline elsewhere), or None
        when there is no unambiguous proof (fail closed → operator route)."""
        from src.core.process_utils import process_gone_proof

        ident = rec.get("backend_identity")
        if not isinstance(ident, dict):
            return None
        return process_gone_proof(ident)

    async def _probe_held_attempt(self, tid: str, tok: str, rec: Dict[str, Any], why: str) -> bool:
        """Rate-limited server consultation for an attempt the carrier cannot
        resolve itself. Re-asserting the recovery hold is idempotent; a
        definitive 404/409 (row terminal / operator-resolved / not ours) drops
        the record and forgets the backend wait. Returns True iff dropped."""
        now = time.monotonic()
        last = self._held_probe_at.get(tid)
        if last is not None and now - last < self._held_probe_interval_sec:
            return False
        self._held_probe_at[tid] = now
        logger.warning("event=managed_recovery_held task_id=%s reason=%s", tid, why)
        await self._enter_managed_recovery(
            tid, tok, rec.get("reason") or f"carrier: held ({why})",
        )
        if tid not in self._managed_claims:
            self._held_probe_at.pop(tid, None)
            return True
        return False

    async def _reconcile_managed_claims(self, batch: int = 8) -> int:
        """[A82 Stage 3 rework, B2] Give every durably held managed attempt a
        live exit (boot + every poll pass, bounded batch, no background tasks).

        Skips attempts still being handled in this process or owned by the
        result-delivery path (a spooled result). For the rest:
          * never invoked (write-ahead flag) ⇒ release with the not-invoked
            attestation: back to pending, prompt preserved;
          * invoked, no result ⇒ ensure the recovery hold is acknowledged, then
            post carrier-provable stop evidence to ``/quiescence``:
            ``carrier_restarted`` for a previous incarnation's attempt (this
            process reaped its backend children at boot), or
            ``backend_quiescent`` once the backend's own oracle reports the
            session quiescent. Resolved ``failed``; the session's native id is
            left untouched. A definitive refusal (row resolved elsewhere, e.g.
            by an operator) drops the record.
        Transport failures leave the record for the next pass. Returns the
        number of attempts that reached a final state this pass."""
        if self._claim_store is None:
            return 0
        done = 0
        recs = self._claim_store.list(limit=batch, after=getattr(self, "_reconcile_cursor", None))
        # M2: rotating cursor — a stuck record can never starve later ones.
        self._reconcile_cursor = recs[-1].get("task_id") if len(recs) >= batch else None
        for rec in recs:
            tid = str(rec.get("task_id") or "")
            tok = str(rec.get("claim_token") or "")
            if not tid or not tok or tid in self._active or tid in self._delivering:
                continue
            if tid in self._pending_result_delivery:
                continue  # the result path owns it
            self._managed_claims.setdefault(tid, rec)
            if not rec.get("invoked"):
                if await self._release_managed_claim(tid, tok, not_invoked=True) != "unknown":
                    done += 1
                continue
            if not rec.get("recovery_acked"):
                ok = await self._enter_managed_recovery(
                    tid, tok, rec.get("reason") or "carrier: invoked attempt without a delivered result",
                )
                if not ok:
                    if tid not in self._managed_claims:
                        done += 1  # definitively refused ⇒ record dropped
                    continue
                # The server was just consulted; the held-record probe below
                # starts its rate-limit window now.
                self._held_probe_at[tid] = time.monotonic()
            proof: Optional[Dict[str, Any]] = None
            if rec.get("incarnation_id") != self._incarnation_id:
                # B2: a previous process's attempt resolves ONLY with proof that
                # the recorded backend process is gone; otherwise it is held for
                # the operator route (no auto-resolve on a guess).
                proof = self._backend_process_gone(rec)
                if proof is None:
                    if await self._probe_held_attempt(tid, tok, rec, "no process proof"):
                        done += 1
                    continue
                kind = "carrier_restarted"
            else:
                session = self._session_for(str(rec.get("session_id") or ""), str(rec.get("backend") or ""))
                if not await self._backend_quiescent(str(rec.get("backend") or ""), session):
                    # Still owed by a live backend: keep holding, but consult
                    # the server (rate-limited) so an operator resolution is a
                    # real exit (the backend wait is then forgotten).
                    if await self._probe_held_attempt(tid, tok, rec, "backend not quiescent"):
                        done += 1
                    continue
                kind = "backend_quiescent"
            if tid in self._pending_result_delivery:
                continue  # m1: a late result was captured during the probe
            try:
                await asyncio.to_thread(
                    self._http.post,
                    f"/tasks/{tid}/quiescence",
                    {"node_id": self.cfg.node_id, "claim_token": tok, "quiescent": True,
                     "terminal": True, "terminal_status": "failed", "stop_evidence": kind,
                     "observer_incarnation": self._incarnation_id,
                     **({"process_proof": proof} if proof else {})},
                )
            except Exception as e:
                if isinstance(e, urllib.error.HTTPError) and e.code in (404, 409):
                    logger.warning("event=managed_quiescence_refused task_id=%s err=%s", tid, e)
                    await self._drop_attempt(tid)
                    done += 1
                else:
                    logger.warning("event=managed_quiescence_post_failed task_id=%s err=%s", tid, e)
                continue
            self._claim_forget(tid)
            done += 1
            logger.warning("event=managed_recovery_resolved task_id=%s evidence=%s", tid, kind)
        return done

    def _setup_proactive_delivery(self) -> None:
        """Wire autonomous (background-job continuation) turns back to the gateway.

        A live SDK session can produce a turn no one prompted for — when a
        run_in_background job finishes and the agent keeps going. The driver
        surfaces those via a sink; we report each to the gateway so it lands in
        the conversation and reaches the user, instead of being dropped."""
        for name, backend in (self._backends or {}).items():
            setter = getattr(backend, "set_proactive_sink", None)
            if callable(setter):
                try:
                    setter(self._deliver_proactive_turn)
                    logger.info("event=proactive_sink_registered backend=%s", name)
                except Exception:
                    logger.warning("event=proactive_sink_register_failed backend=%s", name, exc_info=True)

    def _deliver_proactive_turn(self, session_id: str, outcome: Any) -> None:
        """Sink called by the driver (off the SDK loop) for an autonomous turn.

        Blocking HTTP is fine here — the driver runs this in a worker thread, not
        on its event loop. Best-effort: a delivery failure must never crash the
        session that produced the turn."""
        if getattr(outcome, "late_managed", False):
            # [A82 Stage 3 rework, M3] late reply of a deadline-held managed
            # turn: commit it to that turn, not the proactive transcript.
            try:
                if self._capture_late_managed_result(session_id, outcome):
                    return
            except Exception:
                logger.warning("event=managed_late_capture_failed session_id=%s", session_id, exc_info=True)
        try:
            text = (getattr(outcome, "output", "") or "").strip()
            if not text:
                return
            usage = None
            try:
                from src.services.result_text import extract_usage_from_ndjson
                usage = extract_usage_from_ndjson(getattr(outcome, "raw_ndjson", "") or "")
            except Exception:
                usage = None
            self._http.post(
                f"/sessions/{session_id}/proactive-turn",
                {
                    "node_id": self.cfg.node_id,
                    "session_id": session_id,
                    "backend": "claude",
                    "output": text,
                    "backend_session_id": getattr(outcome, "backend_session_id", "") or "",
                    "usage": usage,
                    "is_error": bool(getattr(outcome, "is_error", False)),
                    "error_text": getattr(outcome, "error_text", "") or "",
                },
                timeout=15,
            )
            logger.info("event=proactive_turn_reported session_id=%s chars=%d", session_id, len(text))
        except Exception:
            logger.warning("event=proactive_turn_report_failed session_id=%s", session_id, exc_info=True)

    def _setup_activity_forwarding(self) -> None:
        """Forward live task_activity to the controller over the explicit HTTP
        event interface.

        Transport is EXPLICIT, never inferred from network identity. Under the
        controller/worker split the two run in separate containers that do NOT
        share a filesystem even on the same host / same Tailscale IP, so a worker
        must ALWAYS forward its activity — the controller owns the SSE feed the UI
        tails. Forwarding is skipped only for a legacy single-host deployment
        where this worker writes DIRECTLY into the controller's events.ndjson
        (same process / shared FS), opted in via ``WORKER_SHARES_CONTROLLER_FS=1``
        to avoid double-emitting. The old "controller URL looks local ⇒ shared
        events.ndjson" heuristic was removed: it silently disabled forwarding
        under Docker and left the pill stuck on "Working…".
        """
        try:
            if self.cfg.shares_controller_fs:
                logger.info(
                    "event=activity_forward_disabled reason=shared_controller_fs node_id=%s",
                    self.cfg.node_id,
                )
                return
            self._activity_forwarder = _ActivityForwarder(self._http, self.cfg.node_id)
            from src.core.observability import register_event_forwarder
            register_event_forwarder(self._activity_forwarder.offer)
            logger.info(
                "event=activity_forward_enabled controller_url=%s node_id=%s",
                self.cfg.controller_url, self.cfg.node_id,
            )
        except Exception:
            logger.warning("event=activity_forward_setup_failed", exc_info=True)

    # ------------------------------------------------------------------
    # Quota observation (harness-side)
    # ------------------------------------------------------------------

    async def _quota_observe_loop(self) -> None:
        """Observe Claude subscription quota where the harness lives and ship a
        typed observation to the controller over the explicit HTTP interface.

        The controller container has no Claude binary or OAuth credentials by
        design, so quota telemetry MUST be read here and crossed to the
        controller — never spawned controller-side. This uses the ``get_usage``
        control request, which is free (not a model turn); prewarm is a separate,
        deliberately-OFF concern and is not touched here.
        """
        if not self.cfg.quota_observe_enabled:
            logger.info("event=quota_observe_disabled node_id=%s", self.cfg.node_id)
            return
        interval = max(30, int(self.cfg.quota_observe_interval_sec))
        logger.info(
            "event=quota_observe_enabled node_id=%s interval_sec=%d",
            self.cfg.node_id, interval,
        )
        while not self._shutdown.is_set():
            await self._observe_quota_once()
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _observe_quota_once(self) -> None:
        """One harness-side read + ship. Never raises: a failed read ships an
        explicit error observation so the controller can surface
        harness-unavailable (distinct from a valid empty window)."""
        provider = "claude"
        observed_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            from config import config as _config
            from src.services.claude_usage_control import (
                _sdk_version,
                claude_code_version,
                read_claude_usage_raw_with_new_client,
            )

            quota_cfg = getattr(_config, "quota", None)
            claude_cfg = getattr(_config, "claude", None)
            cli_path = getattr(claude_cfg, "sdk_cli_path", None)
            timeout = float(getattr(quota_cfg, "claude_get_usage_timeout_sec", 60.0))
            raw = await read_claude_usage_raw_with_new_client(cli_path=cli_path, timeout=timeout)
            body = {
                "node_id": self.cfg.node_id,
                "provider": provider,
                "principal_key": getattr(quota_cfg, "claude_principal_key", "") or "",
                "sdk_version": _sdk_version(),
                "claude_code_version": claude_code_version(cli_path=cli_path, timeout_sec=2.0),
                "observed_at": observed_at,
                "usage": raw,
            }
            await asyncio.to_thread(self._http.post, "/telemetry/quota-observation", body, 15)
            logger.debug("event=quota_observation_shipped node_id=%s", self.cfg.node_id)
        except Exception as e:
            logger.warning(
                "event=quota_observation_failed node_id=%s err_class=%s",
                self.cfg.node_id, type(e).__name__,
            )
            try:
                await asyncio.to_thread(
                    self._http.post,
                    "/telemetry/quota-observation",
                    {
                        "node_id": self.cfg.node_id,
                        "provider": provider,
                        "observed_at": observed_at,
                        "error": type(e).__name__,
                    },
                    15,
                )
            except Exception as post_err:
                logger.warning(
                    "event=quota_observation_error_report_failed node_id=%s err_class=%s",
                    self.cfg.node_id, type(post_err).__name__,
                )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        # [A82 Stage 3] Advertise protocol 1 only for backends with a managed path.
        managed_backends = self._managed_backends()
        self._http.post("/nodes/register", {
            "node_id": self.cfg.node_id,
            "tailscale_ip": self.cfg.tailscale_ip,
            "api_port": self.cfg.api_port,
            "incarnation_id": self._incarnation_id,
            "capabilities": {
                "backends": self.cfg.backends,
                "max_concurrent": self.cfg.max_concurrent,
                "projects_root": self.cfg.projects_root,
                "repos": self.cfg.list_repos(),
                # Publish the retained catalog. A separate probe here used to
                # overwrite a good startup result with an intermittent empty
                # app-server read before the first heartbeat.
                "models": self._model_capabilities,
                # [A82 Stage 3] Advertise managed protocol 1 only when enabled;
                # flag OFF sends the byte-identical legacy capability set.
                **(
                    {"queue_protocols": [0, 1], "managed_backends": managed_backends}
                    if managed_backends else {}
                ),
            },
        }, timeout=_REGISTRATION_TIMEOUT_SECONDS)
        logger.info("event=registered node_id=%s controller=%s projects_root=%s",
                    self.cfg.node_id, self.cfg.controller_url, self.cfg.projects_root or "(none)")

    async def _wait_for_registration_retry(self, delay: float) -> None:
        """Wait for retry delay, waking early when shutdown was requested."""
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _register_until_success(self) -> None:
        """Keep this daemon alive while the controller is temporarily unavailable."""
        delay = 5.0
        failures = 0
        while not self._shutdown.is_set():
            try:
                await asyncio.to_thread(self._register)
                return
            except Exception as e:
                failures += 1
                logger.warning(
                    "event=startup_registration_failed node_id=%s failures=%d retry_in_sec=%.0f err=%s",
                    self.cfg.node_id,
                    failures,
                    delay,
                    e,
                )
                await self._wait_for_registration_retry(delay)
                delay = min(delay * 2, _REGISTRATION_RETRY_MAX_SECONDS)

    def _deregister(self) -> None:
        try:
            self._http.post("/nodes/deregister", {"node_id": self.cfg.node_id})
            logger.info("event=deregistered node_id=%s", self.cfg.node_id)
        except Exception as e:
            logger.warning("event=deregister_failed err=%s", e)

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------

    def _reap_stale_backend_children(self) -> None:
        """On boot, kill backend children left by a PRIOR worker incarnation.

        Their stdin/stdout pipes died with the previous worker, so they are
        unreachable and unusable — pure leak. Reaping is narrow (only children
        stamped with a different incarnation) and default-ON; disable with
        WORKER_REAP_STALE_SESSIONS=0."""
        disabled = os.environ.get("WORKER_REAP_STALE_SESSIONS", "1").strip().lower()
        if disabled in ("0", "false", "no", "off"):
            logger.info("event=boot_reap_disabled node_id=%s", self.cfg.node_id)
            return
        try:
            reaped = reap_stale_worker_children(self._incarnation_id, self.cfg.node_id)
            if reaped:
                logger.warning(
                    "event=boot_reaped_stale_children node_id=%s count=%d pids=%s",
                    self.cfg.node_id, len(reaped), reaped,
                )
        except Exception as e:
            logger.warning("event=boot_reap_failed node_id=%s err=%s", self.cfg.node_id, e)

    def _count_live_backend_sessions(self) -> int:
        """Sum of pooled live backend sessions across all backends (SDK claude
        pool, etc.). Reported in live_state so the mesh can reconcile pooled
        processes against the gateway's session view."""
        total = 0
        for backend in (self._backends or {}).values():
            counter = getattr(backend, "live_session_count", None)
            if callable(counter):
                try:
                    total += int(counter())
                except Exception:
                    pass
        return total

    def _live_state(self) -> dict:
        """Snapshot of current operational state for heartbeat reporting.

        slots_used counts semaphore-acquired tasks only. len(self._active)
        also includes tasks queued but not yet scheduled, so it can exceed
        max_concurrent when the poll loop fetches a batch larger than the
        semaphore allows.
        """
        return {
            "v": 1,
            "active_tasks": list(self._active.keys()),
            "active_task_details": dict(self._active_meta),
            "slots_used": self._slots_used,
            "slots_total": self.cfg.max_concurrent,
            "canary": self._canary,
            "incarnation_id": self._incarnation_id,
            # Pooled live backend sessions (may exceed active_tasks: an idle
            # session keeps its claude process warm between turns).
            "live_sessions": self._count_live_backend_sessions(),
        }

    async def _heartbeat_loop(self) -> None:
        _consecutive_failures = 0
        _reregister_after = 3  # re-register after this many consecutive non-404 failures
        try:
            while not self._shutdown.is_set():
                self._heartbeat_now.clear()
                try:
                    if time.monotonic() - self._model_capabilities_at >= _MODEL_CAPABILITIES_REFRESH_SECONDS:
                        discovered_models = await asyncio.to_thread(
                            _discover_node_models, self.cfg.backends, self._backends
                        )
                        # Codex model discovery is best-effort. Once this
                        # worker has advertised a usable catalog, retain it
                        # through a transient CLI/auth failure rather than
                        # making the picker disappear.
                        if discovered_models.get("codex") or not self._model_capabilities.get("codex"):
                            self._model_capabilities = discovered_models
                        else:
                            logger.warning(
                                "event=node_model_discovery_empty keeping_last_good_catalog=true node_id=%s",
                                self.cfg.node_id,
                            )
                        self._model_capabilities_at = time.monotonic()
                    live = self._live_state()
                    payload = {
                        "node_id": self.cfg.node_id,
                        "live_state": live,
                        "models": self._model_capabilities,
                    }
                    await asyncio.to_thread(
                        self._http.post, "/nodes/heartbeat", payload
                    )
                    _consecutive_failures = 0
                    logger.debug(
                        "event=heartbeat_sent node_id=%s slots=%d/%d active=%s",
                        self.cfg.node_id,
                        live["slots_used"],
                        live["slots_total"],
                        live["active_tasks"],
                    )
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        # Server doesn't know us — likely restarted and lost
                        # its in-memory registry. Re-register so the mesh
                        # doesn't go dark silently.
                        logger.warning(
                            "event=heartbeat_node_unknown node_id=%s — re-registering",
                            self.cfg.node_id,
                        )
                        _consecutive_failures = 0
                        try:
                            await asyncio.to_thread(self._register)
                        except Exception as re_err:
                            logger.warning("event=re_register_failed err=%s", re_err)
                    else:
                        _consecutive_failures += 1
                        logger.warning("event=heartbeat_failed status=%s err=%s", e.code, e)
                except Exception as e:
                    _consecutive_failures += 1
                    logger.warning("event=heartbeat_failed err=%s", e)

                if _consecutive_failures >= _reregister_after:
                    logger.warning(
                        "event=heartbeat_repeated_failure node_id=%s failures=%d — re-registering",
                        self.cfg.node_id, _consecutive_failures,
                    )
                    _consecutive_failures = 0
                    try:
                        await asyncio.to_thread(self._register)
                    except Exception as re_err:
                        logger.warning("event=re_register_failed err=%s", re_err)

                await self._wait_for_next_heartbeat()
        except asyncio.CancelledError:
            pass

    async def _wait_for_next_heartbeat(self) -> None:
        """Wait for the normal interval, shutdown, or an on-demand heartbeat nudge."""
        shutdown_wait = asyncio.create_task(self._shutdown.wait())
        heartbeat_wait = asyncio.create_task(self._heartbeat_now.wait())
        try:
            _, pending = await asyncio.wait(
                {shutdown_wait, heartbeat_wait},
                timeout=30,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for task in (shutdown_wait, heartbeat_wait):
                if not task.done():
                    task.cancel()

    # ------------------------------------------------------------------
    # Job watcher loop (T3 — Watched Jobs)
    # ------------------------------------------------------------------

    async def _job_watcher_loop(self) -> None:
        """Monitor running jobs for this node: spawn new ones, check PIDs,
        report completions, reconcile after restart."""
        try:
            while not self._shutdown.is_set():
                try:
                    running = await asyncio.to_thread(
                        self._http.get,
                        "/jobs",
                        {"node_id": self.cfg.node_id, "status": "running", "limit": "50"},
                    ) or []
                except Exception as e:
                    logger.debug("event=job_watcher_fetch_failed err=%s", e)
                    running = []

                for job in running:
                    job_id = job.get("id", "")
                    pid = job.get("pid")
                    command = job.get("command")

                    if pid is None and command:
                        # Spawn the detached process
                        await self._spawn_job_process(job)
                    elif pid is not None:
                        # Check if process still alive.
                        # IMPORTANT (Windows): we retain the Popen handle in
                        # self._job_procs, which keeps an OS handle open on the
                        # process. A finished-but-handle-held process still answers
                        # OpenProcess(), so _pid_alive() would report it alive
                        # forever and the job would never complete. proc.poll() is
                        # authoritative here: None while running, exit code once
                        # exited. Only fall back to the OS probe when we have no
                        # stored handle (e.g. after a worker restart).
                        stored_proc = self._job_procs.get(job_id)
                        if stored_proc is not None:
                            alive = stored_proc.poll() is None
                            if alive:
                                identity = await asyncio.to_thread(_process_identity, int(pid))
                                try:
                                    await asyncio.to_thread(
                                        self._http.post,
                                        f"/jobs/{job_id}/probe",
                                        {
                                            "node_id": self.cfg.node_id,
                                            "observed_command": identity.get("command"),
                                            "observed_started_epoch": identity.get("started_epoch"),
                                            "probe_error": identity.get("error", ""),
                                        },
                                    )
                                except Exception as e:
                                    logger.debug("event=job_probe_post_failed job_id=%s err=%s", job_id, e)
                        else:
                            identity = await asyncio.to_thread(_process_identity, int(pid))
                            mismatch = _job_identity_mismatch(job, identity)
                            try:
                                await asyncio.to_thread(
                                    self._http.post,
                                    f"/jobs/{job_id}/probe",
                                    {
                                        "node_id": self.cfg.node_id,
                                        "observed_command": identity.get("command"),
                                        "observed_started_epoch": identity.get("started_epoch"),
                                        "probe_error": identity.get("error", ""),
                                    },
                                )
                            except Exception as e:
                                logger.debug("event=job_probe_post_failed job_id=%s err=%s", job_id, e)
                            if mismatch:
                                tail = _read_log_tail(job.get("log_path")) if job.get("log_path") else ""
                                detail = mismatch
                                if tail:
                                    detail = f"{detail}\n\nLast log lines:\n{tail}"
                                try:
                                    await asyncio.to_thread(
                                        self._http.post,
                                        f"/jobs/{job_id}/done",
                                        {
                                            "node_id": self.cfg.node_id,
                                            "exit_code": -1,
                                            "status": "lost",
                                            "tail": detail,
                                        },
                                    )
                                    logger.warning(
                                        "event=job_lost job_id=%s pid=%s reason=%s",
                                        job_id,
                                        pid,
                                        mismatch,
                                    )
                                except Exception as e:
                                    logger.warning("event=job_lost_post_failed job_id=%s err=%s", job_id, e)
                                continue
                            alive = bool(identity.get("alive"))
                        if not alive:
                            # Process exited — collect tail and exit code.
                            # Prefer proc.poll() from the stored Popen object: on Windows
                            # the OS handle stays open as long as Popen is alive, so this
                            # reliably returns the real exit code even after the process ends.
                            log_path = job.get("log_path")
                            tail = _read_log_tail(log_path) if log_path else ""
                            stored_proc = self._job_procs.pop(job_id, None)
                            if stored_proc is not None:
                                exit_code = stored_proc.poll()
                            else:
                                exit_code = await asyncio.to_thread(_collect_exit_code, pid)
                            try:
                                await asyncio.to_thread(
                                    self._http.post,
                                    f"/jobs/{job_id}/done",
                                    {
                                        "node_id": self.cfg.node_id,
                                        "exit_code": exit_code if exit_code is not None else -1,
                                        "tail": tail,
                                    },
                                )
                                logger.info("event=job_completed job_id=%s exit_code=%s", job_id, exit_code)
                            except Exception as e:
                                logger.warning("event=job_done_post_failed job_id=%s err=%s", job_id, e)

                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _spawn_job_process(self, job: Dict[str, Any]) -> None:
        """Spawn a detached process for a watched job."""
        from src.core.process_utils import ensure_node_on_path

        command = job.get("command", "")
        job_id = job.get("id", "")
        label = job.get("label", job_id)
        if not command:
            return

        job_cwd = job.get("cwd") or self.cfg.projects_root or None
        log_dir = Path(job_cwd) / ".ai" if job_cwd else Path.cwd() / ".ai"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / f"job_{job_id.replace('job_', '')}.log")

        try:
            log_fh = open(log_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=job_cwd,
                env=ensure_node_on_path(),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | _NO_WINDOW) if sys.platform == "win32" else 0,
            )
            log_fh.close()
            pgid = proc.pid  # On Windows, process group = pid
            try:
                import os as _os
                if sys.platform != "win32":
                    pgid = _os.getpgid(proc.pid)
            except Exception:
                pass
            identity = await asyncio.to_thread(_process_identity, proc.pid)

            await asyncio.to_thread(
                self._http.post,
                f"/jobs/{job_id}/start",  # We'll add this endpoint
                {
                    "node_id": self.cfg.node_id,
                    "pid": proc.pid,
                    "pgid": pgid,
                    "log_path": log_path,
                    "started_epoch": identity.get("started_epoch"),
                    "observed_command": identity.get("command"),
                },
            )
            self._job_procs[job_id] = proc  # keep handle alive so poll() can read exit code on Windows
            logger.info("event=job_spawned job_id=%s label=%s pid=%d", job_id, label, proc.pid)
        except Exception as e:
            logger.warning("event=job_spawn_failed job_id=%s label=%s err=%s", job_id, label, e)
            try:
                await asyncio.to_thread(
                    self._http.post,
                    f"/jobs/{job_id}/done",
                    {"node_id": self.cfg.node_id, "exit_code": -1, "tail": f"Spawn failed: {e}"},
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        empty_count = 0
        try:
            while not self._shutdown.is_set():
                # [A82 Stage 3 rework, M3/B2] Re-send unacknowledged managed
                # results and move durably held attempts (bounded batches; no
                # background retry tasks). No-op when no carrier state exists.
                # M1: managed-path failures (disk full, bad state) are logged
                # and never kill legacy polling.
                try:
                    if self._pending_result_delivery - self._delivering:
                        await self._redeliver_spooled_results()
                    if self._claim_store is not None:
                        await self._reconcile_managed_claims()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error("event=managed_poll_pass_failed", exc_info=True)
                tasks = await self._fetch_pending()
                if tasks:
                    empty_count = 0
                    for row in tasks:
                        if self._shutdown.is_set():
                            break
                        task_id = row.get("id", "unknown")
                        # [A82 Stage 3] With the managed carrier ON: dedup a
                        # fetched id already scheduled / executing / awaiting
                        # result-delivery (WRK01) and bound scheduled handlers to
                        # 2x slots (WRK02). Control rows (close_session /
                        # cancel_codex) bypass the slot semaphore by design and are
                        # EXEMPT from the capacity bound (M1). Flag OFF: the
                        # scheduling loop is exactly main's.
                        if self._managed_enabled():
                            if self._is_already_scheduled(task_id):
                                continue
                            if (
                                row.get("action") not in ("close_session", "cancel_codex", CANCEL_MANAGED_ACTION)
                                and not self._scheduling_capacity_available()
                            ):
                                # Leave it queued server-side; a later poll picks
                                # it up once a slot frees. Keep scanning so
                                # control rows further down are still scheduled.
                                continue
                        self._active_meta[task_id] = {
                            "task_id": task_id,
                            "backend": row.get("backend", ""),
                            "action": row.get("action", ""),
                            "phase": "scheduled",
                            "started_at": datetime.now(tz=timezone.utc).isoformat(),
                        }
                        t = asyncio.create_task(self._handle_task(row))
                        self._active[task_id] = t
                        t.add_done_callback(lambda _t, tid=task_id: (self._active.pop(tid, None), self._active_meta.pop(tid, None)))
                    # Short pause to avoid hammering if tasks are always present
                    wait_sec = 2
                else:
                    empty_count += 1
                    # Back off to 30s after 5 empty polls
                    wait_sec = 30 if empty_count >= 5 else 5

                # Wait for next poll interval or a nudge
                self._poll_now.clear()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._poll_now.wait()),
                        timeout=wait_sec,
                    )
                    empty_count = 0  # nudge received — reset backoff
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _fetch_pending(self) -> List[Dict[str, Any]]:
        params = {
            "node_id": self.cfg.node_id,
            "backends": ",".join(self.cfg.backends),
            "limit": str(self.cfg.max_concurrent * 2),
            "accept_unpinned": "true" if self.cfg.accept_unpinned else "false",
        }
        legacy: List[Dict[str, Any]] = []
        for attempt in range(1, 3):
            try:
                legacy = await asyncio.to_thread(self._http.get, "/tasks/pending", params)
                break
            except Exception as exc:
                if attempt == 2:
                    logger.warning("event=fetch_pending_failed attempts=%d err=%s", attempt, exc)
                else:
                    await asyncio.sleep(1)
        if not self._managed_enabled():
            return legacy
        return list(legacy or []) + await self._fetch_pending_managed(params)

    async def _fetch_pending_managed(self, params: Dict[str, str]) -> List[Dict[str, Any]]:
        """[A82 Stage 3 rework, B2] Poll protocol-1 pending turns (capability
        negotiated: this carrier declares queue protocol 1). Skipped while new
        managed claims are blocked by an unreconciled result (design §7)."""
        if self._managed_claims_blocked:
            logger.warning(
                "event=managed_claims_blocked reason=%s — not polling managed turns",
                self._managed_claims_blocked,
            )
            return []
        managed_backends = self._managed_backends()
        if not managed_backends:
            return []
        try:
            rows = await asyncio.to_thread(
                self._http.get,
                "/tasks/pending-managed",
                {**params, "backends": ",".join(managed_backends)},
            )
        except Exception as exc:
            logger.warning("event=fetch_pending_managed_failed err=%s", exc)
            return []
        return [r for r in (rows or []) if int(r.get("queue_protocol", 0) or 0) == 1]

    # ------------------------------------------------------------------
    # Task handling
    # ------------------------------------------------------------------

    async def _handle_task(self, task_row: Dict[str, Any]) -> None:
        from src.core.observability import set_log_context, emit_event
        task_id = task_row.get("id", "unknown")
        session_id = task_row.get("session_id", "")
        # Correlate every line + event for this task with task_id/session_id.
        set_log_context(task_id=task_id, session_id=session_id)
        # Lightweight control action — must NOT consume a turn slot or it could
        # wait hours behind long-running turns before the process is freed.
        if task_row.get("action") == "cancel_codex":
            async with self._codex_control_semaphore:
                await self._handle_close_session(task_row)
            return
        if task_row.get("action") == CANCEL_MANAGED_ACTION:
            # [A82 Stage 4b] Operator cancel of a managed attempt this carrier
            # holds — outside the turn slot (it must never queue behind the
            # very turn it stops).
            async with self._codex_control_semaphore:
                await self._handle_cancel_managed(task_row)
            return
        if task_row.get("action") == "close_session":
            await self._handle_close_session(task_row)
            return
        async with self._semaphore:
            self._slots_used += 1
            try:
                # Claim — optimistic lock. [A82 Stage 3 rework] A managed
                # (protocol-1) row — only ever fetched when the managed flag is ON —
                # claims via /claim-managed and executes the claim RESPONSE's
                # frozen task + token (design §5, WRK03), after reserving a result
                # envelope and a fenced start. Legacy protocol-0 rows keep the
                # byte-identical /claim path and execute the poll row.
                managed = self._managed_enabled() and int(task_row.get("queue_protocol", 0) or 0) == 1
                claim_token: Optional[str] = None
                if managed and task_row.get("backend", "") not in self._managed_backends():
                    # Fail closed: never claim a managed row for a backend without
                    # a managed execution path (no legacy fallback).
                    logger.warning(
                        "event=managed_row_backend_unsupported task_id=%s backend=%s",
                        task_id, task_row.get("backend", ""),
                    )
                    return
                if managed:
                    claim_response = await self._claim_and_start_managed(task_id)
                    if claim_response is None:
                        return
                    task_row, claim_token = claim_response
                else:
                    try:
                        await asyncio.to_thread(
                            self._http.post,
                            f"/tasks/{task_id}/claim",
                            {"node_id": self.cfg.node_id},
                        )
                    except urllib.error.HTTPError as e:
                        if e.code == 409:
                            logger.debug("claim_race (already claimed)")
                        else:
                            logger.warning("claim_failed err=%s", e)
                        return
                    except Exception as e:
                        logger.warning("claim_failed err=%s", e)
                        return

                logger.info("task_claimed")
                meta = self._active_meta.setdefault(task_id, {"task_id": task_id})
                meta.update({
                    "backend": task_row.get("backend", ""),
                    "action": task_row.get("action", ""),
                    "phase": "running",
                    "started_at": datetime.now(tz=timezone.utc).isoformat(),
                })
                emit_event("task_claimed", backend=task_row.get("backend", ""))
                self._heartbeat_now.set()  # push slots_used immediately to the server
                self._inflight_sessions.add(session_id)

                # [A82 Stage 3 rework, B2] Write-ahead: durably record that the
                # backend is ABOUT to be invoked. If this cannot be persisted the
                # backend is not invoked and the attempt is released (prompt kept).
                turn_uuid: Optional[str] = None
                if managed and claim_token:
                    turn_uuid = str(uuid.uuid4())
                    # [A82 Stage 4b rework] Pre-invoke cancel check: an operator
                    # cancel handled before this point (no turn uuid to arm yet)
                    # is recorded on the attempt. No await between this check
                    # and recording the uuid below, so a later cancel always
                    # finds the uuid and arms the backend instead.
                    if (self._managed_claims.get(task_id) or {}).get("cancel_requested"):
                        logger.info("event=managed_cancelled_before_invoke task_id=%s", task_id)
                        await self._release_managed_claim(task_id, claim_token, not_invoked=True)
                        return
                    try:
                        self._claim_record(task_id, invoked=True, turn_uuid=turn_uuid)
                    except Exception:
                        logger.error("event=managed_invoke_persist_failed task_id=%s", task_id)
                        await self._release_managed_claim(task_id, claim_token, not_invoked=True)
                        return

                # Execute
                result = await _execute_task(
                    task_row,
                    self._backends,
                    self._http,
                    telemetry_sink=self._telemetry_sink,
                    node_id=self.cfg.node_id,
                    **({"ownership": ManagedTurnOwnership(
                        task_id=task_id,
                        session_id=str(task_row.get("session_id") or session_id or ""),
                        node_id=self.cfg.node_id,
                        claim_token=claim_token,
                        incarnation_id=self._incarnation_id,
                        turn_uuid=turn_uuid,
                    ), "on_process": functools.partial(self._record_backend_process, task_id),
                    } if managed and claim_token else {}),
                )

                # [A82 Stage 3 rework] Uncertain managed outcome (uncorrelated
                # result / deadline): move the started turn to recovery_required
                # through the token-fenced carrier route and KEEP ownership — no
                # result is reported, nothing is auto-released.
                if managed and claim_token and result.get("error_class") == "recovery_required":
                    await self._enter_managed_recovery(
                        task_id, claim_token, "; ".join(result.get("errors") or [])
                    )
                    return
                # [A82 Stage 3 rework, M2] A managed conflict is raised by the
                # backend BEFORE the prompt is submitted (lock busy / session not
                # quiescent at the loop-thread reservation): nothing ran, so the
                # turn returns to pending with the not-invoked attestation — never
                # a terminal failure for a prompt that was never sent.
                if managed and claim_token and result.get("error_class") == "managed_conflict":
                    try:
                        self._claim_record(task_id, invoked=False, status="start_unknown")
                    except Exception:
                        logger.warning("event=managed_conflict_persist_failed task_id=%s", task_id)
                    await self._release_managed_claim(task_id, claim_token, not_invoked=True)
                    return

                # Post result. [A82 Stage 3] A managed (protocol-1) turn spools its
                # result BEFORE the POST to /result-managed and keeps ownership
                # until a receipt-matched ack (design §6); a legacy protocol-0 turn
                # keeps the exact byte-identical in-memory post path.
                try:
                    if managed and claim_token:
                        await self._deliver_managed_result(
                            task_id, claim_token, result
                        )
                    else:
                        delivered = await _post_result_until_accepted(
                            self._http,
                            f"/tasks/{task_id}/result",
                            {"node_id": self.cfg.node_id, **result},
                            label="result_post",
                        )
                        if not delivered:
                            raise RuntimeError("controller_unreachable_until_delivery_deadline")
                    logger.info(
                        "task_result_posted success=%s elapsed=%.1fs",
                        result["success"], result["execution_time"],
                    )
                    emit_event(
                        "task_result_posted",
                        success=result["success"],
                        duration_s=round(result.get("execution_time", 0.0), 3),
                    )
                except Exception as e:
                    logger.error("result_post_failed err=%s", e)
            finally:
                # [A82 Stage 3 rework, m6] Never leak an envelope reservation
                # (cancellation included); a committed envelope already consumed it.
                if claim_token and self._result_spool is not None:
                    self._result_spool.release_reservation(task_id, claim_token)
                self._slots_used -= 1
                self._inflight_sessions.discard(session_id)
                self._heartbeat_now.set()  # push slots_used=0 immediately after task ends

    async def _handle_cancel_managed(self, task_row: Dict[str, Any]) -> None:
        """[A82 Stage 4b] Deliver an operator cancel to the managed attempt this
        carrier holds for ``payload.target_task_id``. Claims the control row
        (legacy protocol-0 claim), looks the attempt up in the durable claim
        record (its per-attempt ``turn_uuid``), and asks the backend to cancel
        EXACTLY that turn (``CodingBackend.cancel_managed_turn``). The attempt's
        own result / recovery then commits ``cancelled`` server-side (the cancel
        is recorded against its token). No live attempt ⇒ nothing to interrupt
        (it already finished, or the Stage-3 recovery exits own it)."""
        task_id = task_row.get("id", "unknown")
        try:
            await asyncio.to_thread(
                self._http.post, f"/tasks/{task_id}/claim", {"node_id": self.cfg.node_id}
            )
        except urllib.error.HTTPError as e:
            if e.code != 409:
                logger.warning("cancel_managed_claim_failed err=%s", e)
            return
        except Exception as e:
            logger.warning("cancel_managed_claim_failed err=%s", e)
            return
        payload = task_row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        target = payload.get("target_task_id") if isinstance(payload, dict) else None
        delivered = False
        detail = "no live managed attempt on this carrier"
        if not isinstance(target, str) or not target or len(target) > 256:
            detail = "invalid cancellation target"
        else:
            live = target in self._managed_claims
            if live:
                # [A82 Stage 4b rework] This process holds the attempt: record
                # the cancel on it durably BEFORE any await, so the pre-invoke
                # check refuses to invoke it even if no turn uuid exists yet.
                try:
                    self._claim_record(target, cancel_requested=True)
                except Exception:
                    self._managed_claims.setdefault(target, {})["cancel_requested"] = True
                    logger.warning("event=cancel_managed_persist_failed target=%s", target)
                detail = "cancel held for the attempt (not invoked yet)"
            rec = dict(self._managed_claims.get(target) or {})
            if not rec and self._claim_store is not None:
                rec = dict(self._claim_store.get(target) or {})
            turn_uuid = str(rec.get("turn_uuid") or "")
            backend = (self._backends or {}).get(str(rec.get("backend") or ""))
            cancel = getattr(backend, "cancel_managed_turn", None)
            if rec and turn_uuid and callable(cancel):
                session = self._session_for(str(rec.get("session_id") or ""), str(rec.get("backend") or ""))
                try:
                    delivered = bool(await asyncio.to_thread(cancel, session, turn_uuid))
                    detail = "interrupt delivered or armed" if delivered else "turn not in flight"
                except Exception as e:
                    detail = f"cancel failed: {type(e).__name__}"
                    logger.warning("event=cancel_managed_failed target=%s", target, exc_info=True)
        logger.info("event=cancel_managed_handled target=%s delivered=%s", target, delivered)
        result = {
            "success": True, "output": detail, "errors": [], "files_modified": [],
            "execution_time": 0.0, "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 0,
        }
        try:
            delivered_post = await _post_result_until_accepted(
                self._http, f"/tasks/{task_id}/result", {"node_id": self.cfg.node_id, **result},
                label="cancel_managed_result_post",
            )
            if not delivered_post:
                raise RuntimeError("controller_unreachable_until_delivery_deadline")
        except Exception as e:
            logger.error("cancel_managed_result_post_failed err=%s", e)

    async def _wait_for_inflight_turn(self, session_id: str) -> None:
        """Hold a close_session control task until the session's turn finishes.

        Closing a session while its turn is mid-generation calls the SDK
        interrupt (``cancel_inflight`` → ``client.interrupt()``), which surfaces
        in the turn result as ``error_during_execution`` and marks the task
        failed with the reply truncated. Wait for the running turn to post its
        real outcome first; the close then tears the process down after nothing
        is mid-flight. Runs as a concurrent task (outside the semaphore), so the
        wait never blocks the poll loop or other slots.
        """
        waited = 0
        while session_id in self._inflight_sessions:
            if waited and waited % 60 == 0:
                logger.warning(
                    "event=close_deferred_inflight session_id=%s waited_s=%d",
                    session_id, waited,
                )
            await asyncio.sleep(1)
            waited += 1

    async def _handle_close_session(self, task_row: Dict[str, Any]) -> None:
        """Claim + execute a close_session control task outside the slot semaphore.

        Claims the task (so it leaves the pending queue and is not re-polled),
        defers while the session has an in-flight turn (so the teardown never
        interrupts a generating reply), then runs the backend teardown via
        _execute_task's close_session branch and posts a terminal result. Kept
        deliberately separate from the turn path so a close is never blocked by a
        full slot pool."""
        task_id = task_row.get("id", "unknown")
        try:
            await asyncio.to_thread(
                self._http.post, f"/tasks/{task_id}/claim", {"node_id": self.cfg.node_id}
            )
        except urllib.error.HTTPError as e:
            if e.code != 409:
                logger.warning("close_claim_failed err=%s", e)
            return
        except Exception as e:
            logger.warning("close_claim_failed err=%s", e)
            return
        session_id = task_row.get("session_id", "")
        if session_id and task_row.get("action") == "close_session":
            await self._wait_for_inflight_turn(session_id)
        result = await _execute_task(
            task_row,
            self._backends,
            self._http,
            telemetry_sink=self._telemetry_sink,
            node_id=self.cfg.node_id,
        )
        try:
            delivered = await _post_result_until_accepted(
                self._http,
                f"/tasks/{task_id}/result",
                {"node_id": self.cfg.node_id, **result},
                label="close_result_post",
            )
            if not delivered:
                raise RuntimeError("controller_unreachable_until_delivery_deadline")
        except Exception as e:
            logger.error("close_result_post_failed err=%s", e)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _install_signal_handler(self) -> None:
        """Install the shutdown handler before startup retries begin."""
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        except (NotImplementedError, OSError):
            try:
                signal.signal(signal.SIGTERM, lambda *_: self._on_sigterm())
            except (OSError, ValueError):
                pass

    async def run(self) -> None:
        self._reap_stale_backend_children()
        self._install_signal_handler()
        await self._register_until_success()
        if self._shutdown.is_set():
            return

        # [A82 Stage 3 rework, M3] Boot replay: re-deliver managed results that
        # were spooled but never acknowledged before the previous process exited.
        if self._pending_result_delivery:
            try:
                await self._redeliver_spooled_results()
            except Exception:
                logger.warning("event=managed_result_boot_replay_failed", exc_info=True)
        # [A82 Stage 3 rework, B2] Every attempt a previous process held (token
        # persisted at claim time) gets an exit now: release if never invoked,
        # else recovery + carrier_restarted evidence (children reaped above).
        if self._claim_store is not None:
            try:
                await self._reconcile_managed_claims()
            except Exception:
                logger.warning("event=managed_claim_boot_reconcile_failed", exc_info=True)

        nudge_listener = asyncio.create_task(
            _run_nudge_listener(
                self.cfg.tailscale_ip,
                self.cfg.api_port,
                self._poll_now,
                self._heartbeat_now,
            )
        )
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        quota_observer = asyncio.create_task(self._quota_observe_loop())
        if self._canary:
            logger.info("event=worker_canary_mode node_id=%s polling_disabled=true", self.cfg.node_id)
            poller = asyncio.create_task(self._shutdown.wait())
            job_watcher = asyncio.create_task(self._shutdown.wait())
        else:
            poller = asyncio.create_task(self._poll_loop())
            job_watcher = asyncio.create_task(self._job_watcher_loop())

        try:
            await self._shutdown.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("event=keyboard_interrupt node_id=%s", self.cfg.node_id)

        # Drain: best-effort release of in-flight claims (T4 fast path),
        # then wait up to 30s for active tasks to finish or cancel.
        logger.info("event=draining active=%d", len(self._active))
        release_tasks = list(self._active.keys())
        for task_id in release_tasks:
            # [A82 Stage 3 rework, M1] A managed attempt is released ONLY when the
            # shutdown guard allows it (claimed, not started, no result pending
            # delivery) and only through the token-fenced managed release; a
            # running managed backend / undelivered result keeps its ownership.
            managed_claim = self._managed_claims.get(task_id)
            if managed_claim is not None:
                if self._managed_shutdown_release_ok({
                    "id": task_id,
                    "queue_protocol": 1,
                    "status": managed_claim.get("status", ""),
                }):
                    await self._release_managed_claim(task_id, managed_claim["claim_token"])
                # Anything else stays durably recorded for the next boot.
                else:
                    logger.info(
                        "event=managed_ownership_retained_on_drain task_id=%s status=%s",
                        task_id, managed_claim.get("status", ""),
                    )
                continue
            try:
                await asyncio.to_thread(
                    self._http.post,
                    f"/tasks/{task_id}/release",
                    {"node_id": self.cfg.node_id},
                )
            except Exception as e:
                logger.debug("event=release_on_drain_failed task_id=%s err=%s", task_id, e)

        if self._active:
            _, pending = await asyncio.wait(
                list(self._active.values()), timeout=30
            )
            for t in pending:
                t.cancel()

        for t in (poller, heartbeat, nudge_listener, job_watcher, quota_observer):
            t.cancel()
        await asyncio.gather(poller, heartbeat, nudge_listener, job_watcher, quota_observer, return_exceptions=True)

        # Terminate any backend subprocesses still alive (e.g. a hung
        # claude.exe that outlived its task). Without this, a worker restart
        # orphans these children and they accumulate as zombies, competing for
        # auth/token slots and memory.
        for name, backend in (self._backends or {}).items():
            terminate = getattr(backend, "terminate_active_processes", None)
            if callable(terminate):
                try:
                    await asyncio.to_thread(terminate)
                    logger.info("event=backend_procs_terminated backend=%s", name)
                except Exception as e:
                    logger.warning("event=backend_terminate_failed backend=%s err=%s", name, e)

        self._deregister()

    def _on_sigterm(self) -> None:
        logger.info("event=sigterm_received node_id=%s", self.cfg.node_id)
        self._shutdown.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        from dotenv import load_dotenv
        from pathlib import Path as _Path
        _env = _Path(__file__).resolve().parent.parent.parent / ".env"
        if _env.exists():
            load_dotenv(_env, override=False)
    except ImportError:
        pass

    # Use the shared observability spine so every worker log line auto-carries
    # [node=<WORKER_NODE_ID> ...] and worker events land in this machine's
    # logs/events.ndjson — correlatable with the gateway by task_id.
    from src.worker.config import WorkerConfig
    from src.core.observability import init_logging
    _cfg = WorkerConfig.from_env()
    init_logging(node_id=_cfg.node_id, level="INFO")

    # Diagnosability: the worker has died silently (exit code 1, no traceback)
    # while running long turns — see docs/INCIDENTS/HORSE_WORKER_RESTARTS.md.
    # faulthandler dumps a native-level traceback to stderr (→ PM2 error log)
    # even for hard faults the normal Python excepthook would miss.
    try:
        faulthandler.enable()
    except (ValueError, RuntimeError):  # stderr not a real file (rare)
        pass

    agent = WorkerAgent()
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    except BaseException:
        # Never let the daemon exit without leaving a trace of WHY. This turns a
        # silent PM2 auto-restart into a diagnosable event on the next occurrence.
        logger.exception("event=worker_fatal_exit node_id=%s", _cfg.node_id)
        for _h in logging.getLogger().handlers:
            try:
                _h.flush()
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
