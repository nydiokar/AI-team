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
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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

    def post(self, path: str, body: Any = None, timeout: int = 10) -> Any:
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(
            f"{self._base}{path}",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def get(self, path: str, params: Optional[Dict[str, str]] = None, timeout: int = 10) -> Any:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Nudge listener — tiny asyncio HTTP server, just accepts POST /nudge
# ---------------------------------------------------------------------------

async def _run_nudge_listener(host: str, port: int, poll_event: asyncio.Event) -> None:
    """Accept POST /nudge and set poll_event so the poll loop fires immediately."""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.read(512), timeout=2)
            # Minimal method/path validation — avoid treating arbitrary TCP
            # probes (port scanners, health checks) as a real nudge, which
            # would reset the poll backoff and cause spurious tight polling.
            is_nudge = data.startswith(b"POST /nudge")
            if is_nudge:
                response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
                poll_event.set()
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
    from src.backends import ClaudeCodeBackend, CodexBackend, OpenCodeBackend, OpenCodeServerBackend
    return {
        "claude": ClaudeCodeBackend(),
        "codex": CodexBackend(),
        "opencode": OpenCodeBackend(),
        "opencode-server": OpenCodeServerBackend(),
    }


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

def _make_session_from_payload(payload: Dict[str, Any]) -> Any:
    """Reconstruct a Session-like object from the task payload."""
    from src.core import SessionStore
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
    )
    # Copy optional fields if present
    for attr in ("telegram_chat_id", "telegram_thread_id", "owner_user_id", "last_user_message"):
        if attr in session_dict:
            setattr(session, attr, session_dict[attr])
    return session


# ---------------------------------------------------------------------------
# Task executor
# ---------------------------------------------------------------------------

async def _execute_task(task_row: Dict[str, Any], backends: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one task row from mesh_tasks. Returns an ExecutionResultPayload-compatible dict."""
    payload = task_row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    action = task_row.get("action", "run_oneoff")
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

    try:
        from src.core.interfaces import ExecutionResult as _ER

        if action in ("create_session", "resume_session"):
            session = _make_session_from_payload(payload)
            if session is None:
                raise ValueError("Session payload missing for session action")
            if action == "create_session" or not session.backend_session_id:
                raw = await asyncio.to_thread(backend.create_session, session)
            else:
                raw = await asyncio.to_thread(backend.resume_session, session, prompt)
        else:
            cwd = payload.get("metadata", {}).get("cwd", "")
            raw = await asyncio.to_thread(backend.run_oneoff, cwd, prompt)

        elapsed = time.monotonic() - start
        if isinstance(raw, _ER):
            return {
                "success": raw.success,
                "output": _bound_output(raw.output or ""),
                "errors": list(raw.errors or []),
                "files_modified": list(raw.files_modified or []),
                "execution_time": elapsed,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "return_code": getattr(raw, "return_code", 0),
                "backend_session_id": raw.backend_session_id or "",
            }
        # Fallback for legacy return types
        return {
            "success": True,
            "output": _bound_output(str(raw)),
            "errors": [],
            "files_modified": [],
            "execution_time": elapsed,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "return_code": 0,
        }
    except Exception as e:
        elapsed = time.monotonic() - start
        import traceback as _tb
        from src.core.observability import emit_event
        detail = _tb.format_exc()
        error_class = type(e).__name__
        concise = f"{error_class}: {e}"
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
        }


# ---------------------------------------------------------------------------
# Worker daemon
# ---------------------------------------------------------------------------

class WorkerAgent:
    def __init__(self) -> None:
        from src.worker.config import WorkerConfig
        self.cfg = WorkerConfig.from_env()
        self._http = _HTTP(self.cfg.controller_url, self.cfg.worker_token)
        self._backends = _make_backends()
        self._active: Dict[str, asyncio.Task] = {}   # task_id → asyncio.Task
        self._shutdown = asyncio.Event()
        self._poll_now = asyncio.Event()
        self._semaphore = asyncio.Semaphore(self.cfg.max_concurrent)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        self._http.post("/nodes/register", {
            "node_id": self.cfg.node_id,
            "tailscale_ip": self.cfg.tailscale_ip,
            "api_port": self.cfg.api_port,
            "capabilities": {
                "backends": self.cfg.backends,
                "max_concurrent": self.cfg.max_concurrent,
                "projects_root": self.cfg.projects_root,
                "repos": self.cfg.list_repos(),
            },
        })
        logger.info("event=registered node_id=%s controller=%s projects_root=%s",
                    self.cfg.node_id, self.cfg.controller_url, self.cfg.projects_root or "(none)")

    def _deregister(self) -> None:
        try:
            self._http.post("/nodes/deregister", {"node_id": self.cfg.node_id})
            logger.info("event=deregistered node_id=%s", self.cfg.node_id)
        except Exception as e:
            logger.warning("event=deregister_failed err=%s", e)

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._shutdown.is_set():
                try:
                    await asyncio.to_thread(
                        self._http.post, "/nodes/heartbeat", {"node_id": self.cfg.node_id}
                    )
                    logger.debug("event=heartbeat_sent node_id=%s", self.cfg.node_id)
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        # Server doesn't know us — likely restarted and lost
                        # its in-memory registry. Re-register so the mesh
                        # doesn't go dark silently.
                        logger.warning(
                            "event=heartbeat_node_unknown node_id=%s — re-registering",
                            self.cfg.node_id,
                        )
                        try:
                            await asyncio.to_thread(self._register)
                        except Exception as re_err:
                            logger.warning("event=re_register_failed err=%s", re_err)
                    else:
                        logger.warning("event=heartbeat_failed status=%s err=%s", e.code, e)
                except Exception as e:
                    logger.warning("event=heartbeat_failed err=%s", e)
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

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
                        # Check if process still alive
                        alive = await asyncio.to_thread(_pid_alive, pid)
                        if not alive:
                            # Process exited — collect tail and report done
                            log_path = job.get("log_path")
                            tail = _read_log_tail(log_path) if log_path else ""
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

                # Reconcile after restart: check if running PIDs still match
                # (uses the same `running` list so no extra fetch needed)
                for job in running:
                    pid = job.get("pid")
                    if pid is None:
                        continue
                    if job.get("id") in {j.get("id") for j in running if j.get("pid")}:
                        continue
                    alive = await asyncio.to_thread(_pid_alive, pid)
                    if not alive:
                        log_path = job.get("log_path")
                        tail = _read_log_tail(log_path) if log_path else ""
                        tail = f"[reconciled after worker restart] {tail}"
                        try:
                            await asyncio.to_thread(
                                self._http.post,
                                f"/jobs/{job.get('id')}/done",
                                {"node_id": self.cfg.node_id, "exit_code": -1, "tail": tail},
                            )
                        except Exception:
                            pass

                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _spawn_job_process(self, job: Dict[str, Any]) -> None:
        """Spawn a detached process for a watched job."""
        command = job.get("command", "")
        job_id = job.get("id", "")
        label = job.get("label", job_id)
        if not command:
            return

        log_dir = Path(self.cfg.projects_root) / ".ai" if self.cfg.projects_root else Path.cwd() / ".ai"
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
                cwd=self.cfg.projects_root or None,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
            log_fh.close()
            pgid = proc.pid  # On Windows, process group = pid
            try:
                import os as _os
                if sys.platform != "win32":
                    pgid = _os.getpgid(proc.pid)
            except Exception:
                pass

            await asyncio.to_thread(
                self._http.post,
                f"/jobs/{job_id}/start",  # We'll add this endpoint
                {
                    "node_id": self.cfg.node_id,
                    "pid": proc.pid,
                    "pgid": pgid,
                    "log_path": log_path,
                },
            )
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
                tasks = await self._fetch_pending()
                if tasks:
                    empty_count = 0
                    for row in tasks:
                        if self._shutdown.is_set():
                            break
                        task_id = row.get("id", "unknown")
                        t = asyncio.create_task(self._handle_task(row))
                        self._active[task_id] = t
                        t.add_done_callback(lambda _t, tid=task_id: self._active.pop(tid, None))
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
        try:
            return await asyncio.to_thread(
                self._http.get,
                "/tasks/pending",
                {
                    "node_id": self.cfg.node_id,
                    "backends": ",".join(self.cfg.backends),
                    "limit": str(self.cfg.max_concurrent * 2),
                },
            )
        except Exception as e:
            logger.warning("event=fetch_pending_failed err=%s", e)
            return []

    # ------------------------------------------------------------------
    # Task handling
    # ------------------------------------------------------------------

    async def _handle_task(self, task_row: Dict[str, Any]) -> None:
        from src.core.observability import set_log_context, emit_event
        task_id = task_row.get("id", "unknown")
        session_id = task_row.get("session_id", "")
        # Correlate every line + event for this task with task_id/session_id.
        set_log_context(task_id=task_id, session_id=session_id)
        async with self._semaphore:
            # Claim — optimistic lock
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
            emit_event("task_claimed", backend=task_row.get("backend", ""))

            # Execute
            result = await _execute_task(task_row, self._backends)

            # Post result
            try:
                await asyncio.to_thread(
                    self._http.post,
                    f"/tasks/{task_id}/result",
                    {"node_id": self.cfg.node_id, **result},
                )
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

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._register()

        nudge_listener = asyncio.create_task(
            _run_nudge_listener(self.cfg.tailscale_ip, self.cfg.api_port, self._poll_now)
        )
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        poller = asyncio.create_task(self._poll_loop())
        job_watcher = asyncio.create_task(self._job_watcher_loop())

        # Install SIGTERM handler — loop.add_signal_handler is Unix-only.
        # On Windows fall back to signal.signal; if SIGTERM isn't supported
        # at all (Windows), skip silently — Ctrl+C (KeyboardInterrupt) is the
        # shutdown path there.
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        except (NotImplementedError, OSError):
            try:
                signal.signal(signal.SIGTERM, lambda *_: self._on_sigterm())
            except (OSError, ValueError):
                pass

        try:
            await self._shutdown.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("event=keyboard_interrupt node_id=%s", self.cfg.node_id)

        # Drain: wait up to 30s for active tasks
        logger.info("event=draining active=%d", len(self._active))
        if self._active:
            _, pending = await asyncio.wait(
                list(self._active.values()), timeout=30
            )
            for t in pending:
                t.cancel()

        for t in (poller, heartbeat, nudge_listener, job_watcher):
            t.cancel()
        await asyncio.gather(poller, heartbeat, nudge_listener, job_watcher, return_exceptions=True)

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

    agent = WorkerAgent()
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
