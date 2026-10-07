"""
OpenCode backends — CLI and server modes.

CLI mode (OpenCodeBackend):
  First turn:  opencode run --dir <repo> --format json --title <title> "<prompt>"
  Resume turn: opencode run --dir <repo> --format json --session <session_id> "<prompt>"

Server mode (OpenCodeServerBackend):
  Manages a persistent `opencode serve` subprocess and talks to it via HTTP.
  POST /session → create session
  POST /session/{id}/message → blocking send + receive (returns full message with parts)
  POST /session/{id}/abort  → cancel running generation
  DELETE /session/{id}      → close session

  Advantages over CLI: no cold-start per turn, no stdout parsing, clean HTTP JSON,
  token/cost data in responses, `session.diff` events, abort support.

Both are synchronous — called via asyncio.to_thread() by the orchestrator.
"""
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

from src.core.process_utils import (
    ensure_node_on_path, process_gone_proof, process_identity, terminate_many_popen,
)
from src.core.interfaces import CodingBackend, ExecutionResult, Session
from src.core.telemetry import TelemetryContext, telemetry_subprocess_env

logger = logging.getLogger(__name__)


def _mcp_jobs_configured() -> bool:
    """True if setup_mcp.py has registered the jobs server in OpenCode's config."""
    try:
        cfg = json.loads(
            (Path.home() / ".config" / "opencode" / "config.json").read_text(encoding="utf-8")
        )
        return "jobs" in cfg.get("mcp", {})
    except Exception:
        return False


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
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
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



# Markers opencode emits (in stdout JSON events or on stderr) when its own
# permission system blocks a tool call. These mean the agent was *prevented*
# from acting, not that it chose to stop.
_PERMISSION_BLOCK_MARKERS = (
    "auto-rejecting",
    "rejected permission",
    "user rejected permission",
    "the user rejected",
    "permission denied",
)

# Forward-looking phrases that signal the model only stated an *intention* to
# work rather than reporting completed work. Used together with "no side
# effects" to catch the false-success / intent-only pattern. Kept deliberately
# narrow: bare openers like "let me " / "i'll " are NOT here because they very
# often begin substantive, completed replies and would cause false positives.
_INTENT_ONLY_PREFIXES = (
    "understood",
    "starting with",
    "starting by",
    "let me start",
    "i'll start",
    "i will start",
    "let me begin",
    "i'll begin",
    "working autonomously",
)


def _detect_permission_block(stdout: str, stderr: str) -> str:
    """Return the matched marker if opencode auto-rejected a permission, else ''.

    Only markers that indicate an *actual rejection* count. We deliberately do
    NOT treat the bare token "external_directory" as a block: opencode prints it
    in permission *prompts* even for calls that are subsequently allowed, so
    matching it alone causes false positives on successful runs. A genuine
    rejection always co-occurs with "auto-rejecting" or a "rejected" phrase.
    """
    haystack = f"{stderr}\n{stdout}".lower()
    for marker in _PERMISSION_BLOCK_MARKERS:
        if marker in haystack:
            return marker
    return ""


def _looks_intent_only(output: str) -> bool:
    """True if the text only announces intent (no evidence of completed work).

    Conservative: only fires for short outputs that *start* with a forward-looking
    phrase. A long, substantive reply is never treated as intent-only.
    """
    text = (output or "").strip().lower()
    if not text:
        return True  # empty output with a permission block is definitely a dead end
    if len(text) > 600:
        return False
    return any(text.startswith(p) for p in _INTENT_ONLY_PREFIXES)


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

    def create_session(self, session: Session, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        return self._run(
            cwd=session.repo_path,
            message=session.last_user_message,
            session_id=None,
            title=session.session_id,   # use gateway session ID as title for traceability
            model=self._session_model(session),
            agent=self._session_agent(session),
            session_key=session.session_id,
            telemetry_context=telemetry_context,
        )

    def resume_session(self, session: Session, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        oc_session_id = session.backend_session_id
        if not oc_session_id:
            # No session ID — fall back to a fresh session rather than dead-ending.
            logger.warning(
                "event=opencode_cli_resume_no_id gateway_session=%s — falling back to create_session",
                session.session_id,
            )
            session.last_user_message = message
            return self.create_session(
                session,
                telemetry_context=telemetry_context,
                telemetry_sink=telemetry_sink,
            )
        return self._run(
            cwd=session.repo_path,
            message=message,
            session_id=oc_session_id,
            title=None,
            model=self._session_model(session),
            agent=self._session_agent(session),
            session_key=session.session_id,
            telemetry_context=telemetry_context,
        )

    def run_oneoff(self, cwd: str, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        return self._run(
            cwd=cwd,
            message=message,
            session_id=None,
            title=None,
            model=None,
            agent=None,
            session_key=None,
            telemetry_context=telemetry_context,
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
        telemetry_context: Optional[TelemetryContext] = None,
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
                telemetry_context=telemetry_context,
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
        telemetry_context: Optional[TelemetryContext] = None,
    ) -> ExecutionResult:
        # Cost guard: blocked under test mode unless OpenCode e2e is opted in.
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("opencode")
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
            inactivity_sec = max(60, int(getattr(_cfg.system, "inactivity_timeout_sec", 36000)))
            oc_cfg = getattr(_cfg, "opencode", None)
            collect_diff = bool(getattr(oc_cfg, "collect_diff", True)) if oc_cfg else True
        except Exception:
            inactivity_sec = 36000
            collect_diff = True

        logger.info(
            "event=opencode_run cmd=%s cwd=%s session_id=%s session_key=%s",
            cmd,
            cwd,
            session_id or "(new)",
            session_key or "(oneoff)",
        )

        proc: Optional[subprocess.Popen] = None
        proc_env = ensure_node_on_path()
        if session_key:
            proc_env["SESSION_ID"] = session_key
        proc_env.update(telemetry_subprocess_env(telemetry_context))

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd or None,
                env=proc_env,
                creationflags=_NO_WINDOW,
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
            for q_ref, lines_ref, already_done, wait in (
                (stdout_q, stdout_lines, stdout_done, False),
                (stderr_q, stderr_lines, stderr_done, True),
            ):
                if already_done:
                    continue
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

                # Un-flag a suspect "permission_block": if the run actually
                # modified files, real work happened — the rejected permission
                # was incidental, not a dead-end. The gate in _parse runs before
                # git state is known, so we correct it here.
                if (
                    not result.success
                    and result.error_class == "permission_block"
                    and (result.files_modified or diff.strip())
                ):
                    logger.info(
                        "event=opencode_suspect_cleared reason=files_modified files=%s",
                        result.files_modified,
                    )
                    result.success = True
                    result.error_class = ""
                    # Drop the dead-end error we added in _parse.
                    result.errors = [
                        e for e in (result.errors or [])
                        if "dead-end" not in e.lower()
                    ]

            # Auto-commit so the working tree is clean for subsequent runs.
            # OpenCode enforces a clean tree before each run; without this the
            # second task in the same session will always fail.
            if result.success and cwd and _git_changed_files(cwd):
                commit_label = session_key or title or "opencode-task"
                self._auto_commit(cwd, commit_label)

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
        # Track step_finish reasons to detect interrupted/truncated generation.
        # Normal reasons: "stop" (natural end), "tool-calls" (tool invocation).
        # "unknown" means the model generation was cut off mid-response.
        step_finish_reasons: List[str] = []

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
            elif event_type == "step_finish":
                reason = part.get("reason") or event.get("reason") or ""
                if reason:
                    step_finish_reasons.append(reason)

            # Error events
            if event_type in ("error",):
                msg = event.get("message") or event.get("error") or part.get("message") or ""
                if isinstance(msg, str) and msg:
                    parsed_errors.append(msg)

            parsed_output = event  # keep last event for diagnostics

        if not output:
            output = stdout.strip()

        # Detect truncated generation: step_finish with reason="unknown" and partial output.
        # OpenCode exits 0 but the model was interrupted before completing its response.
        truncated = any(r == "unknown" for r in step_finish_reasons) and bool(output)
        if truncated:
            logger.warning(
                "event=opencode_truncated_output step_finish_reasons=%s output_len=%d",
                step_finish_reasons,
                len(output),
            )
            output = output + "\n\n_(Note: the response above was cut off — OpenCode reported an interrupted generation. The full reply may be missing.)_"

        errors: List[str] = []
        if not success:
            if stderr and stderr.strip():
                errors.append(stderr.strip())
            if parsed_errors:
                errors.extend(parsed_errors)
            if not errors:
                errors.append(f"opencode exited with code {returncode}")

        # Suspect-run / dead-end detection. opencode can exit 0 having done no
        # real work because it hit a permission wall (e.g. it tried to read a
        # path outside the repo and opencode auto-rejected it) and then gave up.
        # Such a run reports success with optimistic, intent-only text ("Working
        # autonomously...", "Starting with...") and zero side effects — which is
        # indistinguishable from a real success unless we look. Flip it to a
        # failure so the orchestrator retries / surfaces it instead of relaying
        # a false "it's going to work" to the user.
        error_class = ""
        if success:
            blocked = _detect_permission_block(stdout, stderr)
            if blocked:
                no_side_effects = (
                    not any(r == "stop" for r in step_finish_reasons)  # never reached a natural end
                )
                intent_only = _looks_intent_only(output)
                if no_side_effects and intent_only:
                    success = False
                    error_class = "permission_block"
                    errors.append(
                        "OpenCode stopped early on an auto-rejected permission "
                        f"({blocked}) and produced only intent-only text without "
                        "completing the work. This is a dead-end, not a success. "
                        "Widen the opencode permission/allowed paths or keep the "
                        "agent's actions inside the repo."
                    )

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
            error_class=error_class,
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
                text=True, encoding="utf-8", errors="replace",
                timeout=15,
                creationflags=_NO_WINDOW,
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
    # Auto-commit helper
    # ------------------------------------------------------------------

    @staticmethod
    def _auto_commit(cwd: str, label: str) -> None:
        """Stage all changes and commit so the working tree is clean for the next run."""
        try:
            add = subprocess.run(
                ["git", "add", "-A"],
                cwd=cwd,
                capture_output=True,
                timeout=30,
                creationflags=_NO_WINDOW,
            )
            if add.returncode != 0:
                logger.warning("event=opencode_auto_commit_add_failed cwd=%s", cwd)
                return
            msg = f"chore(opencode): auto-commit after task [{label}]"
            commit = subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=cwd,
                capture_output=True,
                timeout=30,
                creationflags=_NO_WINDOW,
            )
            if commit.returncode == 0:
                logger.info("event=opencode_auto_committed cwd=%s label=%s", cwd, label)
            else:
                # Nothing to commit is fine (returncode 1 with "nothing to commit")
                stderr = commit.stderr.decode(errors="replace").strip()
                if "nothing to commit" not in stderr:
                    logger.warning("event=opencode_auto_commit_failed cwd=%s stderr=%s", cwd, stderr)
        except Exception as e:
            logger.warning("event=opencode_auto_commit_exception cwd=%s err=%s", cwd, e)

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
        # Resolve via the shared catalog logic: session.model → config default →
        # catalog default. (Previously read a dead task_history["opencode_model"]
        # key that nothing ever wrote — see MODEL_PICKER_PLAN.md R2.)
        try:
            from config.models import resolve_model
            return resolve_model(session)
        except Exception:
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


# ---------------------------------------------------------------------------
# OpenCode server-mode backend
# ---------------------------------------------------------------------------

def _find_free_port(preferred: int) -> int:
    """Return `preferred` if available, otherwise any free port.

    Both checks use SO_REUSEADDR=False (the default) so a port in TIME_WAIT
    is reported as in-use and we fall back to a kernel-assigned port.
    The caller must pass the returned port to the child process immediately;
    there is an inherent TOCTOU window, but it is small for loopback sockets.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    # Let the OS choose; bind on 0 then read back the assigned port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_MSG_ID_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def managed_message_id(turn_uuid: str) -> str:
    """[A82 Step 4b] Deterministic OpenCode ``messageID`` for a managed turn.

    Derived from the carrier's write-ahead ``turn_uuid`` so the native user
    message (and every assistant reply whose ``parentID`` is it) is attributable
    to exactly that attempt even after a carrier crash. Format verified against
    the installed opencode 1.18.32 ``Identifier`` module: the server only
    requires the ``msg`` prefix (``ID ... does not start with msg`` otherwise);
    native ids are ``msg_`` + 12 hex (6 bytes) + 14 base62 chars, mirrored here.
    History order is ``time_created`` then ``id`` (``MessageV2.page`` /
    ``latest``), so the hash-derived "time" bytes cannot reorder the turn."""
    digest = hashlib.sha256(f"ai-team.managed-turn:{turn_uuid}".encode()).digest()
    tail = "".join(_MSG_ID_BASE62[b % 62] for b in digest[6:20])
    return f"msg_{digest[:6].hex()}{tail}"


class _ManagedTurn(BaseModel):
    """[A82 Step 4b] In-memory record of one managed attempt on this backend
    instance (never persisted; the carrier's claim record is the durable one)."""

    model_config = ConfigDict(extra="forbid")

    turn_uuid: str
    session_id: str
    key: str = ""
    oc_session_id: str = ""
    kind: str = "turn"                      # "turn" | "compaction"
    message_id: str = ""                    # our user message id ("" for compaction)
    known_ids: List[str] = Field(default_factory=list)  # compaction: pre-submit ids
    phase: str = "reserved"                 # reserved | submitted | held
    cancel_armed: bool = False
    cancel_delivered: bool = False
    submitted_at: float = 0.0               # time.monotonic()
    server_identity: Optional[Dict[str, Any]] = None
    forgotten: bool = False                 # [m4] terminal server-side: no late delivery
    late_pending: bool = False              # [m4] late capture not yet attempted


class _LateManagedOutcome(BaseModel):
    """[A82 step 4 rework, m4] The real reply of a held managed turn, handed to
    the carrier's proactive sink — the same duck-typed shape its
    ``_capture_late_managed_result`` binds by ``managed_turn_uuid`` only."""

    late_managed: bool = True
    managed_turn_uuid: str
    output: str
    is_error: bool
    error_text: str = ""
    error_class: str = ""
    backend_session_id: str = ""
    raw_ndjson: str = ""


# [A82 step 4 rework, m7] Write-ahead record of a FIRST turn's native session
# (gateway session id → native id), written before the prompt is submitted so a
# recovery on that turn (the gateway never learns the id from a result) keeps
# the session's history instead of creating a second native session. Cleared
# once a terminal result carries the id to the carrier.
# [A82 pre-cutover, m7] It lives in the carrier's own state dir — the same
# ``WORKER_STATE_DIR`` (default ``logs/carrier_state``) base the carrier's result
# spool and claim store use (``WorkerAgent._carrier_state_dir``), so it shares
# their persistence (e.g. the container's state volume). Cleared also when a
# recovery resolves (late capture delivered the id) or the gateway knows the id.
def _native_store_path() -> Path:
    root = os.getenv("WORKER_STATE_DIR") or os.path.join("logs", "carrier_state")
    return Path(root) / "opencode-native-sessions.sqlite3"


def _native_store(sql: str, args: tuple) -> List[tuple]:
    path = _native_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS native_sessions (session_id TEXT PRIMARY KEY, "
                         "server_key TEXT NOT NULL, native_id TEXT NOT NULL)")
            return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _clear_native(session_id: str, native_id: str = "") -> None:
    """[A82 pre-cutover, m7] Drop ``session_id``'s write-ahead row (only the one
    naming ``native_id`` when given). Never creates the store."""
    if not _native_store_path().exists():
        return
    try:
        if native_id:
            _native_store("DELETE FROM native_sessions WHERE session_id = ? AND native_id = ?",
                          (session_id, native_id))
        else:
            _native_store("DELETE FROM native_sessions WHERE session_id = ?", (session_id,))
    except (OSError, sqlite3.Error):
        logger.warning("event=opencode_native_store_clear_failed session=%s", session_id)


def _stored_native(session_id: str, key: str) -> str:
    try:
        rows = _native_store("SELECT native_id FROM native_sessions WHERE session_id = ? AND server_key = ?",
                             (session_id, key))
    except (OSError, sqlite3.Error):
        logger.warning("event=opencode_native_store_read_failed session=%s", session_id)
        return ""
    return str(rows[0][0]) if rows else ""


def _http_status(err: Optional[str]) -> Optional[int]:
    """HTTP status code embedded in an ``OpenCodeServerBackend._http`` error."""
    m = re.match(r"HTTP (\d{3}) ", err or "")
    return int(m.group(1)) if m else None


class OpenCodeServerBackend(CodingBackend):
    """OpenCode HTTP server backend.

    `opencode serve` has no per-request directory override — its working
    directory is fixed to the process's launch cwd for the lifetime of the
    server. To support sessions in different repos, we run one `opencode
    serve` process per distinct repo directory (keyed by resolved path) and
    launch each with `cwd` set to that directory.
    """

    def __init__(self) -> None:
        self._exe = shutil.which("opencode") or "opencode"
        self._procs: Dict[str, subprocess.Popen] = {}   # resolved dir -> process
        self._base_urls: Dict[str, str] = {}            # resolved dir -> base URL
        self._lock = threading.Lock()      # guards _procs / _base_urls
        self._repo_capacity = threading.BoundedSemaphore(8)
        self._server_slots = threading.BoundedSemaphore(8)
        self._server_slot_keys: set[str] = set()
        self._active_cancel: Dict[str, threading.Event] = {}
        # [A82 Step 4b] managed-turn bookkeeping (memory only).
        self._managed: Dict[str, _ManagedTurn] = {}          # turn_uuid -> attempt
        self._armed_cancels: set[str] = set()                # cancels for not-yet-seen turns
        self._native: Dict[str, tuple[str, str]] = {}        # gateway sid -> (server key, native id)
        self._proactive_sink: Any = None                     # [m4] carrier late-reply sink
        self._refused_since: Dict[str, float] = {}           # [m6] server key -> first refusal (monotonic)
        self._refused_last: Dict[str, float] = {}            # [m6] server key -> latest refusal (monotonic)

    @staticmethod
    def _server_key(repo_path: str) -> str:
        """Resolve a repo path to the key used to look up its dedicated server."""
        if not repo_path:
            return ""
        try:
            return str(Path(repo_path).resolve())
        except Exception:
            return repo_path

    # ------------------------------------------------------------------
    # CodingBackend interface
    # ------------------------------------------------------------------

    def create_session(self, session: Session, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        start = time.time()
        key = self._server_key(session.repo_path)
        err = self._ensure_server(key, session.repo_path)
        if err:
            return ExecutionResult(success=False, output="", errors=[err], execution_time=time.time() - start)

        agent = self._session_agent(session) or "build"
        model_id, provider_id = self._parse_model(self._session_model(session))

        create_body: Dict[str, Any] = {
            "title": session.session_id,
            "agent": agent,
        }

        oc_session, err = self._http(key, "POST", "/session", create_body)
        if err:
            return ExecutionResult(success=False, output="", errors=[err], execution_time=time.time() - start)

        oc_session_id: str = oc_session.get("id", "")
        if not oc_session_id:
            return ExecutionResult(
                success=False, output="", errors=["Server returned session without ID"],
                execution_time=time.time() - start,
            )

        # NOTE: do NOT PATCH /session/{id} to set the model. On opencode 1.16.2
        # that PATCH is a silent no-op that leaves the session in a corrupt state
        # (providerID="big-pickle", modelID="") and then 500s at message time
        # (ProviderModelNotFoundError). The supported way is to pass the model
        # inline in the message body, which _send_message does.
        session.backend_session_id = oc_session_id
        result = self._send_message(
            key=key,
            oc_session_id=oc_session_id,
            message=session.last_user_message,
            cwd=session.repo_path,
            start=start,
            model_id=model_id,
            provider_id=provider_id,
            telemetry_context=telemetry_context,
            telemetry_sink=telemetry_sink,
        )
        if result.error_class in {"capacity_exceeded", "repo_busy", "event_stream_unavailable"}:
            # These failures occur before prompt submission, so this newly
            # created empty session has no resumable user history to preserve.
            self._http(key, "DELETE", f"/session/{oc_session_id}")
            session.backend_session_id = ""
            result.backend_session_id = ""
        return result

    def resume_session(self, session: Session, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        start = time.time()
        oc_session_id = session.backend_session_id
        key = self._server_key(session.repo_path)

        # A missing native identity cannot safely resume this conversation. Do not
        # silently replace it with a blank session and lose continuity.
        if not oc_session_id:
            return ExecutionResult(False, "", errors=["OpenCode native session ID is missing; conversation cannot be resumed safely."], error_class="session_identity_missing")

        err = self._ensure_server(key, session.repo_path)
        if err:
            return ExecutionResult(success=False, output="", errors=[err], execution_time=time.time() - start)

        # Verify the session still exists (server may have restarted and lost it).
        info, sess_err = self._http(key, "GET", f"/session/{oc_session_id}")
        if sess_err or not info.get("id"):
            return ExecutionResult(False, "", backend_session_id=oc_session_id,
                errors=[sess_err or "Saved OpenCode session no longer exists; refusing to create a blank replacement."],
                error_class="session_unavailable", execution_time=time.time() - start)

        model_id, provider_id = self._parse_model(self._session_model(session))
        result = self._send_message(
            key=key,
            oc_session_id=oc_session_id,
            message=message,
            cwd=session.repo_path,
            start=start,
            model_id=model_id,
            provider_id=provider_id,
            telemetry_context=telemetry_context,
            telemetry_sink=telemetry_sink,
        )
        return result

    def run_oneoff(self, cwd: str, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        start = time.time()
        key = self._server_key(cwd)
        err = self._ensure_server(key, cwd)
        if err:
            return ExecutionResult(success=False, output="", errors=[err], execution_time=time.time() - start)

        body: Dict[str, Any] = {"title": "oneoff", "agent": "build"}

        oc_session, err = self._http(key, "POST", "/session", body)
        if err:
            return ExecutionResult(success=False, output="", errors=[err], execution_time=time.time() - start)

        oc_session_id = oc_session.get("id", "")
        result = self._send_message(key=key, oc_session_id=oc_session_id, message=message, cwd=cwd, start=start,
                                    telemetry_context=telemetry_context, telemetry_sink=telemetry_sink)

        # One-off sessions have no caller-owned resume handle, so delete this
        # explicitly temporary session after successful completion. Failed turns
        # remain available for diagnostics.
        if (result.success or result.error_class in {"capacity_exceeded", "repo_busy", "event_stream_unavailable"}) and oc_session_id:
            self._http(key, "DELETE", f"/session/{oc_session_id}")
        result.backend_session_id = ""
        return result

    def cancel(self, session: Session) -> None:
        oc_id = session.backend_session_id
        key = self._server_key(session.repo_path)
        with self._lock:
            cancel_event = self._active_cancel.get(oc_id)
        if cancel_event is not None:
            cancel_event.set()
        if oc_id and self._base_urls.get(key):
            self._http(key, "POST", f"/session/{oc_id}/abort")

    def close(self, session: Session) -> None:
        # OpenCode DELETE removes the session and all its data. Ordinary close
        # only releases gateway-side ownership; persisted history remains resumable.
        # [A82 pre-cutover] A closed session gets no late delivery: stop its held
        # attempts' late watchers (their entries stay, so quiescence still
        # follows native truth until resolved / forgotten).
        with self._lock:
            for entry in self._managed.values():
                if entry.phase == "held" and entry.session_id == (session.session_id or ""):
                    entry.forgotten = True
        return None

    def compact_session(self, session: Session) -> ExecutionResult:
        start = time.time()
        oc_id = session.backend_session_id
        if not oc_id:
            return ExecutionResult(False, "", errors=["OpenCode native session ID is missing."],
                                   error_class="session_identity_missing")
        key = self._server_key(session.repo_path)
        err = self._ensure_server(key, session.repo_path)
        if err:
            return ExecutionResult(False, "", backend_session_id=oc_id, errors=[err],
                                   error_class="server_unavailable", execution_time=time.time() - start)
        model_id, provider_id = self._parse_model(self._session_model(session))
        if not provider_id or not model_id:
            providers, provider_err = self._http(key, "GET", "/config/providers", timeout=10)
            defaults = providers.get("default") if isinstance(providers, dict) else None
            if not provider_err and isinstance(defaults, dict) and defaults:
                provider_id, model_id = next(iter(defaults.items()))
        if not provider_id or not model_id:
            return ExecutionResult(False, "", backend_session_id=oc_id,
                errors=["OpenCode has no selected or configured default model for native compaction."],
                error_class="model_unavailable", execution_time=time.time() - start)
        body = {"providerID": provider_id, "modelID": model_id}
        repo_lock = _get_repo_lock(session.repo_path)
        if not self._repo_capacity.acquire(blocking=False):
            return ExecutionResult(False, "", backend_session_id=oc_id,
                errors=["OpenCode server capacity is full."], error_class="capacity_exceeded")
        if not repo_lock.acquire(blocking=False):
            self._repo_capacity.release()
            return ExecutionResult(False, "", backend_session_id=oc_id,
                errors=["Another OpenCode task is already running against this repo."], error_class="repo_busy")
        try:
            summarized, err = self._http(key, "POST", f"/session/{oc_id}/summarize", body)
        finally:
            repo_lock.release()
            self._repo_capacity.release()
        if err is None and summarized is False:
            err = "OpenCode did not complete native session summarization."
        return ExecutionResult(not err, "", backend_session_id=oc_id, errors=[err] if err else [],
                               error_class="server_error" if err else "", execution_time=time.time() - start)

    def terminate_active_processes(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
            self._procs = {}
            self._base_urls = {}
            slot_count = len(self._server_slot_keys)
            self._server_slot_keys.clear()
            # Kill inside the lock so _ensure_server cannot start a new server
            # while the old processes are still alive and own their ports.
            if procs:
                terminate_many_popen(procs)
            for _ in range(slot_count):
                self._server_slots.release()

    # ------------------------------------------------------------------
    # [A82 Step 4b] Managed (protocol-1) turn contract — OpenCode native.
    #
    # Attribution: the user message is submitted under
    # ``managed_message_id(turn_uuid)``; the result is ONLY the assistant reply
    # whose ``parentID`` is that id. Never interrupts: a busy/unknown session is
    # a pre-submit conflict; ambiguity (lost ack, deadline, server death) is
    # ``recovery_required`` and the native turn is left running. Abort is
    # session-wide in OpenCode, so a cancel is delivered only while the running
    # (latest) user message is provably ours.
    # ------------------------------------------------------------------
    _MANAGED_POLL_SEC: float = 1.0
    _MANAGED_ACK_TIMEOUT_SEC: int = 30
    _MANAGED_RECONCILE_TRIES: int = 5
    _MANAGED_IDLE_GRACE_POLLS: int = 3
    _MANAGED_ABSENT_GRACE_SEC: float = 60.0
    _LATE_CAPTURE_SEC: float = 36000.0      # [m4] how long a held turn's reply is awaited
    _UNREACHABLE_TERMINATE_SEC: float = 60.0  # [m6] a LIVE serve refusing connections this long

    def set_proactive_sink(self, sink: Any) -> None:
        """[A82 step 4 rework, m4] Carrier sink for late managed replies."""
        self._proactive_sink = sink

    def supports_managed_turns(self) -> bool:
        """The opencode-server protocol supports every managed invariant
        (client-chosen messageID, status + history reconciliation, abort gated
        on the provably-running message). The CLI backend does not (default)."""
        return True

    def provision_sender_capability(self, session_id: str, token: Optional[str]) -> bool:
        """Fail closed. OpenCode's only MCP seams are per server PROCESS
        (``opencode.json`` / ``OPENCODE_CONFIG_CONTENT`` at launch) or per
        directory INSTANCE (``POST /mcp``, "add MCP server to the system"); there
        is no per-session MCP config. One ``opencode serve`` is shared by every
        session in the repo, so provisioning one session's scoped sender token
        would hand it to its neighbours (and a launch-time config would put the
        raw token in the child env / on disk). Agent send is therefore not
        available on OpenCode."""
        return False

    def _managed_deadline_sec(self) -> float:
        try:
            from config import config as _cfg
            return float(max(1, int(getattr(_cfg.opencode, "timeout_seconds", 1800))))
        except Exception:
            return 1800.0

    def run_managed_turn(self, session: Session, message: str, ownership: Any, *,
                         telemetry_context: Any = None, telemetry_sink: Any = None,
                         on_process: Any = None) -> ExecutionResult:
        return self._run_managed("turn", session, message, ownership, telemetry_context,
                                 telemetry_sink, on_process)

    def run_managed_compaction(self, session: Session, ownership: Any, *,
                               telemetry_context: Any = None, telemetry_sink: Any = None,
                               on_process: Any = None) -> ExecutionResult:
        """Native ``POST /session/{id}/summarize`` (synchronous; server-chosen
        ids, so the compaction message is identified as the new user message
        carrying a ``compaction`` part that did not exist before submit)."""
        return self._run_managed("compaction", session, "", ownership, telemetry_context,
                                 telemetry_sink, on_process)

    def _run_managed(self, kind: str, session: Session, message: str, ownership: Any,
                     telemetry_context: Any, telemetry_sink: Any, on_process: Any) -> ExecutionResult:
        from src.control.turn_queue import (
            OwnershipConflictError, RecoveryRequiredError, TurnQueueError,
        )

        if (getattr(ownership, "session_id", "") or "") != (session.session_id or ""):
            raise OwnershipConflictError("managed ownership does not match the session",
                                         task_id=getattr(ownership, "task_id", None))
        turn_uuid = str(getattr(ownership, "turn_uuid", "") or "")
        if not turn_uuid:
            raise OwnershipConflictError("managed OpenCode turn requires a turn_uuid (attribution key)",
                                         task_id=getattr(ownership, "task_id", None))
        start = time.time()
        entry = _ManagedTurn(turn_uuid=turn_uuid, session_id=session.session_id or "", kind=kind,
                             message_id=managed_message_id(turn_uuid) if kind == "turn" else "")
        keep = False
        try:
            if kind == "turn":
                return self._managed_turn_body(entry, session, message, start, telemetry_context,
                                               telemetry_sink, on_process)
            return self._managed_compaction_body(entry, session, start, on_process)
        except TurnQueueError as e:
            recovery = isinstance(e, RecoveryRequiredError)
            keep = recovery and entry.phase != "reserved"
            if keep:
                with self._lock:
                    entry.phase = "held"
                    late = kind == "turn" and bool(entry.message_id) and self._proactive_sink is not None
                    entry.late_pending = late
                if late:
                    threading.Thread(target=self._capture_late, args=(entry, session, start),
                                     name=f"opencode-late-{turn_uuid[:12]}", daemon=True).start()
            logger.warning("event=opencode_managed_%s kind=%s turn=%s err=%s",
                           "recovery" if recovery else "conflict", kind, turn_uuid, e)
            return ExecutionResult(
                False, "", backend_session_id=entry.oc_session_id or session.backend_session_id or "",
                errors=[str(e)], error_class="recovery_required" if recovery else "managed_conflict",
                execution_time=time.time() - start)
        finally:
            if not keep:
                with self._lock:
                    if self._managed.get(turn_uuid) is entry:
                        self._managed.pop(turn_uuid, None)

    def _capture_late(self, entry: _ManagedTurn, session: Session, start: float) -> None:
        """[A82 step 4 rework, m4] A held turn's real reply (parented to OUR
        derived message id) is still captured after the deadline and handed to
        the carrier's proactive sink as ``late_managed`` bound to the turn uuid
        (mirrors the Claude/Codex late-delivery contract). The session stays
        non-quiescent until this attempt was made. Never aborts."""
        response: Dict[str, Any] = {}
        try:
            response = self._managed_wait(entry, self._LATE_CAPTURE_SEC)
        except Exception as e:  # noqa: BLE001 — server lost / never recorded / window over
            logger.info("event=opencode_managed_late_capture_ended turn=%s why=%s", entry.turn_uuid, e)
        try:
            with self._lock:
                live = self._managed.get(entry.turn_uuid) is entry and not entry.forgotten
            sink = self._proactive_sink
            if response and live and sink is not None:
                result = self._managed_outcome(response, session, entry.oc_session_id,
                                               time.time() - start, None, None)
                sink(entry.session_id, _LateManagedOutcome(
                    managed_turn_uuid=entry.turn_uuid, output=result.output or "",
                    is_error=not result.success, error_text="; ".join(result.errors or []),
                    error_class=result.error_class or "", backend_session_id=entry.oc_session_id,
                    raw_ndjson=result.raw_stdout or ""))
                _clear_native(entry.session_id, entry.oc_session_id)  # [m7] recovery resolved: id delivered
        except Exception:  # noqa: BLE001 — the sink must never kill this thread silently
            logger.warning("event=opencode_managed_late_delivery_failed turn=%s", entry.turn_uuid,
                           exc_info=True)
        finally:
            with self._lock:
                entry.late_pending = False

    def _managed_reserve(self, entry: _ManagedTurn) -> None:
        """Register ``entry`` as THE active attempt of its session, or raise a
        typed conflict. A held (recovery) attempt blocks until it is natively
        resolved (or forgotten); the native probe runs outside the lock."""
        from src.control.turn_queue import OwnershipConflictError

        with self._lock:
            if entry.turn_uuid in self._managed:
                raise OwnershipConflictError("managed turn already active on this backend")
            others = [e for e in self._managed.values()
                      if e.session_id == entry.session_id
                      or (entry.oc_session_id and e.oc_session_id == entry.oc_session_id)]
        for other in others:
            if other.phase != "held" or other.late_pending or self._entry_in_flight(other) is not False:
                raise OwnershipConflictError("another managed turn of this session is in flight")
        with self._lock:
            for other in others:
                if self._managed.get(other.turn_uuid) is other:
                    self._managed.pop(other.turn_uuid, None)   # natively resolved hold
            if any(e.session_id == entry.session_id for e in self._managed.values()):
                raise OwnershipConflictError("another managed turn of this session is in flight")
            if entry.turn_uuid in self._armed_cancels:
                self._armed_cancels.discard(entry.turn_uuid)
                entry.cancel_armed = True
            self._managed[entry.turn_uuid] = entry

    def _managed_native_session(self, entry: _ManagedTurn, session: Session, start: float) -> Optional[ExecutionResult]:
        """Resolve the native session. A saved native id the server no longer
        knows is recovery — never re-created. [A82 step 4 rework, m7] A first
        turn's session is NOT created here: ``_managed_create_native`` does it
        only after the reserve / lock / idle gates passed; a first turn whose
        earlier attempt created one (write-ahead record) reuses it."""
        from src.control.turn_queue import RecoveryRequiredError

        key = self._server_key(session.repo_path)
        entry.key = key
        err = self._ensure_server(key, session.repo_path)
        if err:
            return ExecutionResult(False, "", backend_session_id=session.backend_session_id or "",
                                   errors=[err], error_class="server_unavailable",
                                   execution_time=time.time() - start)
        oc_id = session.backend_session_id or ""
        if oc_id:
            _clear_native(entry.session_id)  # [m7] the gateway knows its id: the record is stale
        if not oc_id and entry.kind == "turn":
            with self._lock:
                known = self._native.get(entry.session_id)
            oc_id = known[1] if known and known[0] == key else _stored_native(entry.session_id, key)
            if oc_id:
                session.backend_session_id = oc_id
        if oc_id:
            info, ierr = self._http(key, "GET", f"/session/{oc_id}", timeout=10)
            if ierr and _http_status(ierr) == 404:
                raise RecoveryRequiredError(
                    "saved OpenCode native session no longer exists; refusing to re-create it")
            if ierr or not isinstance(info, dict) or not info.get("id"):
                return ExecutionResult(False, "", backend_session_id=oc_id,
                                       errors=[ierr or "OpenCode session lookup returned no id"],
                                       error_class="server_unavailable", execution_time=time.time() - start)
        elif entry.kind != "turn":
            return ExecutionResult(False, "", errors=["OpenCode native session ID is missing."],
                                   error_class="session_identity_missing", execution_time=time.time() - start)
        if oc_id:
            entry.oc_session_id = oc_id
            with self._lock:
                self._native[entry.session_id] = (key, oc_id)
        return None

    def _managed_create_native(self, entry: _ManagedTurn, session: Session, start: float) -> Optional[ExecutionResult]:
        """[A82 step 4 rework, m7] Create a first turn's native session — only
        after every gate passed — and record it write-ahead (before the prompt
        is submitted) so a recovery on this turn keeps it."""
        agent = self._session_agent(session) or "build"
        created, cerr = self._http(entry.key, "POST", "/session", {"title": session.session_id, "agent": agent})
        oc_id = created.get("id", "") if isinstance(created, dict) else ""
        if cerr or not oc_id:
            return ExecutionResult(False, "", errors=[cerr or "Server returned session without ID"],
                                   error_class="server_unavailable", execution_time=time.time() - start)
        try:
            _native_store("INSERT OR REPLACE INTO native_sessions VALUES (?, ?, ?)",
                          (entry.session_id, entry.key, oc_id))
        except (OSError, sqlite3.Error):
            logger.warning("event=opencode_native_store_write_failed session=%s — memory only", entry.session_id)
        session.backend_session_id = oc_id
        entry.oc_session_id = oc_id
        with self._lock:
            self._native[entry.session_id] = (entry.key, oc_id)
        return None

    def _managed_acquire(self, session: Session) -> threading.Lock:
        """Non-blocking repo lock + capacity; busy ⇒ typed pre-submit conflict."""
        from src.control.turn_queue import OwnershipConflictError

        lock = _get_repo_lock(session.repo_path)
        if not self._repo_capacity.acquire(blocking=False):
            raise OwnershipConflictError("OpenCode server capacity is full")
        if not lock.acquire(blocking=False):
            self._repo_capacity.release()
            raise OwnershipConflictError("another OpenCode turn is running against this repo")
        return lock

    def _managed_release(self, lock: threading.Lock) -> None:
        lock.release()
        self._repo_capacity.release()

    def _managed_presubmit(self, entry: _ManagedTurn, on_process: Any, require_idle: bool = True) -> None:
        """Idle gate + process identity. Raises a typed conflict unless the
        native session is provably idle (unknown ⇒ refuse; never abort)."""
        from src.control.turn_queue import OwnershipConflictError

        status = self._native_status(entry.key, entry.oc_session_id) if require_idle else "idle"
        if status != "idle":
            raise OwnershipConflictError(
                f"OpenCode session is {status or 'in an unknown state'}; managed turn not submitted")
        proc = self._procs.get(entry.key)
        if proc is not None and proc.poll() is None:
            ident = process_identity(proc.pid)
            entry.server_identity = dict(ident)
            if callable(on_process):
                on_process(dict(ident))

    def _managed_turn_body(self, entry: _ManagedTurn, session: Session, message: str, start: float,
                           telemetry_context: Any, telemetry_sink: Any, on_process: Any) -> ExecutionResult:
        from src.control.turn_queue import OwnershipConflictError, RecoveryRequiredError

        early = self._managed_native_session(entry, session, start)
        if early is not None:
            return early
        self._managed_reserve(entry)
        lock = self._managed_acquire(session)
        try:
            if not entry.oc_session_id:
                early = self._managed_create_native(entry, session, start)
                if early is not None:
                    return early
            key, oc_id, mid = entry.key, entry.oc_session_id, entry.message_id
            existing = self._message_exists(key, oc_id, mid)
            if existing is None:
                raise OwnershipConflictError("could not verify the managed message id before submit")
            # Re-bind (our id already recorded ⇒ an earlier invocation of THIS
            # attempt submitted it): no idle gate, never resubmitted.
            self._managed_presubmit(entry, on_process, require_idle=not existing)
            if not existing:
                if entry.cancel_armed:
                    return ExecutionResult(False, "", backend_session_id=oc_id,
                                           errors=["Managed turn cancelled before submission."],
                                           error_class="cancelled", execution_time=time.time() - start)
            stop_reader = threading.Event()
            reader_ready = threading.Event()
            activity = threading.Thread(
                target=self._read_activity_events,
                args=(key, oc_id, telemetry_context, telemetry_sink, stop_reader, reader_ready,
                      {"at": time.monotonic()}),
                name=f"opencode-events-{oc_id[:12]}", daemon=True)
            activity.start()
            reader_ready.wait(timeout=2)
            try:
                if not reader_ready.is_set():
                    if existing:
                        raise RecoveryRequiredError("OpenCode event stream unavailable while re-binding the attempt")
                    return ExecutionResult(False, "", backend_session_id=oc_id,
                        errors=["OpenCode event stream did not connect; turn was not submitted."],
                        error_class="event_stream_unavailable", execution_time=time.time() - start)
                with self._lock:
                    entry.phase = "submitted"
                    entry.submitted_at = time.monotonic()
                if not existing:
                    refused = self._managed_submit(entry, session, message)
                    if refused:
                        return ExecutionResult(False, "", backend_session_id=oc_id, errors=[refused],
                                               error_class="provider_error", execution_time=time.time() - start)
                response = self._managed_wait(entry, start + self._managed_deadline_sec() - time.time())
            finally:
                stop_reader.set()
                activity.join(timeout=2)
            elapsed = time.time() - start
            if entry.cancel_delivered:
                return ExecutionResult(False, "", backend_session_id=oc_id,
                                       errors=["OpenCode managed turn cancelled."], error_class="cancelled",
                                       execution_time=elapsed)
            result = self._managed_outcome(response, session, oc_id, elapsed, telemetry_context, telemetry_sink)
            _clear_native(entry.session_id, oc_id)  # [m7] the terminal result carries the id now
            return result
        finally:
            self._managed_release(lock)

    def _managed_outcome(self, response: Dict[str, Any], session: Session, oc_id: str, elapsed: float,
                         telemetry_context: Any, telemetry_sink: Any) -> ExecutionResult:
        """The managed result for OUR correlated reply (``{}`` ⇒ ended without one)."""
        result = self._turn_result(response, session.repo_path, oc_id, elapsed,
                                   telemetry_context, telemetry_sink)
        native_error = ((response.get("info") or {}).get("error") or {}) if response else {}
        if native_error:
            detail = (native_error.get("data") or {}).get("message") or native_error.get("name") or "error"
            result.success = False
            result.errors = [f"OpenCode turn ended with an error: {detail}"] + list(result.errors or [])
            result.error_class = result.error_class or "provider_error"
        elif not response:
            result.errors = ["OpenCode finished this turn without a reply."] + list(result.errors or [])
        return result

    def _managed_submit(self, entry: _ManagedTurn, session: Session, message: str) -> Optional[str]:
        """POST prompt_async under our deterministic id. A lost/failed ack is
        reconciled by that id; never resubmitted, never aborted. Returns a
        refusal message iff OpenCode definitively rejected the request (4xx and
        our id is provably not recorded) — nothing ran, so it is a plain
        failure, not recovery."""
        from src.control.turn_queue import RecoveryRequiredError

        body: Dict[str, Any] = {"parts": [{"type": "text", "text": message}], "messageID": entry.message_id}
        model_id, provider_id = self._parse_model(self._session_model(session))
        if model_id:
            body["model"] = {"providerID": provider_id or "opencode", "modelID": model_id}
        _, err = self._http(entry.key, "POST", f"/session/{entry.oc_session_id}/prompt_async", body,
                            timeout=self._MANAGED_ACK_TIMEOUT_SEC)
        if not err:
            return None
        code = _http_status(err)
        if code is not None and 400 <= code < 500 and code not in (408, 429):
            # Definitive refusal of the request itself (nothing recorded).
            if self._message_exists(entry.key, entry.oc_session_id, entry.message_id) is False:
                return f"OpenCode refused the managed prompt: {err}"
        for attempt in range(max(1, self._MANAGED_RECONCILE_TRIES)):
            if self._message_exists(entry.key, entry.oc_session_id, entry.message_id):
                return None
            if attempt + 1 < self._MANAGED_RECONCILE_TRIES:
                time.sleep(self._MANAGED_POLL_SEC)
        raise RecoveryRequiredError(f"OpenCode prompt acceptance is ambiguous ({err}); "
                                    "message id not found in native history")

    def _managed_wait(self, entry: _ManagedTurn, budget_sec: float) -> Dict[str, Any]:
        """Wait for OUR terminal reply. Returns the correlated assistant message
        (``{}`` ⇒ our turn ended without one). Deadline / lost server ⇒ typed
        recovery; the native turn is never interrupted for it."""
        from src.control.turn_queue import RecoveryRequiredError

        key, oc_id, mid = entry.key, entry.oc_session_id, entry.message_id
        deadline = time.monotonic() + max(0.0, budget_sec)
        idle_polls = 0
        while True:
            if entry.forgotten:
                return {}  # [m4] terminal server-side: nothing to deliver
            if entry.cancel_armed and not entry.cancel_delivered:
                self._abort_if_ours(entry)
            if time.monotonic() >= deadline:
                raise RecoveryRequiredError("OpenCode managed turn deadline expired without a terminal reply")
            status = self._native_status(key, oc_id)
            if status is None:
                if not self._base_urls.get(key):
                    raise RecoveryRequiredError("OpenCode server was lost mid-turn; outcome unknown")
            elif status == "busy":
                idle_polls = 0
            else:
                history, herr = self._http(key, "GET", f"/session/{oc_id}/message?limit=100", timeout=10)
                if not herr:
                    response = self._find_correlated_response(history, mid)
                    if response:
                        return response
                    exists = self._message_exists(key, oc_id, mid)
                    if exists:
                        idle_polls += 1
                        if idle_polls >= self._MANAGED_IDLE_GRACE_POLLS:
                            # Idle long enough: the latest correlated step is all there is.
                            return self._find_correlated_response(history, mid, terminal_only=False)
                    elif exists is False and time.monotonic() - entry.submitted_at > self._MANAGED_ABSENT_GRACE_SEC:
                        raise RecoveryRequiredError("OpenCode never recorded the managed prompt")
            time.sleep(self._MANAGED_POLL_SEC)

    def _managed_compaction_body(self, entry: _ManagedTurn, session: Session, start: float,
                                 on_process: Any) -> ExecutionResult:
        from src.control.turn_queue import RecoveryRequiredError

        early = self._managed_native_session(entry, session, start)
        if early is not None:
            return early
        key, oc_id = entry.key, entry.oc_session_id
        model_id, provider_id = self._parse_model(self._session_model(session))
        if not provider_id or not model_id:
            providers, perr = self._http(key, "GET", "/config/providers", timeout=10)
            defaults = providers.get("default") if isinstance(providers, dict) else None
            if not perr and isinstance(defaults, dict) and defaults:
                provider_id, model_id = next(iter(defaults.items()))
        if not provider_id or not model_id:
            return ExecutionResult(False, "", backend_session_id=oc_id,
                errors=["OpenCode has no selected or configured default model for native compaction."],
                error_class="model_unavailable", execution_time=time.time() - start)
        self._managed_reserve(entry)
        lock = self._managed_acquire(session)
        try:
            history, herr = self._http(key, "GET", f"/session/{oc_id}/message?limit=100", timeout=10)
            if herr or not isinstance(history, list):
                from src.control.turn_queue import OwnershipConflictError
                raise OwnershipConflictError("could not snapshot native history before compaction")
            entry.known_ids = [str((m.get("info") or {}).get("id")) for m in history if isinstance(m, dict)]
            self._managed_presubmit(entry, on_process)
            if entry.cancel_armed:
                return ExecutionResult(False, "", backend_session_id=oc_id,
                                       errors=["Managed compaction cancelled before submission."],
                                       error_class="cancelled", execution_time=time.time() - start)
            with self._lock:
                entry.phase = "submitted"
                entry.submitted_at = time.monotonic()
            summarized, err = self._http(key, "POST", f"/session/{oc_id}/summarize",
                                         {"providerID": provider_id, "modelID": model_id},
                                         timeout=int(self._managed_deadline_sec()))
            if entry.cancel_delivered:
                return ExecutionResult(False, "", backend_session_id=oc_id,
                                       errors=["OpenCode managed compaction cancelled."],
                                       error_class="cancelled", execution_time=time.time() - start)
            if err or summarized is False:
                # Synchronous call with server-chosen ids: a failed/lost
                # response is not attributable (it may have compacted).
                raise RecoveryRequiredError(f"OpenCode compaction outcome is ambiguous: "
                                            f"{err or 'summarize returned false'}")
            return ExecutionResult(True, "", backend_session_id=oc_id, execution_time=time.time() - start)
        finally:
            self._managed_release(lock)

    # -- native probes --------------------------------------------------- #
    def _native_status(self, key: str, oc_id: str) -> Optional[str]:
        """``idle`` / ``busy`` from ``GET /session/status`` (an absent entry is
        idle on opencode 1.18.x); ``None`` when unknown/unreachable."""
        if not key or not oc_id or not self._base_urls.get(key):
            return None
        states, err = self._http(key, "GET", "/session/status", timeout=5)
        if err or not isinstance(states, dict):
            return None
        status = states.get(oc_id)
        if status is None:
            return "idle"
        state = status.get("type") if isinstance(status, dict) else status
        return "idle" if state == "idle" else "busy"

    def _message_exists(self, key: str, oc_id: str, message_id: str) -> Optional[bool]:
        info, err = self._http(key, "GET", f"/session/{oc_id}/message/{message_id}", timeout=10)
        if not err:
            got = (info.get("info") or {}).get("id") if isinstance(info, dict) else None
            return got == message_id
        return False if _http_status(err) == 404 else None

    def _latest_user(self, history: Any) -> Optional[Dict[str, Any]]:
        """OpenCode's own "latest user message" order: time.created, then id."""
        users = [m for m in history if isinstance(m, dict) and isinstance(m.get("info"), dict)
                 and m["info"].get("role") == "user"] if isinstance(history, list) else []
        if not users:
            return None
        return max(users, key=lambda m: ((m["info"].get("time") or {}).get("created") or 0,
                                         str(m["info"].get("id") or "")))

    def _running_is_ours(self, entry: _ManagedTurn) -> Optional[bool]:
        """True iff the session is busy AND its latest user message is ours;
        False when busy with someone else's message; None when not running/unknown."""
        if self._native_status(entry.key, entry.oc_session_id) != "busy":
            return None
        history, err = self._http(entry.key, "GET", f"/session/{entry.oc_session_id}/message?limit=20", timeout=10)
        latest = None if err else self._latest_user(history)
        if latest is None:
            return None
        latest_id = str(latest["info"].get("id") or "")
        if entry.message_id:
            return latest_id == entry.message_id
        is_compaction = any(isinstance(p, dict) and p.get("type") == "compaction" for p in latest.get("parts") or [])
        return is_compaction and latest_id not in entry.known_ids

    def _abort_if_ours(self, entry: _ManagedTurn) -> Optional[bool]:
        """Deliver the session-wide abort ONLY while our message is the running
        one. Returns True (delivered), False (another message runs), None (ours
        is not running yet / unknown — stays armed)."""
        ours = self._running_is_ours(entry)
        if not ours:
            return ours
        _, err = self._http(entry.key, "POST", f"/session/{entry.oc_session_id}/abort", timeout=5)
        if err:
            return None
        with self._lock:
            entry.cancel_delivered = True
        return True

    def _entry_in_flight(self, entry: _ManagedTurn) -> Optional[bool]:
        """Is a HELD attempt's native work still running? A gone server process
        proves it stopped (OpenCode runs turns in-process); otherwise status +
        history decide; unknown ⇒ None."""
        if entry.server_identity and process_gone_proof(entry.server_identity):
            return False
        status = self._native_status(entry.key, entry.oc_session_id)
        if status is None:
            return None
        if status == "busy":
            return True
        if entry.kind == "compaction" or not entry.message_id:
            return False
        exists = self._message_exists(entry.key, entry.oc_session_id, entry.message_id)
        if exists is None:
            return None
        if exists:
            return False
        return time.monotonic() - entry.submitted_at < self._MANAGED_ABSENT_GRACE_SEC

    def is_quiescent(self, session: Session) -> bool:
        """No native work for ``session`` is in flight: no running local attempt,
        every held attempt natively resolved, and ``/session/status`` idle.
        Unknown / unreachable ⇒ False. No native session at all ⇒ True."""
        sid = session.session_id or ""
        with self._lock:
            entries = [e for e in self._managed.values() if e.session_id == sid]
            known = self._native.get(sid)
        for e in entries:
            if e.phase != "held" or e.late_pending or self._entry_in_flight(e) is not False:
                return False
        oc_id = session.backend_session_id or (known[1] if known else "")
        if not oc_id:
            return True
        key = self._server_key(session.repo_path) if session.repo_path else (known[0] if known else "")
        if not key:
            return False
        if not self._base_urls.get(key):
            try:
                if not session.repo_path or self._ensure_server(key, session.repo_path):
                    return False
            except Exception:
                return False  # e.g. live-call guard / spawn failure ⇒ unknown
        return self._native_status(key, oc_id) == "idle"

    def cancel_managed_turn(self, session: Session, turn_uuid: str) -> bool:
        """Cancel exactly ``turn_uuid``: arm it when it has not begun here; abort
        only while its message is the running one; refuse (False, disarmed)
        when another message is running — never abort someone else's turn."""
        if not turn_uuid:
            return False
        with self._lock:
            entry = self._managed.get(turn_uuid)
            if entry is None:
                self._armed_cancels.add(turn_uuid)
                return True
            entry.cancel_armed = True
            phase = entry.phase
        if phase == "reserved":
            return True        # the pre-submit check refuses to submit it
        delivered = self._abort_if_ours(entry)
        if delivered is False:
            with self._lock:
                entry.cancel_armed = False
            return False
        if phase == "held":
            return bool(delivered)  # no live waiter to deliver it later
        return True            # delivered, or armed for when ours is running

    def forget_managed_turn(self, session: Session, turn_uuid: str) -> bool:
        """Drop a HELD attempt (or an armed cancel) the carrier learned is
        terminal server-side. An attempt whose call is still running here is
        not dropped — its own waiter ends it (reply / deadline)."""
        with self._lock:
            entry = self._managed.get(turn_uuid)
            dropped = entry is not None and entry.phase == "held"
            if dropped:
                entry.forgotten = True
                self._managed.pop(turn_uuid, None)
            armed = turn_uuid in self._armed_cancels
            self._armed_cancels.discard(turn_uuid)
        return dropped or armed

    # ------------------------------------------------------------------
    # Core message send
    # ------------------------------------------------------------------

    def _send_message(
        self,
        key: str,
        oc_session_id: str,
        message: str,
        cwd: str,
        start: float,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        telemetry_context=None,
        telemetry_sink=None,
    ) -> ExecutionResult:
        lock = _get_repo_lock(cwd)
        if not self._repo_capacity.acquire(blocking=False):
            return ExecutionResult(False, "", backend_session_id=oc_session_id,
                                   errors=["OpenCode server capacity is full."], error_class="capacity_exceeded")
        if not lock.acquire(blocking=False):
            self._repo_capacity.release()
            return ExecutionResult(False, "", backend_session_id=oc_session_id,
                                   errors=[f"Another OpenCode task is already running against repo: {cwd}."],
                                   error_class="repo_busy")
        try:
            return self._send_message_locked(key, oc_session_id, message, cwd, start, model_id, provider_id,
                                             telemetry_context, telemetry_sink)
        finally:
            lock.release()
            self._repo_capacity.release()

    def _send_message_locked(
        self, key: str, oc_session_id: str, message: str, cwd: str, start: float,
        model_id: Optional[str], provider_id: Optional[str], telemetry_context: Any,
        telemetry_sink: Any,
    ) -> ExecutionResult:
        try:
            from config import config as _cfg
            # The existing OpenCode setting is the hard request ceiling.
            timeout = int(getattr(_cfg.opencode, "timeout_seconds", 1800))
        except Exception:
            timeout = 1800

        body: Dict[str, Any] = {"parts": [{"type": "text", "text": message}]}
        message_id = f"msg_{os.urandom(12).hex()}"
        body["messageID"] = message_id
        # Set the model inline in the message body — the only reliable way on
        # opencode 1.16.2 (PATCH /session is a no-op that corrupts model state).
        # Only sent when we have a concrete model id; otherwise opencode resolves
        # it from the agent/global config (which already defaults correctly).
        if model_id:
            body["model"] = {"providerID": provider_id or "opencode", "modelID": model_id}
        stop_reader = threading.Event()
        reader_ready = threading.Event()
        cancel_event = threading.Event()
        last_progress = {"at": time.monotonic()}
        with self._lock:
            self._active_cancel[oc_session_id] = cancel_event
        activity_thread = threading.Thread(
            target=self._read_activity_events,
            args=(key, oc_session_id, telemetry_context, telemetry_sink, stop_reader, reader_ready, last_progress),
            name=f"opencode-events-{oc_session_id[:12]}", daemon=True,
        )
        activity_thread.start()
        reader_ready.wait(timeout=2)
        if not reader_ready.is_set():
            stop_reader.set()
            activity_thread.join(timeout=2)
            with self._lock:
                self._active_cancel.pop(oc_session_id, None)
            return ExecutionResult(False, "", backend_session_id=oc_session_id,
                errors=["OpenCode event stream did not connect; turn was not submitted."],
                error_class="event_stream_unavailable", execution_time=time.time() - start)
        response: Dict[str, Any] = {}
        err: Optional[str] = None
        turn_deadline = time.monotonic() + max(1, timeout)
        try:
            _, err = self._http(key, "POST", f"/session/{oc_session_id}/prompt_async", body,
                                timeout=min(30, max(1, timeout)))
            if err:
                # The request may have reached OpenCode even if its acceptance
                # response was lost. Reconcile by the exact client message ID
                # before deciding whether to abort; never blindly resubmit.
                accepted_history, reconcile_err = self._http(
                    key, "GET", f"/session/{oc_session_id}/message?limit=100", timeout=10)
                accepted = self._message_was_accepted(accepted_history, message_id)
                if accepted:
                    err = None
                else:
                    self._http(key, "POST", f"/session/{oc_session_id}/abort", timeout=5)
                    if reconcile_err:
                        err = f"Prompt acceptance was ambiguous and history reconciliation failed: {reconcile_err}"
                    else:
                        err = "Prompt acceptance was ambiguous; request was not safely confirmed."
            deadline = turn_deadline
            try:
                from config import config as _cfg
                configured_stall = max(60, int(getattr(_cfg.system, "inactivity_timeout_sec", 36000)))
                stall_timeout = min(configured_stall, max(30, timeout // 2))
            except Exception:
                stall_timeout = 36000
            next_poll = 0.0
            terminal_error = ""
            while err is None:
                now = time.monotonic()
                if cancel_event.is_set():
                    err = "OpenCode turn cancelled."
                    break
                if last_progress.get("error"):
                    err = last_progress["error"]
                    self._http(key, "POST", f"/session/{oc_session_id}/abort", timeout=5)
                    break
                if now >= deadline:
                    err = f"OpenCode hard timeout after {timeout}s."
                    self._http(key, "POST", f"/session/{oc_session_id}/abort", timeout=5)
                    break
                if now - last_progress["at"] >= stall_timeout:
                    err = f"OpenCode inactivity timeout after {stall_timeout}s."
                    self._http(key, "POST", f"/session/{oc_session_id}/abort", timeout=5)
                    break
                if now < next_poll:
                    cancel_event.wait(min(0.25, next_poll - now))
                    continue
                next_poll = now + 1.0
                states, poll_err = self._http(key, "GET", "/session/status", timeout=5)
                if poll_err:
                    err = poll_err
                    break
                # OpenCode 1.18.x omits idle sessions from this map; the live
                # `/session/status` response is `{}` once a turn has finished.
                # Treat an absent key as idle and reconcile history below. A
                # present status entry remains authoritative (including busy).
                status_present = isinstance(states, dict) and oc_session_id in states
                status = states.get(oc_session_id, {}) if isinstance(states, dict) else {}
                state = status.get("type") if isinstance(status, dict) else status
                if not status_present:
                    state = "idle"
                if state == "error":
                    terminal_error = str(status.get("error") or "OpenCode session reported an error")
                    break
                if state in ("idle", "error"):
                    history, history_err = self._http(key, "GET", f"/session/{oc_session_id}/message?limit=100", timeout=10)
                    if history_err:
                        err = history_err
                        break
                    response = self._find_correlated_response(history, message_id)
                    if response:
                        break
                    if state == "error":
                        err = terminal_error
                        break
                    # Status can briefly report idle while an accepted async
                    # prompt is entering the session queue. Keep reconciling;
                    # only a correlated assistant message is terminal success.
            if terminal_error and not err:
                err = terminal_error
        finally:
            stop_reader.set()
            activity_thread.join(timeout=2)
            with self._lock:
                self._active_cancel.pop(oc_session_id, None)

        elapsed = time.time() - start

        if err:
            return ExecutionResult(False, "", backend_session_id=oc_session_id, errors=[err],
                execution_time=elapsed,
                error_class="cancelled" if "cancelled" in err.lower() else "timeout" if "timeout" in err.lower() else "transport_error" if "unreachable" in err.lower() else "malformed_event" if "event stream malformed" in err.lower() else "provider_error")

        return self._turn_result(response, cwd, oc_session_id, elapsed, telemetry_context, telemetry_sink)

    def _turn_result(
        self, response: Dict[str, Any], cwd: str, oc_session_id: str, elapsed: float,
        telemetry_context: Any, telemetry_sink: Any,
    ) -> ExecutionResult:
        """Build the ExecutionResult for a terminal correlated OpenCode reply
        (shared by the legacy and the managed send paths)."""
        output, errors, finish = self._parse_message_response(response)

        if finish in ("stop", "tool-calls"):
            success = not errors
        elif finish == "unknown":
            # Truncated but partial output was returned — treat as success so the
            # user sees the partial answer (truncation note already appended in parser).
            success = not errors
        else:
            # finish="" → no step-finish part at all, malformed/empty response.
            errors.append(f"Generation ended with unexpected finish reason: {finish!r}")
            success = False

        # Collect git diff
        files_modified: List[str] = []
        git_diff_stat = ""
        git_diff = ""
        if cwd:
            files_modified = _git_changed_files(cwd)
            git_diff_stat = _run_git(cwd, ["diff", "--stat", "HEAD"]) or ""
            git_diff = _run_git(cwd, ["diff", "HEAD"]) or ""

        # Suspect-run / dead-end detection (mirrors the CLI backend): a clean
        # finish that only announced intent after a permission block, with NO
        # files changed, is a dead-end rather than a success. The files-modified
        # check makes this safe — a run that did real work is never flagged.
        result_error_class = ""
        if success and finish != "stop" and not files_modified and not git_diff.strip():
            try:
                blocked = _detect_permission_block(json.dumps(response), "")
            except Exception:
                blocked = ""
            if blocked and _looks_intent_only(output):
                success = False
                result_error_class = "permission_block"
                errors.append(
                    "OpenCode stopped early on an auto-rejected permission "
                    f"({blocked}) with only intent-only text and no file changes. "
                    "This is a dead-end, not a success. Widen the opencode "
                    "permission/allowed paths or keep actions inside the repo."
                )

        parsed_output: Dict[str, Any] = {
            "git_diff_stat": git_diff_stat,
            "git_diff": git_diff,
            "tokens": response.get("info", {}).get("tokens"),
            "cost": response.get("info", {}).get("cost"),
            "finish": finish,
        }
        self._emit_usage(telemetry_context, telemetry_sink, response.get("info", {}))

        # Auto-commit so the working tree is clean for the next run.
        if success and cwd and files_modified:
            OpenCodeBackend._auto_commit(cwd, oc_session_id)
            files_modified = []  # consumed by the commit

        return ExecutionResult(
            success=success,
            output=output,
            backend_session_id=oc_session_id,
            errors=errors,
            execution_time=elapsed,
            files_modified=files_modified,
            parsed_output=parsed_output,
            error_class=result_error_class,
        )

    def _read_activity_events(
        self, key: str, oc_session_id: str, telemetry_context: Any, telemetry_sink: Any,
        stop: threading.Event, ready: threading.Event, last_progress: Dict[str, Any],
    ) -> None:
        """Read bounded OpenCode SSE frames and forward only safe structural labels."""
        base_url = self._base_urls.get(key, "")
        if not base_url:
            return
        last_activity: tuple[str, str] | None = None
        try:
            while not stop.is_set():
                request = urllib.request.Request(base_url + "/event", headers={"Accept": "text/event-stream"})
                try:
                    with urllib.request.urlopen(request, timeout=1) as response:
                        ready.set()
                        while not stop.is_set():
                            line = response.readline(262145)
                            if len(line) > 262144:
                                last_progress["error"] = "OpenCode event stream malformed: frame exceeds 256 KiB."
                                return
                            if not line:
                                break
                            if not line.startswith(b"data:"):
                                continue
                            raw = line[5:].strip()
                            if len(raw) > 262144:
                                last_progress["error"] = "OpenCode event stream malformed: payload exceeds 256 KiB."
                                return
                            try:
                                envelope = json.loads(raw)
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                last_progress["error"] = "OpenCode event stream malformed: invalid JSON."
                                return
                            event = envelope.get("payload", envelope) if isinstance(envelope, dict) else {}
                            if not isinstance(event, dict):
                                last_progress["error"] = "OpenCode event stream malformed: payload is not an object."
                                return
                            props = event.get("properties") or {}
                            if not isinstance(props, dict):
                                last_progress["error"] = "OpenCode event stream malformed: event properties are not an object."
                                return
                            raw_part = props.get("part")
                            native_id = props.get("sessionID") or props.get("sessionId")
                            if not native_id and isinstance(raw_part, dict):
                                native_id = raw_part.get("sessionID")
                            if native_id != oc_session_id:
                                continue
                            if raw_part is not None and not isinstance(raw_part, dict):
                                last_progress["error"] = "OpenCode event stream malformed: message part is not an object."
                                return
                            part = raw_part or {}
                            name = event.get("type", "")
                            if not isinstance(name, str) or not name:
                                last_progress["error"] = "OpenCode event stream malformed: missing event type."
                                return
                            activity_category = ""
                            activity_tool = ""
                            if name == "session.status":
                                status = props.get("status") or {}
                                if not isinstance(status, dict):
                                    last_progress["error"] = "OpenCode event stream malformed: session status is not an object."
                                    return
                                activity_category = "backend_busy" if status.get("type") == "busy" else ""
                            elif name == "message.part.updated":
                                if part.get("type") == "tool":
                                    tool = part.get("tool", "tool")
                                    safe_tool = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(tool))[:60] or "tool"
                                    state = part.get("state") or {}
                                    if not isinstance(state, dict):
                                        last_progress["error"] = "OpenCode event stream malformed: tool state is not an object."
                                        return
                                    tool_status = state.get("status")
                                    activity_tool = {
                                        "bash": "Bash", "read": "Read", "edit": "Edit",
                                        "write": "Write", "glob": "Glob", "grep": "Grep",
                                        "task": "Task", "websearch": "WebSearch",
                                        "webfetch": "WebFetch", "notebookedit": "NotebookEdit",
                                    }.get(str(tool).casefold(), "")
                                    activity_category = (
                                        "tool_started" if tool_status in ("running", "pending")
                                        else "tool_completed" if tool_status in ("completed", "error")
                                        else ""
                                    )
                                    self._emit_tool_telemetry(telemetry_context, telemetry_sink, safe_tool,
                                        str(part.get("callID") or part.get("id") or ""), tool_status, last_progress)
                                elif part.get("type") == "text":
                                    activity_category = "writing"
                            elif name == "session.idle":
                                activity_category = "finished"
                            elif name == "permission.asked":
                                activity_category = "waiting_permission"
                            elif name == "session.error":
                                last_progress["error"] = "OpenCode reported a session error."
                            if name == "message.part.updated" and part.get("type") in ("text", "reasoning"):
                                last_progress["at"] = time.monotonic()
                            elif name == "message.part.updated" and part.get("type") == "tool" and (part.get("state") or {}).get("status") in ("completed", "error"):
                                last_progress["at"] = time.monotonic()
                            activity_key = (activity_category, activity_tool)
                            if activity_category and activity_key != last_activity:
                                from src.core.activity import publish_activity
                                publish_activity(
                                    session_id=getattr(telemetry_context, "session_id", None),
                                    task_id=getattr(telemetry_context, "turn_id", None),
                                    category=activity_category,
                                    tool=activity_tool or None,
                                )
                                last_activity = activity_key
                    if not stop.wait(0.1):
                        continue
                except (TimeoutError, socket.timeout, OSError, urllib.error.URLError):
                    if stop.wait(0.1):
                        break
        except Exception:
            # Status and history reconciliation remain the source of truth for
            # completion; activity transport cannot produce a successful result.
            pass
    def _find_correlated_response(self, history: Any, message_id: str, *,
                                  terminal_only: bool = True) -> Dict[str, Any]:
        """Select only the assistant result explicitly parented to this request.

        ``terminal_only``: skip a reply still being generated (no
        ``time.completed``) or a finished intermediate ``tool-calls`` step —
        ``/session/status`` can read idle while either is the latest message."""
        if not isinstance(history, list):
            return {}
        for item in reversed(history[-100:]):
            if not isinstance(item, dict):
                continue
            info = item.get("info") or {}
            if (info.get("role") == "assistant" and
                    (info.get("parentID") or info.get("parentId")) == message_id):
                if terminal_only and not info.get("error"):
                    steps = [p.get("reason") for p in item.get("parts") or []
                             if isinstance(p, dict) and p.get("type") == "step-finish"]
                    done = (info.get("time") or {}).get("completed") or steps
                    finish = info.get("finish") or (steps[-1] if steps else "")
                    if not done or finish == "tool-calls":
                        return {}
                return item
        return {}

    def _message_was_accepted(self, history: Any, message_id: str) -> bool:
        return isinstance(history, list) and any(
            isinstance(item, dict) and isinstance(item.get("info"), dict)
            and item["info"].get("id") == message_id
            for item in history[-100:]
        )

    def _emit_tool_telemetry(
        self, context: Any, sink: Any, tool_name: str, tool_call_id: str, state: Any,
        progress: Dict[str, Any],
    ) -> None:
        if context is None or sink is None or not tool_call_id or state not in ("running", "completed", "error"):
            return
        from src.core.telemetry import EMITTER_PROCESS_INSTANCE_ID, build_event
        statuses = progress.setdefault("tool_status", {})
        now = time.monotonic()
        key = tool_call_id
        sequence_by_id = progress.setdefault("tool_sequence_by_id", {})
        completed = progress.setdefault("completed_tools", set())
        if state == "running":
            if key in statuses or key in completed:
                return
            if int(progress.get("tool_sequence", 0)) >= 512:
                return
            sequence = int(progress.get("tool_sequence", 0)) + 1
            progress["tool_sequence"] = sequence
            statuses[key] = now
            sequence_by_id[key] = sequence
            event_name = "tool.call.started"
            attrs = {"tool_name": tool_name, "tool_category": "other", "sequence": sequence}
        else:
            if key in completed:
                return
            if int(progress.get("tool_terminal_count", 0)) >= 512:
                return
            sequence = sequence_by_id.pop(key, int(progress.get("tool_sequence", 0)) + 1)
            started = statuses.pop(key, now)
            completed.add(key)
            progress["tool_terminal_count"] = int(progress.get("tool_terminal_count", 0)) + 1
            event_name = "tool.call.failed" if state == "error" else "tool.call.completed"
            attrs = {"tool_name": tool_name, "tool_category": "other", "sequence": sequence,
                     "duration_ms": max(0, int((now - started) * 1000))}
            if event_name == "tool.call.failed":
                attrs["error_code"] = "tool_error"
            else:
                attrs["status"] = "success"
        try:
            event = build_event(event_name, turn_id=context.turn_id, node_id=context.node_id,
                emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID, source="backend",
                invocation_id=context.invocation_id, tool_call_id=tool_call_id or None,
                backend="opencode-server", model=context.model, attributes=attrs)
            sink.emit(event)
        except Exception:
            logger.warning("event=opencode_telemetry_emit_failed")

    def _emit_usage(self, context: Any, sink: Any, info: Any) -> None:
        if context is None or sink is None:
            return
        from src.core.telemetry import EMITTER_PROCESS_INSTANCE_ID, build_event
        tokens = info.get("tokens") if isinstance(info, dict) else None
        if not isinstance(tokens, dict) or not any(
            isinstance(tokens.get(key), int) and tokens.get(key) >= 0 for key in ("input", "output")
        ):
            try:
                sink.emit(build_event("telemetry.coverage", turn_id=context.turn_id,
                    node_id=context.node_id, emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                    source="backend", invocation_id=context.invocation_id, backend="opencode-server",
                    model=context.model, attributes={"area": "usage", "coverage": "unavailable",
                    "reason_code": "opencode_message_tokens_missing", "adapter_version": "opencode-server-v1"}))
            except Exception:
                logger.warning("event=opencode_telemetry_emit_failed")
            return
        attrs = {
            "sequence": 1,
            "input_tokens": tokens.get("input"),
            "output_tokens": tokens.get("output"),
            "cache_read_tokens": tokens.get("cache", {}).get("read") if isinstance(tokens.get("cache"), dict) else None,
            "cache_creation_tokens": tokens.get("cache", {}).get("write") if isinstance(tokens.get("cache"), dict) else None,
            "input_token_semantics": "unknown",
            "usage_granularity": "request",
            "usage_source": "opencode.message.info.tokens",
            "usage_coverage": "provider_reported",
        }
        attrs = {key: value for key, value in attrs.items() if value is not None}
        try:
            sink.emit(build_event("model.request.usage", turn_id=context.turn_id,
                node_id=context.node_id, emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                source="backend", invocation_id=context.invocation_id,
                backend="opencode-server", model=context.model, attributes=attrs))
        except Exception:
            logger.warning("event=opencode_telemetry_emit_failed")

    @staticmethod
    def _parse_message_response(response: Dict[str, Any]) -> tuple:
        """Return (output_text, errors, finish_reason) from a message POST response."""
        parts = response.get("parts") or []
        text_chunks: List[str] = []
        errors: List[str] = []
        finish = ""

        for part in parts:
            ptype = part.get("type", "")
            if ptype == "text":
                chunk = part.get("text") or ""
                if chunk:
                    text_chunks.append(chunk)
            elif ptype == "step-finish":
                finish = part.get("reason") or ""
            elif ptype == "error":
                msg = part.get("message") or part.get("text") or ""
                if msg:
                    errors.append(msg)

        output = "".join(text_chunks).strip()

        # Detect truncated generation
        if finish == "unknown" and output:
            logger.warning("event=opencode_server_truncated_output finish=%s output_len=%d", finish, len(output))
            output += "\n\n_(Note: response was cut off — OpenCode reported an interrupted generation.)_"

        return output, errors, finish

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def _ensure_server(self, key: str, repo_path: str) -> Optional[str]:
        """Start the per-directory server if not running and verify it responds.

        `opencode serve` has no per-request directory override, so each distinct
        repo directory gets its own server process launched with `cwd=repo_path`.
        Returns error string or None.
        """
        # Cost guard: blocked under test mode unless OpenCode e2e is opted in.
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("opencode-server")

        # NOTE: deliberately NO auth.json pre-flight here. opencode authenticates
        # via a cached session in opencode.db, so a missing/empty auth.json does
        # NOT mean "logged out" — checking it produces false negatives that block
        # working setups. A genuine auth failure surfaces as an error from the
        # message POST and is handled there.

        with self._lock:
            proc = self._procs.get(key)
            if proc is not None and proc.poll() is None and self._base_urls.get(key):
                return None  # already up

            # Clean up any dead process reference before restarting.
            if proc is not None:
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
                self._procs.pop(key, None)
                self._base_urls.pop(key, None)
                self._release_server_slot_locked(key)

            if not repo_path:
                return "repo_path is required to start an opencode server."

            p = Path(repo_path)
            if not p.exists() or not p.is_dir():
                return f"Repository path does not exist or is not a directory: {repo_path}"

            if not self._server_slots.acquire(blocking=False):
                return "OpenCode server capacity is full (8 repo servers are already resident)."
            self._server_slot_keys.add(key)

            try:
                from config import config as _cfg
                oc_cfg = _cfg.opencode
                host = getattr(oc_cfg, "server_host", "127.0.0.1")
                preferred_port = int(getattr(oc_cfg, "server_port", 4096))
            except Exception:
                host = "127.0.0.1"
                preferred_port = 4096

            port = _find_free_port(preferred_port)
            cmd = [self._exe, "serve", "--hostname", host, "--port", str(port)]

            logger.info("event=opencode_server_start cmd=%s cwd=%s", cmd, repo_path)
            proc_env = ensure_node_on_path()
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=repo_path,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,   # capture for diagnostics
                    env=proc_env,
                    creationflags=_NO_WINDOW,
                )
            except Exception as e:
                self._release_server_slot_locked(key)
                return f"Failed to start opencode server: {e}"

            # Register the proc immediately so it is never orphaned if an
            # exception occurs below (terminate_active_processes will find it).
            self._procs[key] = proc

            base_url = f"http://{host}:{port}"

            # Wait up to 15 seconds for the server to accept connections.
            deadline = time.time() + 15
            while time.time() < deadline:
                if proc.poll() is not None:
                    stderr_tail = ""
                    try:
                        stderr_tail = proc.stderr.read(2000).decode(errors="replace").strip()
                    except Exception:
                        pass
                    self._procs.pop(key, None)
                    self._release_server_slot_locked(key)
                    return (
                        f"opencode server process exited immediately (exit={proc.returncode}). "
                        + (f"stderr: {stderr_tail}" if stderr_tail else "No stderr captured.")
                    )
                try:
                    with urllib.request.urlopen(f"{base_url}/global/health", timeout=1) as resp:
                        health = json.loads(resp.read(4097))
                    if not isinstance(health, dict) or health.get("healthy") is not True:
                        raise RuntimeError("OpenCode health endpoint returned an invalid response")
                    break
                except Exception:
                    time.sleep(0.3)
            else:
                stderr_tail = ""
                try:
                    # Read whatever the process wrote before killing it.
                    proc.stderr.read(2000)  # non-blocking since proc may still be alive
                    stderr_tail = proc.stderr.read(2000).decode(errors="replace").strip()
                except Exception:
                    pass
                self._procs.pop(key, None)
                terminate_many_popen([proc])
                self._release_server_slot_locked(key)
                return (
                    f"opencode server did not start within 15s on {base_url}. "
                    + (f"stderr: {stderr_tail}" if stderr_tail else "No stderr output captured.")
                )

            self._base_urls[key] = base_url
            logger.info("event=opencode_server_ready url=%s pid=%s cwd=%s", base_url, proc.pid, repo_path)
            return None

    def _release_server_slot_locked(self, key: str) -> None:
        if key in self._server_slot_keys:
            self._server_slot_keys.remove(key)
            self._server_slots.release()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _http(
        self,
        key: str,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        timeout: int = 300,
    ) -> tuple:
        """Make an HTTP request against the server for `key`. Returns (parsed_json, error_str_or_None).

        Request and response bodies are capped at 8 MiB. Timeouts do not imply
        server death; connection failures do and release the repo server slot
        so a later call may start a fresh process.
        """
        base_url = self._base_urls.get(key, "")
        url = base_url + path
        data = json.dumps(body).encode() if body is not None else None
        if data is not None and len(data) > 8 * 1024 * 1024:
            return {}, f"OpenCode request exceeded 8 MiB ({method} {path})"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                declared_size = resp.headers.get("Content-Length")
                if declared_size and int(declared_size) > 8 * 1024 * 1024:
                    return {}, f"OpenCode response exceeded 8 MiB ({method} {path})"
                raw = resp.read(8 * 1024 * 1024 + 1)
                self._refused_since.pop(key, None)
                if len(raw) > 8 * 1024 * 1024:
                    return {}, f"OpenCode response exceeded 8 MiB ({method} {path})"
                if not raw:
                    return {}, None
                return json.loads(raw), None
        except urllib.error.HTTPError as e:
            self._refused_since.pop(key, None)  # the server answered
            raw = e.read(8 * 1024 * 1024 + 1)
            try:
                err_body = json.loads(raw)
                msg = err_body.get("data", {}).get("message") or err_body.get("name") or str(e)
            except Exception:
                msg = raw.decode(errors="replace") if raw else str(e)
            return {}, f"HTTP {e.code} from opencode server ({method} {path}): {msg}"
        except (TimeoutError, socket.timeout) as e:
            # A timed-out generation may still be running. Abort it while
            # retaining the server and native session so subsequent turns can
            # resume the same history.
            if method == "POST" and path.endswith("/message"):
                session_id = path.split("/")[-2]
                self._http(key, "POST", f"/session/{session_id}/abort", timeout=5)
            return {}, (
                f"opencode request timed out ({method} {path}) after {timeout}s"
            )
        except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
            # [A82 step 4 rework, m6] A transport error is not proof the SHARED
            # serve is gone — terminating it kills every turn it hosts. A
            # connect-phase timeout (URLError(timeout)) is a timeout; a reset or
            # refusal on a LIVE process is transient (unknown to the caller).
            # Only proven death (the process exited), or a live process that
            # has refused connections for _UNREACHABLE_TERMINATE_SEC (it is not
            # serving: unrecoverable), clears the reference for a restart.
            reason = getattr(e, "reason", e)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return {}, f"opencode request timed out ({method} {path}) after {timeout}s"
            now = time.monotonic()
            with self._lock:
                proc = self._procs.get(key)
                dead = proc is None or proc.poll() is not None
                refusing = isinstance(reason, ConnectionRefusedError)
                if refusing:
                    # [A82 pre-cutover, m6] Only a CONTINUOUS refusal streak
                    # counts: an earlier refusal older than the window (no
                    # refusal since) is isolated — the streak restarts.
                    last = self._refused_last.get(key)
                    if last is not None and now - last >= self._UNREACHABLE_TERMINATE_SEC:
                        self._refused_since.pop(key, None)
                    self._refused_last[key] = now
                first = self._refused_since.setdefault(key, now) if refusing else None
                wedged = first is not None and now - first >= self._UNREACHABLE_TERMINATE_SEC
                if dead or wedged:
                    self._procs.pop(key, None)
                    self._base_urls.pop(key, None)
                    self._refused_since.pop(key, None)
                    self._release_server_slot_locked(key)
            if not (dead or wedged):
                return {}, f"opencode server unreachable ({method} {path}): {e} — server kept (transient)"
            if proc is not None:
                terminate_many_popen([proc])  # reap an exited group / stop a wedged one
            return {}, f"opencode server unreachable ({method} {path}): {e} — will restart on next call"
        except Exception as e:
            return {}, f"Request failed ({method} {path}): {e}"

    # ------------------------------------------------------------------
    # Helpers (reuse from CLI backend)
    # ------------------------------------------------------------------

    @staticmethod
    def _session_model(session: Session) -> Optional[str]:
        return OpenCodeBackend._session_model(session)

    @staticmethod
    def _session_agent(session: Session) -> Optional[str]:
        return OpenCodeBackend._session_agent(session)

    @staticmethod
    def _parse_model(model_str: Optional[str]) -> tuple:
        """Split 'provider/model' into (model_id, provider_id). Falls back to bare model ID.

        Hardened against malformed input: a string like 'big-pickle/' or '/big-pickle'
        previously yielded an empty model or provider half, which opencode rejects with
        an opaque ProviderModelNotFoundError (HTTP 500). We never emit an empty model_id:
        if the model half is blank, we treat the whole non-empty token as a bare model id.
        """
        if not model_str:
            return None, None
        model_str = model_str.strip()
        if not model_str:
            return None, None
        if "/" in model_str:
            provider, _, model = model_str.partition("/")
            provider = provider.strip()
            model = model.strip()
            if provider and model:
                return model, provider
            # Malformed (one side empty) — recover the non-empty half as a bare model id
            # rather than sending an empty model to the server.
            bare = model or provider
            logger.warning(
                "event=opencode_model_malformed input=%r recovered_model=%r",
                model_str, bare,
            )
            return (bare or None), None
        return model_str, None
