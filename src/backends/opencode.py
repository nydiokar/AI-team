"""
OpenCodeBackend — wraps the OpenCode CLI (opencode).

First turn:  opencode run --dir <repo> --format json --title <title> "<prompt>"
Resume turn: opencode run --dir <repo> --format json --session <session_id> "<prompt>"

Optional flags: --model <provider/model>  --agent <agent_name>

Session ID is extracted from JSON events in stdout.  If not found there, a
fallback query (opencode session list --format json) is attempted.  If the
session ID is still unknown the task is marked needs_manual_attention to
prevent accidentally continuing the wrong session.

Synchronous — called via asyncio.to_thread() by the orchestrator.
"""
import json
import logging
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.process_utils import terminate_many_popen
from src.core.interfaces import CodingBackend, ExecutionResult, Session

logger = logging.getLogger(__name__)

# Repo-level lock: only one mutating OpenCode run per repo path at a time.
# Key: normalised absolute repo path string.  Value: threading.Lock().
_repo_locks: Dict[str, threading.Lock] = {}
_repo_locks_mutex = threading.Lock()


def _get_repo_lock(repo_path: str) -> threading.Lock:
    key = str(Path(repo_path).resolve())
    with _repo_locks_mutex:
        if key not in _repo_locks:
            _repo_locks[key] = threading.Lock()
        return _repo_locks[key]


def _run_git(cwd: str, args: List[str], timeout: int = 10) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except Exception:
        return None


def _git_changed_files(cwd: str) -> List[str]:
    out = _run_git(cwd, ["status", "--porcelain"])
    if not out:
        return []
    files = []
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip())
    return files


def _is_dirty(cwd: str) -> bool:
    out = _run_git(cwd, ["status", "--porcelain"])
    return bool(out and out.strip())


class OpenCodeBackend(CodingBackend):
    """OpenCode CLI backend."""

    def __init__(self) -> None:
        self._exe = shutil.which("opencode") or "opencode"
        self._session_procs: Dict[str, subprocess.Popen] = {}
        self._oneoff_procs: set = set()
        self._proc_lock = threading.Lock()

    # ------------------------------------------------------------------
    # CodingBackend interface
    # ------------------------------------------------------------------

    def create_session(self, session: Session) -> ExecutionResult:
        return self._run(
            cwd=session.repo_path,
            message=session.last_user_message,
            session_id=None,
            title=session.session_id,   # use gateway session ID as title for traceability
            model=self._session_model(session),
            agent=self._session_agent(session),
            session_key=session.session_id,
        )

    def resume_session(self, session: Session, message: str) -> ExecutionResult:
        oc_session_id = session.backend_session_id
        if not oc_session_id:
            return ExecutionResult(
                success=False,
                output="",
                errors=[
                    "OpenCode session ID is not set for this session. "
                    "Cannot resume without an explicit session ID. "
                    "Status: needs_manual_attention"
                ],
            )
        return self._run(
            cwd=session.repo_path,
            message=message,
            session_id=oc_session_id,
            title=None,
            model=self._session_model(session),
            agent=self._session_agent(session),
            session_key=session.session_id,
        )

    def run_oneoff(self, cwd: str, message: str) -> ExecutionResult:
        return self._run(
            cwd=cwd,
            message=message,
            session_id=None,
            title=None,
            model=None,
            agent=None,
            session_key=None,
        )

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

    # ------------------------------------------------------------------
    # Core run
    # ------------------------------------------------------------------

    def _run(
        self,
        cwd: str,
        message: str,
        session_id: Optional[str],
        title: Optional[str],
        model: Optional[str],
        agent: Optional[str],
        session_key: Optional[str],
    ) -> ExecutionResult:
        start = time.time()

        # --- git safety pre-checks ---
        pre_check = self._pre_run_git_check(cwd)
        if pre_check is not None:
            return pre_check

        # --- repo-level lock ---
        repo_lock = _get_repo_lock(cwd)
        if not repo_lock.acquire(blocking=False):
            return ExecutionResult(
                success=False,
                output="",
                errors=[
                    f"Another OpenCode task is already running against repo: {cwd}. "
                    "Concurrent mutations are not allowed. Wait for the current task to finish."
                ],
            )

        try:
            return self._run_locked(
                cwd=cwd,
                message=message,
                session_id=session_id,
                title=title,
                model=model,
                agent=agent,
                session_key=session_key,
                start=start,
            )
        finally:
            repo_lock.release()

    def _run_locked(
        self,
        cwd: str,
        message: str,
        session_id: Optional[str],
        title: Optional[str],
        model: Optional[str],
        agent: Optional[str],
        session_key: Optional[str],
        start: float,
    ) -> ExecutionResult:
        cmd = self._build_cmd(
            cwd=cwd,
            message=message,
            session_id=session_id,
            title=title,
            model=model,
            agent=agent,
        )

        try:
            from config import config as _cfg
            inactivity_sec = max(60, int(getattr(_cfg.system, "inactivity_timeout_sec", 600)))
            oc_cfg = getattr(_cfg, "opencode", None)
            allow_dirty = bool(getattr(oc_cfg, "allow_dirty_repo", False)) if oc_cfg else False
            collect_diff = bool(getattr(oc_cfg, "collect_diff", True)) if oc_cfg else True
        except Exception:
            inactivity_sec = 600
            allow_dirty = False
            collect_diff = True

        # Second dirty check (after acquiring the lock) using the config value.
        # The first check in _pre_run_git_check uses allow_dirty=False conservatively;
        # here we re-check with the actual config value so the config is respected.
        if not allow_dirty and _is_dirty(cwd):
            return ExecutionResult(
                success=False,
                output="",
                errors=[
                    f"Repository at {cwd} has uncommitted changes. "
                    "OpenCode requires a clean working tree by default. "
                    "Set OPENCODE_ALLOW_DIRTY_REPO=true to override, or commit/stash your changes."
                ],
            )

        logger.info(
            "event=opencode_run cmd=%s cwd=%s session_id=%s session_key=%s",
            cmd,
            cwd,
            session_id or "(new)",
            session_key or "(oneoff)",
        )

        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd or None,
            )
            self._register_process(proc, session_key)

            stdout_q: queue.Queue = queue.Queue()
            stderr_q: queue.Queue = queue.Queue()
            _SENTINEL = object()

            def _reader(pipe: Any, q: queue.Queue) -> None:
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
                            "opencode inactivity timeout after %.0fs (no stdout) — terminating pid=%s",
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

            # Flush remaining output
            for q_ref, lines_ref, wait in (
                (stdout_q, stdout_lines, False),
                (stderr_q, stderr_lines, True),
            ):
                if not wait:
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
                        f"OpenCode process killed after {inactivity_min}m of inactivity "
                        f"(total elapsed: {elapsed_min}m). The process produced no output — "
                        f"it may have been waiting for input or hung on a tool call. "
                        f"Adjust GATEWAY_INACTIVITY_TIMEOUT_SEC (currently {inactivity_sec}) to tune this."
                    ],
                    execution_time=elapsed,
                    raw_stdout=stdout,
                    raw_stderr=stderr,
                )

            result = self._parse(stdout, stderr, returncode, elapsed, known_session_id=session_id or "")

            # Session ID fallback: if the run started a new session and we still
            # don't have an ID, query the session list to recover it.
            if not result.backend_session_id and session_id is None and returncode == 0:
                recovered = self._recover_session_id(cwd=cwd, title=title)
                if recovered:
                    result.backend_session_id = recovered
                    logger.info("event=opencode_session_id_recovered id=%s", recovered)
                else:
                    logger.warning(
                        "event=opencode_session_id_missing cwd=%s title=%s — marking needs_manual_attention",
                        cwd,
                        title,
                    )
                    result.errors = list(result.errors or []) + [
                        "OpenCode session ID could not be extracted from output or session list. "
                        "Status: needs_manual_attention — continuation is blocked until the session ID is resolved."
                    ]
                    result.success = False

            # Post-run git diff collection
            if collect_diff and cwd:
                result.files_modified = _git_changed_files(cwd)
                diff_stat = _run_git(cwd, ["diff", "--stat", "HEAD"]) or ""
                diff = _run_git(cwd, ["diff", "HEAD"]) or ""
                if result.parsed_output is None:
                    result.parsed_output = {}
                if isinstance(result.parsed_output, dict):
                    result.parsed_output["git_diff_stat"] = diff_stat
                    result.parsed_output["git_diff"] = diff

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

    # ------------------------------------------------------------------
    # Command builder
    # ------------------------------------------------------------------

    def _build_cmd(
        self,
        cwd: str,
        message: str,
        session_id: Optional[str],
        title: Optional[str],
        model: Optional[str],
        agent: Optional[str],
    ) -> List[str]:
        """Build the opencode run argument list. Never shell-concatenates."""
        cmd = [self._exe, "run", "--dir", cwd, "--format", "json"]

        if model:
            cmd += ["--model", model]
        if agent:
            cmd += ["--agent", agent]

        if session_id:
            cmd += ["--session", session_id]
        elif title:
            cmd += ["--title", title]

        cmd.append(message)
        return cmd

    # ------------------------------------------------------------------
    # Output parser
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(stdout: str, stderr: str, returncode: int, elapsed: float, known_session_id: str = "") -> ExecutionResult:
        success = returncode == 0
        backend_session_id = known_session_id or ""
        output = ""
        parsed_output: Optional[Dict[str, Any]] = None
        parsed_errors: List[str] = []

        # OpenCode emits newline-delimited JSON events.
        # Known session ID fields: "sessionID", "session_id", "id" (inside a session object).
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event: Dict[str, Any] = json.loads(line)
            except Exception:
                continue

            # Session ID extraction — try multiple field shapes defensively.
            for field in ("sessionID", "session_id", "session"):
                val = event.get(field)
                if isinstance(val, str) and val:
                    backend_session_id = val
                    break
                if isinstance(val, dict):
                    for sub in ("id", "sessionID", "session_id"):
                        sub_val = val.get(sub)
                        if isinstance(sub_val, str) and sub_val:
                            backend_session_id = sub_val
                            break

            event_type = event.get("type", "") or event.get("event", "")

            # Real OpenCode event schema (v1.x):
            #   type="text"  → part.text contains the assistant text chunk
            #   type="message"/"assistant"/"content" → legacy/generic shapes
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            if event_type == "text":
                chunk = part.get("text") or ""
                if isinstance(chunk, str) and chunk.strip():
                    output = (output + chunk) if output else chunk
            elif event_type in ("message", "assistant", "content"):
                for key in ("content", "text", "message", "output"):
                    val = event.get(key) or part.get(key)
                    if isinstance(val, str) and val.strip():
                        output = val.strip()
                        break

            # Error events
            if event_type in ("error",):
                msg = event.get("message") or event.get("error") or part.get("message") or ""
                if isinstance(msg, str) and msg:
                    parsed_errors.append(msg)

            parsed_output = event  # keep last event for diagnostics

        if not output:
            output = stdout.strip()

        errors: List[str] = []
        if not success:
            if stderr and stderr.strip():
                errors.append(stderr.strip())
            if parsed_errors:
                errors.extend(parsed_errors)
            if not errors:
                errors.append(f"opencode exited with code {returncode}")

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

    # ------------------------------------------------------------------
    # Session ID recovery via session list
    # ------------------------------------------------------------------

    def _recover_session_id(self, cwd: str, title: Optional[str]) -> Optional[str]:
        """Query `opencode session list` and match the most recent session for this repo/title."""
        try:
            result = subprocess.run(
                [self._exe, "session", "list", "--format", "json", "--max-count", "10"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return None
        except Exception:
            return None

        cwd_resolved = str(Path(cwd).resolve())

        # Output may be a JSON array or newline-delimited JSON objects.
        raw = result.stdout.strip()
        sessions: List[Dict[str, Any]] = []
        if raw.startswith("["):
            try:
                sessions = json.loads(raw)
            except Exception:
                pass
        else:
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        sessions.append(json.loads(line))
                    except Exception:
                        pass

        if not sessions:
            return None

        # Score each session: title match > path match > most recent
        def _score(s: Dict[str, Any]) -> int:
            score = 0
            s_title = str(s.get("title") or "")
            s_path = str(s.get("path") or s.get("dir") or s.get("cwd") or "")
            if title and s_title and s_title == title:
                score += 10
            if cwd_resolved and s_path:
                try:
                    if str(Path(s_path).resolve()) == cwd_resolved:
                        score += 5
                except Exception:
                    pass
            return score

        ranked = sorted(sessions, key=lambda s: (_score(s), s.get("createdAt") or s.get("created_at") or ""), reverse=True)
        best = ranked[0]
        for field in ("id", "sessionID", "session_id"):
            val = best.get(field)
            if isinstance(val, str) and val:
                return val
        return None

    # ------------------------------------------------------------------
    # Git pre-run check
    # ------------------------------------------------------------------

    def _pre_run_git_check(self, cwd: str) -> Optional[ExecutionResult]:
        """Return an error ExecutionResult if the repo fails basic safety checks."""
        if not cwd:
            return ExecutionResult(
                success=False,
                output="",
                errors=["repo_path is required for OpenCode runs."],
            )
        p = Path(cwd)
        if not p.exists():
            return ExecutionResult(
                success=False,
                output="",
                errors=[f"Repository path does not exist: {cwd}"],
            )
        if not p.is_dir():
            return ExecutionResult(
                success=False,
                output="",
                errors=[f"Repository path is not a directory: {cwd}"],
            )
        # Verify it is a git repo
        toplevel = _run_git(cwd, ["rev-parse", "--show-toplevel"])
        if toplevel is None:
            return ExecutionResult(
                success=False,
                output="",
                errors=[f"Path is not inside a Git repository: {cwd}"],
            )

        # Allowed root check — reuse the existing config value (claude.allowed_root).
        # OpenCode can override this with OPENCODE_ALLOWED_ROOT if needed.
        try:
            import os
            oc_root = os.getenv("OPENCODE_ALLOWED_ROOT") or os.getenv("CLAUDE_ALLOWED_ROOT")
            if oc_root:
                resolved = p.resolve()
                allowed = Path(oc_root).resolve()
                if not (resolved == allowed or allowed in resolved.parents):
                    return ExecutionResult(
                        success=False,
                        output="",
                        errors=[f"Repository path {cwd} is outside the allowed root: {oc_root}"],
                    )
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------
    # Process registry helpers
    # ------------------------------------------------------------------

    def _register_process(self, proc: subprocess.Popen, session_key: Optional[str]) -> None:
        stale: Optional[subprocess.Popen] = None
        with self._proc_lock:
            if session_key:
                stale = self._session_procs.get(session_key)
                self._session_procs[session_key] = proc
            else:
                self._oneoff_procs.add(proc)
        if stale is not None and stale is not proc:
            terminate_many_popen([stale])

    def _unregister_process(self, proc: subprocess.Popen, session_key: Optional[str]) -> None:
        with self._proc_lock:
            if session_key:
                current = self._session_procs.get(session_key)
                if current is proc:
                    self._session_procs.pop(session_key, None)
            else:
                self._oneoff_procs.discard(proc)

    # ------------------------------------------------------------------
    # Helpers to read model/agent from session metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _session_model(session: Session) -> Optional[str]:
        meta = session.task_history[-1] if session.task_history else {}
        explicit = meta.get("opencode_model") or None
        if explicit:
            return explicit
        try:
            from config import config as _cfg
            return getattr(_cfg.opencode, "default_model", None) or None
        except Exception:
            return None

    @staticmethod
    def _session_agent(session: Session) -> Optional[str]:
        meta = session.task_history[-1] if session.task_history else {}
        explicit = meta.get("opencode_agent") or None
        if explicit:
            return explicit
        try:
            from config import config as _cfg
            return getattr(_cfg.opencode, "default_agent", None) or None
        except Exception:
            return None
