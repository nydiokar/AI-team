"""
ClaudeCodeBackend wraps the Claude Code CLI.

First turn:  claude -p "<message>" --output-format stream-json ...
Resume turn: claude --resume <backend_session_id> --output-format stream-json -p "<message>"

These methods are synchronous and are called via asyncio.to_thread() by the
orchestrator, so they must NOT use asyncio internally.

The backend_session_id is extracted from Claude's JSON output field `session_id`
and stored in the gateway Session record for subsequent resumes.
"""
import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

from src.core.process_utils import ensure_node_on_path, terminate_many_popen
from src.core.interfaces import CodingBackend, ExecutionResult, Session
from src.core.telemetry import TelemetryContext, telemetry_subprocess_env

logger = logging.getLogger(__name__)

_DEFAULT_TOOLS = ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob", "Bash"]


def _resolve_model(session: Session) -> Optional[str]:
    """Resolve the model for this session via the shared catalog logic."""
    try:
        from config.models import resolve_model
        return resolve_model(session)
    except Exception:
        return None


def _mcp_jobs_configured() -> bool:
    """True if setup_mcp.py has registered the jobs server in ~/.claude.json."""
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        return "jobs" in cfg.get("mcpServers", {})
    except Exception:
        return False
_STATUS_LABELS = {
    "A": "created",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type_changed",
    "U": "unmerged",
    "?": "untracked",
}


def _run_git(cwd: str, args: List[str], timeout: int = 10) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except Exception:
        return None


def _normalize_path(raw_path: str) -> str:
    if " -> " in raw_path:
        return raw_path.split(" -> ", 1)[1].strip()
    return raw_path.strip()


def _status_code(status: str) -> str:
    status = (status or "").replace(" ", "")
    for char in status:
        if char != ".":
            return char
    return ""


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, "modified")


def _file_fingerprint(root: str, rel_path: str) -> str:
    path = Path(root) / rel_path
    if not path.exists():
        return "<missing>"
    if path.is_dir():
        return "<dir>"
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except Exception:
        try:
            stat = path.stat()
            return f"<stat:{stat.st_size}:{int(stat.st_mtime_ns)}>"
        except Exception:
            return "<unreadable>"


def _snapshot_worktree(cwd: str) -> Dict[str, Dict[str, str]]:
    """Capture the current dirty worktree state keyed by repo-relative path."""
    stdout = _run_git(cwd, ["status", "--porcelain=v1"])
    if stdout is None:
        return {}

    snapshot: Dict[str, Dict[str, str]] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        status = line[:2]
        path = _normalize_path(line[3:])
        snapshot[path] = {
            "status": status,
            "fingerprint": _file_fingerprint(cwd, path),
        }
    return snapshot


def _line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return sum(1 for _ in handle)
    except Exception:
        return 0


def _current_diff_stats(cwd: str, path: str, status_code: str) -> Dict[str, Optional[int]]:
    """Return current diff stats for a path.

    These stats are net stats against the repo baseline. For files that were
    already dirty before the turn, they are not guaranteed to be strictly
    incremental for just this turn.
    """
    if status_code in ("A", "?"):
        return {"added": _line_count(Path(cwd) / path), "deleted": 0}

    stdout = _run_git(cwd, ["diff", "--numstat", "--", path])
    if stdout:
        for line in stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                added_raw, deleted_raw = parts[0], parts[1]
                added = None if added_raw == "-" else int(added_raw)
                deleted = None if deleted_raw == "-" else int(deleted_raw)
                return {"added": added, "deleted": deleted}

    if status_code == "D":
        return {"added": 0, "deleted": None}
    return {"added": None, "deleted": None}


def _compute_turn_changes(cwd: str, before: Dict[str, Dict[str, str]], after: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    changes: List[Dict[str, Any]] = []
    for path in sorted(after.keys()):
        prev = before.get(path)
        curr = after[path]
        if prev and prev.get("status") == curr.get("status") and prev.get("fingerprint") == curr.get("fingerprint"):
            continue
        status = curr.get("status", "")
        code = _status_code(status)
        stats = _current_diff_stats(cwd, path, code)
        changes.append(
            {
                "path": path,
                "git_status": status,
                "change_type": _status_label(code),
                "added_lines": stats["added"],
                "deleted_lines": stats["deleted"],
            }
        )
    return changes


def _extract_output(payload: Any) -> str:
    """Best-effort extraction of the final user-visible answer from Claude payloads."""
    if isinstance(payload, str):
        return payload.strip()

    if isinstance(payload, list):
        for item in reversed(payload):
            text = _extract_output(item)
            if text:
                return text
        return ""

    if not isinstance(payload, dict):
        return ""

    for key in ("result", "content", "output", "message", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        text = _extract_output(value)
        if text:
            return text

    for key in ("messages", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            text = _extract_output(value)
            if text:
                return text

    if payload.get("type") in ("text", "message") and isinstance(payload.get("text"), str):
        return payload["text"].strip()

    return ""


def _extract_text_blocks(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    parts: List[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts).strip()


class ClaudeCodeBackend(CodingBackend):
    """CodingBackend implementation for the Claude Code CLI.

    Delegates to a ClaudeDriver (SDK continuous driver by default, print/resume
    as fallback). The driver choice is made once at construction time and applies
    to all sessions managed by this backend instance.

    The existing _run/_build_cmd methods are kept for run_oneoff and for the
    test suite that asserts on _build_cmd output. New multi-turn session calls
    go through the driver boundary.
    """

    def __init__(self, driver_type: str = "auto"):
        from src.backends.claude_driver import build_driver, ClaudePrintResumeDriver
        self._driver = build_driver(driver_type)
        # Fallback driver for one-off calls and tests that directly use _build_cmd
        self._fallback = ClaudePrintResumeDriver()
        # Legacy process tracking for terminate_active_processes compatibility
        self._exe = shutil.which("claude") or "claude"
        self._session_procs: dict[str, subprocess.Popen] = {}
        self._oneoff_procs: set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()

    def create_session(self, session: Session, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        proc_env = self._build_proc_env(session.backend_session_id or str(uuid.uuid4()), telemetry_context)
        before_snapshot = _snapshot_worktree(session.repo_path) if session.repo_path else {}

        result = self._driver.start_session(
            session,
            session.last_user_message,
            model=_resolve_model(session),
            telemetry_context=telemetry_context,
            proc_env=proc_env,
        )
        self._observe_driver_state(session, result)
        result = self._observe_cache_health(session, result)
        if session.repo_path:
            after_snapshot = _snapshot_worktree(session.repo_path)
            result.file_changes = _compute_turn_changes(session.repo_path, before_snapshot, after_snapshot)
            result.files_modified = [item["path"] for item in result.file_changes]
        return result

    def resume_session(self, session: Session, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        # Guards are checked BEFORE the live-call gate so they work in test mode too.

        # Guard: if session was lost after worker restart, don't silently resume
        # via print/resume into stale context.
        if session.driver_status == "lost" and self._driver.driver_type() != "print_resume":
            return ExecutionResult(
                success=False,
                output="",
                errors=[
                    "Claude session was lost after a worker restart and cannot be resumed "
                    "by the continuous driver. Start a new session or explicitly request "
                    "fallback resume."
                ],
                error_class="session_lost",
            )

        # Guard: if cache is unhealthy twice, block silent print/resume continuation
        if (
            session.cache_health == "unhealthy"
            and session.cache_unhealthy_count >= 2
            and self._driver.driver_type() == "print_resume"
        ):
            return ExecutionResult(
                success=False,
                output="",
                errors=[
                    f"Claude session cache is unhealthy ({session.cache_unhealthy_count} times). "
                    "Context is being fully recreated every turn, burning subscription quota. "
                    "Start a new session to reset cache health."
                ],
                error_class="cache_unhealthy",
            )

        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        proc_env = self._build_proc_env(session.backend_session_id, telemetry_context)
        before_snapshot = _snapshot_worktree(session.repo_path) if session.repo_path else {}

        result = self._driver.send_turn(
            session,
            message,
            model=_resolve_model(session),
            telemetry_context=telemetry_context,
            proc_env=proc_env,
        )
        self._observe_driver_state(session, result)
        result = self._observe_cache_health(session, result)
        if session.repo_path:
            after_snapshot = _snapshot_worktree(session.repo_path)
            result.file_changes = _compute_turn_changes(session.repo_path, before_snapshot, after_snapshot)
            result.files_modified = [item["path"] for item in result.file_changes]
        return result

    def run_oneoff(self, cwd: str, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        return self._run(cwd, message, resume_id=None, session_id=None, session_key=None, model=None, telemetry_context=telemetry_context)

    def cancel(self, session: Session) -> None:
        self._driver.cancel(session)
        # Also cancel any legacy procs (compat)
        with self._proc_lock:
            proc = self._session_procs.get(session.session_id)
        if proc is not None:
            terminate_many_popen([proc])

    def close(self, session: Session) -> None:
        self._driver.close(session)

    def mark_sessions_lost(self) -> None:
        """Called on worker restart — all live SDK sessions are orphaned."""
        from src.backends.claude_driver import ClaudeSDKClientDriver
        if isinstance(self._driver, ClaudeSDKClientDriver):
            # Clear the session map; driver_status is updated by the orchestrator
            for sid in list(self._driver._sessions.keys()):
                self._driver.mark_lost(sid)

    def terminate_active_processes(self) -> None:
        # For SDK driver, close all live sessions
        from src.backends.claude_driver import ClaudeSDKClientDriver, ClaudePrintResumeDriver
        if isinstance(self._driver, ClaudeSDKClientDriver):
            for sdk_sess in list(self._driver._sessions.values()):
                sdk_sess.close()
        elif isinstance(self._driver, ClaudePrintResumeDriver):
            self._driver.terminate_active_processes()
        # Legacy procs
        with self._proc_lock:
            procs = list(self._session_procs.values()) + list(self._oneoff_procs)
        terminate_many_popen(procs)

    @staticmethod
    def _build_proc_env(session_id: Optional[str], telemetry_context: Optional[TelemetryContext]) -> dict:
        proc_env = ensure_node_on_path()
        if session_id:
            proc_env["SESSION_ID"] = session_id
        proc_env.update(telemetry_subprocess_env(telemetry_context))
        return proc_env

    @staticmethod
    def _observe_driver_state(session: Session, result: ExecutionResult) -> None:
        """Persist the selected driver mode on the Session object after a turn."""
        if session.driver_type == "sdk":
            session.driver_status = "live" if result.success else (session.driver_status or "")
        elif session.driver_type == "print_resume":
            session.driver_status = "closed"

    @staticmethod
    def _observe_cache_health(session: Session, result: ExecutionResult) -> ExecutionResult:
        """Parse cache stats from result and mutate session health fields in-place."""
        from src.backends.claude_driver import parse_cache_stats_from_ndjson, CacheStats
        stats = parse_cache_stats_from_ndjson(result.raw_stdout)
        if stats is None:
            return result
        if stats.is_unhealthy:
            session.cache_health = "unhealthy"
            session.cache_unhealthy_count += 1
            logger.warning(
                "Cache unhealthy for session %s: creation=%d hit_ratio=%.2f (count=%d)",
                session.session_id,
                stats.cache_creation,
                stats.hit_ratio,
                session.cache_unhealthy_count,
            )
        else:
            session.cache_health = "healthy"
        return result

    def _run(
        self,
        cwd: str,
        message: str,
        resume_id: Optional[str],
        session_id: Optional[str],
        session_key: Optional[str],
        model: Optional[str] = None,
        telemetry_context: Optional[TelemetryContext] = None,
    ) -> ExecutionResult:
        # Cost guard: refuse to spawn the (paid) Claude CLI under test mode.
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        start = time.time()
        cmd = self._build_cmd(resume_id, session_id, model)
        before_snapshot = _snapshot_worktree(cwd) if cwd else {}

        try:
            from config import config as _cfg
            inactivity_sec = max(60, int(getattr(_cfg.system, "inactivity_timeout_sec", 600)))
        except Exception:
            inactivity_sec = 600

        # Propagate session ID so the MCP watch_job tool can route notifications
        # back to the right Telegram chat without the agent having to pass it explicitly.
        proc_env = ensure_node_on_path()
        if session_id:
            proc_env["SESSION_ID"] = session_id
        proc_env.update(telemetry_subprocess_env(telemetry_context))

        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd or None,
                env=proc_env,
                creationflags=_NO_WINDOW,
            )
            self._register_process(proc, session_key)

            # Write stdin and close it immediately so Claude starts processing.
            # communicate() is NOT used — it blocks until EOF on both pipes,
            # preventing incremental reading and inactivity detection.
            proc.stdin.write(message.encode())
            proc.stdin.close()

            stdout_q: queue.Queue = queue.Queue()
            stderr_q: queue.Queue = queue.Queue()
            _SENTINEL = object()

            def _reader(pipe, q: queue.Queue) -> None:
                try:
                    for raw_line in pipe:
                        q.put(raw_line)
                finally:
                    q.put(_SENTINEL)

            stdout_thread = threading.Thread(target=_reader, args=(proc.stdout, stdout_q), daemon=True)
            stderr_thread = threading.Thread(target=_reader, args=(proc.stderr, stderr_q), daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            stdout_lines: List[bytes] = []
            stderr_lines: List[bytes] = []
            stdout_done = False
            stderr_done = False
            killed_for_inactivity = False

            while not (stdout_done and stderr_done):
                # Drain stdout with inactivity timeout
                if not stdout_done:
                    try:
                        item = stdout_q.get(timeout=inactivity_sec)
                        if item is _SENTINEL:
                            stdout_done = True
                        else:
                            stdout_lines.append(item)
                    except queue.Empty:
                        # No output for inactivity_sec — process is hung; kill it
                        logger.warning(
                            "claude inactivity timeout after %.0fs (no stdout) — terminating pid=%s",
                            inactivity_sec,
                            proc.pid,
                        )
                        killed_for_inactivity = True
                        terminate_many_popen([proc])
                        stdout_done = True
                        # Fall through to stderr flush below (don't break — we want stderr)

                # Drain stderr non-blockingly while stdout is being processed
                if not stderr_done:
                    while True:
                        try:
                            item = stderr_q.get_nowait()
                            if item is _SENTINEL:
                                stderr_done = True
                                break
                            stderr_lines.append(item)
                        except queue.Empty:
                            break

            # Flush any remaining stdout/stderr after the main loop
            for q_ref, lines_ref, done_flag in (
                (stdout_q, stdout_lines, True),
                (stderr_q, stderr_lines, False),
            ):
                if done_flag:  # stdout: just drain non-blocking
                    while True:
                        try:
                            item = q_ref.get_nowait()
                            if item is not _SENTINEL:
                                lines_ref.append(item)
                        except queue.Empty:
                            break
                else:  # stderr: wait briefly for the reader thread to finish
                    while True:
                        try:
                            item = q_ref.get(timeout=5.0)
                            if item is _SENTINEL:
                                break
                            lines_ref.append(item)
                        except queue.Empty:
                            break

            stdout_thread.join(timeout=5.0)
            stderr_thread.join(timeout=5.0)

            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                pass  # process resisted termination; returncode stays None
            returncode = proc.returncode if proc.returncode is not None else -1

            stdout = b"".join(stdout_lines).decode(errors="replace")
            stderr = b"".join(stderr_lines).decode(errors="replace")
            elapsed = time.time() - start

            if killed_for_inactivity:
                elapsed_min = int(elapsed // 60)
                inactivity_min = int(inactivity_sec // 60)
                return ExecutionResult(
                    success=False,
                    output="",
                    errors=[
                        f"Claude process killed after {inactivity_min}m of inactivity "
                        f"(total elapsed: {elapsed_min}m). The process produced no output — "
                        f"it may have been waiting for I/O or hung on a tool call. "
                        f"Adjust GATEWAY_INACTIVITY_TIMEOUT_SEC (currently {inactivity_sec}) to tune this."
                    ],
                    execution_time=elapsed,
                    raw_stdout=stdout,
                    raw_stderr=stderr,
                )

            result = self._parse(stdout, stderr, returncode, elapsed, known_session_id=session_id or resume_id or "")
            if cwd:
                after_snapshot = _snapshot_worktree(cwd)
                result.file_changes = _compute_turn_changes(cwd, before_snapshot, after_snapshot)
                result.files_modified = [item["path"] for item in result.file_changes]
            return result

        except Exception as e:
            return ExecutionResult(
                success=False,
                output="",
                errors=[str(e)],
                execution_time=time.time() - start,
            )
        finally:
            if proc is not None:
                self._unregister_process(proc, session_key)

    def _build_cmd(self, resume_id: Optional[str], session_id: Optional[str], model: Optional[str] = None) -> List[str]:
        if resume_id:
            cmd = [
                self._exe,
                "--resume",
                resume_id,
                "--verbose",
                "--output-format",
                "stream-json",
                # --include-partial-messages omitted: duplicated usage rows per
                # assistant message and inflated telemetry (P0 finding).
                "--dangerously-skip-permissions",
                "-p",
            ]
        else:
            cmd = [
                self._exe,
                "--verbose",
                "--output-format",
                "stream-json",
                "--dangerously-skip-permissions",
                "-p",
            ]
            if session_id:
                cmd.extend(["--session-id", session_id])

        # --model is a per-invocation setting (verified: works with --resume too).
        if model:
            cmd.extend(["--model", model])

        tools = list(_DEFAULT_TOOLS)
        if _mcp_jobs_configured():
            tools.append("mcp__jobs__watch_job")

        cmd.extend(["--allowedTools", ",".join(tools)])
        return cmd

    def _register_process(self, proc: subprocess.Popen, session_key: Optional[str]) -> None:
        stale_proc: Optional[subprocess.Popen] = None
        with self._proc_lock:
            if session_key:
                stale_proc = self._session_procs.get(session_key)
                self._session_procs[session_key] = proc
            else:
                self._oneoff_procs.add(proc)
        if stale_proc is not None and stale_proc is not proc:
            terminate_many_popen([stale_proc])

    def _unregister_process(self, proc: subprocess.Popen, session_key: Optional[str]) -> None:
        with self._proc_lock:
            if session_key:
                current = self._session_procs.get(session_key)
                if current is proc:
                    self._session_procs.pop(session_key, None)
            else:
                self._oneoff_procs.discard(proc)

    @staticmethod
    def _parse(stdout: str, stderr: str, returncode: int, elapsed: float, known_session_id: str = "") -> ExecutionResult:
        success = returncode == 0
        backend_session_id = known_session_id or ""
        output = stdout.strip()
        parsed_output = None
        parsed_errors: List[str] = []

        if stdout:
            try:
                data = json.loads(stdout)
                backend_session_id = data.get("session_id", "") or backend_session_id
                parsed_output = data
                output = _extract_output(data)
                maybe_errors = data.get("errors")
                if isinstance(maybe_errors, list):
                    parsed_errors = [str(item).strip() for item in maybe_errors if str(item).strip()]
            except Exception:
                assistant_text = ""
                delta_parts: List[str] = []
                final_result: Optional[Dict[str, Any]] = None
                for line in stdout.splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            d: Dict[str, Any] = json.loads(line)
                            if "session_id" in d:
                                backend_session_id = d["session_id"]
                            if d.get("type") == "assistant":
                                message = d.get("message")
                                text = ""
                                if isinstance(message, dict):
                                    text = _extract_text_blocks(message.get("content"))
                                if text:
                                    assistant_text = text
                            elif d.get("type") == "stream_event":
                                event = d.get("event")
                                if isinstance(event, dict) and event.get("type") == "content_block_delta":
                                    delta = event.get("delta")
                                    if isinstance(delta, dict) and delta.get("type") == "text_delta":
                                        text = delta.get("text")
                                        if isinstance(text, str) and text:
                                            delta_parts.append(text)
                            elif d.get("type") == "result":
                                final_result = d
                                maybe_errors = d.get("errors")
                                if isinstance(maybe_errors, list):
                                    parsed_errors = [str(item).strip() for item in maybe_errors if str(item).strip()]
                            candidate = _extract_output(d)
                            if candidate:
                                output = candidate
                                parsed_output = d
                        except Exception:
                            pass
                if assistant_text:
                    output = assistant_text
                elif delta_parts:
                    output = "".join(delta_parts).strip()
                if final_result is not None:
                    if output:
                        final_result["assistant_text"] = output
                    parsed_output = final_result
                    maybe_errors = final_result.get("errors")
                    if isinstance(maybe_errors, list):
                        parsed_errors = [str(item).strip() for item in maybe_errors if str(item).strip()]
                if not output:
                    output = stdout.strip()

        errors = [stderr.strip()] if stderr and not success else []
        if not success and not errors and parsed_errors:
            errors = parsed_errors
        if not success and not errors:
            errors = [f"Claude exited with code {returncode}"]
        return ExecutionResult(
            success=success,
            output=output,
            backend_session_id=backend_session_id,
            errors=errors,
            execution_time=elapsed,
            raw_stdout=stdout,
            raw_stderr=stderr,
            parsed_output=parsed_output,
            return_code=returncode,
        )
