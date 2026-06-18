"""
CodexBackend — wraps the OpenAI Codex CLI (`codex`).

First turn:  codex exec --json --dangerously-bypass-approvals-and-sandbox [-C <dir>] -
Resume turn: codex exec resume <thread_id> --json --dangerously-bypass-approvals-and-sandbox -

Prompt is passed via stdin (the trailing `-` argument).
Session ID is extracted from the `thread_id` field in the `thread.started` NDJSON event.

Synchronous — called via asyncio.to_thread() by the orchestrator.
"""
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from src.core.process_utils import ensure_node_on_path, terminate_many_popen
from src.core.interfaces import CodingBackend, ExecutionResult, Session

logger = logging.getLogger(__name__)


def _mcp_jobs_configured() -> bool:
    """True if setup_mcp.py has registered the jobs server in Codex's config."""
    try:
        content = (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
        return 'name = "jobs"' in content
    except Exception:
        return False


class CodexBackend(CodingBackend):

    def __init__(self):
        self._exe = self._resolve_exe(ensure_node_on_path())
        self._session_procs: dict[str, subprocess.Popen] = {}
        self._oneoff_procs: set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()

    def create_session(self, session: Session) -> ExecutionResult:
        return self._run(session.repo_path, session.last_user_message, resume_id=None, session_key=session.session_id)

    def resume_session(self, session: Session, message: str) -> ExecutionResult:
        return self._run(session.repo_path, message, resume_id=session.backend_session_id or None, session_key=session.session_id)

    def run_oneoff(self, cwd: str, message: str) -> ExecutionResult:
        return self._run(cwd, message, resume_id=None, session_key=None)

    def cancel(self, session: Session) -> None:
        with self._proc_lock:
            proc = self._session_procs.get(session.session_id)
        if proc is not None:
            terminate_many_popen([proc])

    def close(self, session: Session) -> None:
        pass

    def terminate_active_processes(self) -> None:
        with self._proc_lock:
            procs = list(self._session_procs.values()) + list(self._oneoff_procs)
        terminate_many_popen(procs)

    def _run(self, cwd: str, message: str, resume_id: Optional[str], session_key: Optional[str]) -> ExecutionResult:
        # Cost guard: refuse to spawn the Codex CLI under test mode.
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("codex")
        start = time.time()

        try:
            from config import config as _cfg
            inactivity_sec = max(60, int(getattr(_cfg.system, "inactivity_timeout_sec", 600)))
        except Exception:
            inactivity_sec = 600

        proc_env = ensure_node_on_path()
        if session_key:
            proc_env["SESSION_ID"] = session_key

        cmd = self._build_cmd(resume_id, cwd)
        cmd[0] = self._resolve_exe(proc_env)

        if sys.platform == "win32":
            node_hint = os.getenv("CODEX_NODE_PATH") or os.getenv("NODE_EXE")
            current_path = proc_env.get("PATH") or proc_env.get("Path") or ""
            if node_hint:
                node_dir = str(Path(node_hint).expanduser().parent)
                current_path = node_dir + os.pathsep + current_path
                proc_env["PATH"] = current_path
                proc_env["Path"] = current_path
            if shutil.which("node", path=current_path) is None:
                return ExecutionResult(
                    success=False,
                    output="",
                    errors=[
                        "Codex CLI requires node.exe, but node is not on PATH for this worker process. "
                        "Install Node.js or set CODEX_NODE_PATH/NODE_EXE to the full node.exe path in the worker .env."
                    ],
                    execution_time=time.time() - start,
                )

        logger.info(
            "event=codex_spawn exe=%s node=%s cwd=%s session_key=%s",
            cmd[0],
            shutil.which("node", path=(proc_env.get("PATH") or proc_env.get("Path") or "")),
            cwd,
            session_key or "(oneoff)",
        )

        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd or None,
                env=proc_env,
            )
            self._register_process(proc, session_key)

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
                if not stdout_done:
                    try:
                        item = stdout_q.get(timeout=inactivity_sec)
                        if item is _SENTINEL:
                            stdout_done = True
                        else:
                            stdout_lines.append(item)
                    except queue.Empty:
                        logger.warning(
                            "codex inactivity timeout after %.0fs (no stdout) — terminating pid=%s",
                            inactivity_sec,
                            proc.pid,
                        )
                        killed_for_inactivity = True
                        terminate_many_popen([proc])
                        stdout_done = True

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

            for q_ref, lines_ref, done_flag in (
                (stdout_q, stdout_lines, True),
                (stderr_q, stderr_lines, False),
            ):
                if done_flag:
                    while True:
                        try:
                            item = q_ref.get_nowait()
                            if item is not _SENTINEL:
                                lines_ref.append(item)
                        except queue.Empty:
                            break
                else:
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
                pass
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
                        f"Codex process killed after {inactivity_min}m of inactivity "
                        f"(total elapsed: {elapsed_min}m). The process produced no output — "
                        f"it may have been waiting for I/O or hung on a tool call. "
                        f"Adjust GATEWAY_INACTIVITY_TIMEOUT_SEC (currently {inactivity_sec}) to tune this."
                    ],
                    execution_time=elapsed,
                    raw_stdout=stdout,
                    raw_stderr=stderr,
                )

            return self._parse(stdout, stderr, returncode, elapsed)

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

    @staticmethod
    def _resolve_exe(env: Optional[dict] = None) -> str:
        path_value = ""
        if env:
            path_value = env.get("Path") or env.get("PATH") or ""
        return shutil.which("codex", path=path_value or None) or shutil.which("codex") or "codex"

    def _build_cmd(self, resume_id: Optional[str], cwd: Optional[str]) -> List[str]:
        if resume_id:
            return [
                self._exe, "exec", "resume", resume_id,
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "-",
            ]
        cmd = [
            self._exe, "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if cwd:
            cmd += ["-C", cwd]
        cmd.append("-")
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
    def _parse(stdout: str, stderr: str, returncode: int, elapsed: float) -> ExecutionResult:
        success = returncode == 0
        backend_session_id = ""
        output_parts: List[str] = []
        parsed_output: Optional[dict] = None

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue

            event_type = event.get("type", "")

            if event_type == "thread.started":
                backend_session_id = event.get("thread_id", "")
                parsed_output = event

            elif event_type == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    text = item.get("text", "")
                    if text:
                        output_parts.append(text)

            elif event_type == "turn.completed":
                parsed_output = event

        output = "\n".join(output_parts).strip()
        if not output:
            output = stdout.strip()

        errors: List[str] = []
        if not success:
            if stderr:
                errors.append(stderr.strip())
            if not errors:
                errors.append(f"codex exited with code {returncode}")

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
