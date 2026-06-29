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

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

from src.core.process_utils import ensure_node_on_path, terminate_many_popen
from src.core.interfaces import CodingBackend, ExecutionResult, Session
from src.control.telemetry_sink import NullTelemetrySink
from src.core.telemetry import (
    EMITTER_PROCESS_INSTANCE_ID,
    TelemetryContext,
    build_event,
    new_telemetry_id,
    telemetry_subprocess_env,
)
from src.core.telemetry_adapters.codex import CodexTelemetryAdapter

logger = logging.getLogger(__name__)


def _resolve_model(session: Session) -> Optional[str]:
    """Resolve the model for this session via the shared catalog logic."""
    try:
        from config.models import resolve_model
        return resolve_model(session)
    except Exception:
        return None


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
        self._session_telemetry: dict[str, TelemetryContext] = {}
        self._oneoff_procs: set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()

    def create_session(self, session: Session, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        return self._run(session.repo_path, session.last_user_message, resume_id=None, session_key=session.session_id, model=_resolve_model(session), telemetry_context=telemetry_context, telemetry_sink=telemetry_sink)

    def resume_session(self, session: Session, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        return self._run(session.repo_path, message, resume_id=session.backend_session_id or None, session_key=session.session_id, model=_resolve_model(session), telemetry_context=telemetry_context, telemetry_sink=telemetry_sink)

    def run_oneoff(self, cwd: str, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        return self._run(cwd, message, resume_id=None, session_key=None, model=None, telemetry_context=telemetry_context, telemetry_sink=telemetry_sink)

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

    def _run(
        self,
        cwd: str,
        message: str,
        resume_id: Optional[str],
        session_key: Optional[str],
        model: Optional[str] = None,
        telemetry_context: Optional[TelemetryContext] = None,
        telemetry_sink=None,
    ) -> ExecutionResult:
        # Cost guard: refuse to spawn the Codex CLI under test mode.
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("codex")
        start = time.time()
        start_monotonic = time.monotonic()

        try:
            from config import config as _cfg
            inactivity_sec = max(60, int(getattr(_cfg.system, "inactivity_timeout_sec", 600)))
        except Exception:
            inactivity_sec = 600

        proc_env = ensure_node_on_path()
        if session_key:
            proc_env["SESSION_ID"] = session_key
        proc_env.update(telemetry_subprocess_env(telemetry_context))

        cmd = self._build_cmd(resume_id, cwd, model)
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

        sink = telemetry_sink or NullTelemetrySink()
        process_instance_id: Optional[str] = None
        adapter: Optional[CodexTelemetryAdapter] = None

        def _emit(events) -> None:
            try:
                if isinstance(events, list):
                    sink.emit_many(events)
                else:
                    sink.emit(events)
            except Exception:
                logger.warning("event=codex_telemetry_emit_failed", exc_info=True)

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
            self._register_process(
                proc,
                session_key,
                telemetry_context=telemetry_context,
                emit=_emit,
            )
            if telemetry_context is not None:
                process_instance_id = new_telemetry_id("proc")
                adapter = CodexTelemetryAdapter(
                    telemetry_context,
                    emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                )
                _emit(
                    build_event(
                        "process.spawned",
                        turn_id=telemetry_context.turn_id,
                        session_id=telemetry_context.session_id,
                        node_id=telemetry_context.node_id,
                        emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                        source=telemetry_context.source,
                        invocation_id=telemetry_context.invocation_id,
                        backend="codex",
                        model=telemetry_context.model,
                        pid=proc.pid,
                        attributes={
                            "process_instance_id": process_instance_id,
                            "process_role": "agent",
                            "executable_name": "codex",
                        },
                    )
                )
                _emit(adapter.coverage_events())

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
                            if adapter is not None:
                                try:
                                    _emit(adapter.consume_line(item.decode(errors="replace")))
                                except Exception:
                                    logger.warning(
                                        "event=codex_telemetry_parse_failed", exc_info=True
                                    )
                    except queue.Empty:
                        logger.warning(
                            "codex inactivity timeout after %.0fs (no stdout) — terminating pid=%s",
                            inactivity_sec,
                            proc.pid,
                        )
                        killed_for_inactivity = True
                        if telemetry_context is not None:
                            _emit(
                                build_event(
                                    "process.timeout_detected",
                                    turn_id=telemetry_context.turn_id,
                                    session_id=telemetry_context.session_id,
                                    node_id=telemetry_context.node_id,
                                    emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                                    source=telemetry_context.source,
                                    invocation_id=telemetry_context.invocation_id,
                                    backend="codex",
                                    model=telemetry_context.model,
                                    pid=proc.pid,
                                    attributes={
                                        "timeout_kind": "backend_inactivity_timeout",
                                        "timeout_ms": inactivity_sec * 1000,
                                    },
                                )
                            )
                            _emit(
                                build_event(
                                    "process.termination_requested",
                                    turn_id=telemetry_context.turn_id,
                                    session_id=telemetry_context.session_id,
                                    node_id=telemetry_context.node_id,
                                    emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                                    source=telemetry_context.source,
                                    invocation_id=telemetry_context.invocation_id,
                                    backend="codex",
                                    model=telemetry_context.model,
                                    pid=proc.pid,
                                    attributes={"reason_code": "inactivity_timeout"},
                                )
                            )
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
                                if q_ref is stdout_q and adapter is not None:
                                    _emit(adapter.consume_line(item.decode(errors="replace")))
                        except queue.Empty:
                            break
                else:
                    while True:
                        try:
                            item = q_ref.get(timeout=5.0)
                            if item is _SENTINEL:
                                break
                            lines_ref.append(item)
                            if q_ref is stdout_q and adapter is not None:
                                _emit(adapter.consume_line(item.decode(errors="replace")))
                        except queue.Empty:
                            break

            stdout_thread.join(timeout=5.0)
            stderr_thread.join(timeout=5.0)

            reaped = True
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                reaped = False
            returncode = proc.returncode if proc.returncode is not None else -1
            if telemetry_context is not None and process_instance_id is not None:
                if reaped:
                    _emit(
                        build_event(
                            "process.exited",
                            turn_id=telemetry_context.turn_id,
                            session_id=telemetry_context.session_id,
                            node_id=telemetry_context.node_id,
                            emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                            source=telemetry_context.source,
                            invocation_id=telemetry_context.invocation_id,
                            backend="codex",
                            model=telemetry_context.model,
                            pid=proc.pid,
                            attributes={
                                "process_instance_id": process_instance_id,
                                "exit_code": returncode,
                                "signal": abs(returncode) if returncode < 0 else None,
                                "duration_ms": round(
                                    (time.monotonic() - start_monotonic) * 1000
                                ),
                            },
                        )
                    )
                else:
                    _emit(
                        build_event(
                            "process.exit_unknown",
                            turn_id=telemetry_context.turn_id,
                            session_id=telemetry_context.session_id,
                            node_id=telemetry_context.node_id,
                            emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                            source=telemetry_context.source,
                            invocation_id=telemetry_context.invocation_id,
                            backend="codex",
                            model=telemetry_context.model,
                            pid=proc.pid,
                            attributes={
                                "process_instance_id": process_instance_id,
                                "reason_code": "wait_timeout",
                            },
                        )
                    )

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
            try:
                sink.flush()
            except Exception:
                logger.warning("event=codex_telemetry_flush_failed", exc_info=True)

    @staticmethod
    def _resolve_exe(env: Optional[dict] = None) -> str:
        path_value = ""
        if env:
            path_value = env.get("Path") or env.get("PATH") or ""
        return shutil.which("codex", path=path_value or None) or shutil.which("codex") or "codex"

    def _build_cmd(self, resume_id: Optional[str], cwd: Optional[str], model: Optional[str] = None) -> List[str]:
        # -m is valid on both `exec` and `exec resume` (verified via --help).
        if resume_id:
            cmd = [self._exe, "exec", "resume", resume_id]
            if model:
                cmd += ["-m", model]
            cmd += [
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "-",
            ]
            return cmd
        cmd = [self._exe, "exec"]
        if model:
            cmd += ["-m", model]
        cmd += [
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if cwd:
            cmd += ["-C", cwd]
        cmd.append("-")
        return cmd

    def _register_process(
        self,
        proc: subprocess.Popen,
        session_key: Optional[str],
        *,
        telemetry_context: Optional[TelemetryContext] = None,
        emit=None,
    ) -> None:
        stale_proc: Optional[subprocess.Popen] = None
        stale_context: Optional[TelemetryContext] = None
        with self._proc_lock:
            if session_key:
                stale_proc = self._session_procs.get(session_key)
                stale_context = self._session_telemetry.get(session_key)
                self._session_procs[session_key] = proc
                if telemetry_context is not None:
                    self._session_telemetry[session_key] = telemetry_context
            else:
                self._oneoff_procs.add(proc)
        if stale_proc is not None and stale_proc is not proc:
            if (
                emit is not None
                and telemetry_context is not None
                and stale_context is not None
                and stale_context.invocation_id != telemetry_context.invocation_id
            ):
                emit(
                    build_event(
                        "invocation.duplicate_detected",
                        turn_id=telemetry_context.turn_id,
                        session_id=telemetry_context.session_id,
                        node_id=telemetry_context.node_id,
                        emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                        source=telemetry_context.source,
                        invocation_id=telemetry_context.invocation_id,
                        backend="codex",
                        model=telemetry_context.model,
                        attributes={
                            "duplicate_of_invocation_id": stale_context.invocation_id,
                            "confidence": "probable",
                            "rule": "session_process_replacement",
                        },
                    )
                )
            terminate_many_popen([stale_proc])

    def _unregister_process(self, proc: subprocess.Popen, session_key: Optional[str]) -> None:
        with self._proc_lock:
            if session_key:
                current = self._session_procs.get(session_key)
                if current is proc:
                    self._session_procs.pop(session_key, None)
                    self._session_telemetry.pop(session_key, None)
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

        # Collect error-type events from stdout JSON stream.
        error_events: List[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("type") in ("error", "session.error", "turn.error"):
                msg = (
                    event.get("message")
                    or event.get("error", {}).get("message")
                    or str(event)
                )
                if msg:
                    error_events.append(msg)

        errors: List[str] = []
        if not success:
            if stderr:
                errors.append(stderr.strip())
            for msg in error_events:
                if msg not in errors:
                    errors.append(msg)
            if not errors:
                # Last resort: surface raw stdout so the real error isn't hidden.
                raw = stdout.strip()
                errors.append(raw if raw else f"codex exited with code {returncode}")

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
