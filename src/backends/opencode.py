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
import json
import logging
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.process_utils import ensure_node_on_path, terminate_many_popen
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
            inactivity_sec = max(60, int(getattr(_cfg.system, "inactivity_timeout_sec", 600)))
            oc_cfg = getattr(_cfg, "opencode", None)
            collect_diff = bool(getattr(oc_cfg, "collect_diff", True)) if oc_cfg else True
        except Exception:
            inactivity_sec = 600
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
        result = self._send_message(
            key=key,
            oc_session_id=oc_session_id,
            message=session.last_user_message,
            cwd=session.repo_path,
            start=start,
            model_id=model_id,
            provider_id=provider_id,
        )
        if not result.success and self._message_transport_failed(result):
            session.backend_session_id = ""
            result.backend_session_id = ""
        return result

    def resume_session(self, session: Session, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        start = time.time()
        oc_session_id = session.backend_session_id
        key = self._server_key(session.repo_path)

        # No session ID at all — treat as a fresh start rather than a dead end.
        if not oc_session_id:
            logger.warning(
                "event=opencode_server_resume_no_id gateway_session=%s — falling back to create_session",
                session.session_id,
            )
            session.last_user_message = message
            session.backend_session_id = ""
            return self.create_session(session)

        err = self._ensure_server(key, session.repo_path)
        if err:
            return ExecutionResult(success=False, output="", errors=[err], execution_time=time.time() - start)

        # Verify the session still exists (server may have restarted and lost it).
        info, sess_err = self._http(key, "GET", f"/session/{oc_session_id}")
        if sess_err or not info.get("id"):
            # Session lost — recreate it transparently and continue.
            logger.warning(
                "event=opencode_server_session_lost id=%s gateway_session=%s — recreating",
                oc_session_id, session.session_id,
            )
            session.backend_session_id = ""
            session.last_user_message = message
            return self.create_session(session)

        model_id, provider_id = self._parse_model(self._session_model(session))
        result = self._send_message(
            key=key,
            oc_session_id=oc_session_id,
            message=message,
            cwd=session.repo_path,
            start=start,
            model_id=model_id,
            provider_id=provider_id,
        )
        if not result.success and self._message_transport_failed(result):
            session.backend_session_id = ""
            result.backend_session_id = ""
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
        result = self._send_message(key=key, oc_session_id=oc_session_id, message=message, cwd=cwd, start=start)

        # Only delete on success — on failure the session may hold partial useful state
        # for diagnostics (e.g. the user can check logs). Either way clear the ID so
        # no caller mistakenly tries to resume a deleted/unknown session.
        if result.success:
            self._http(key, "DELETE", f"/session/{oc_session_id}")
        result.backend_session_id = ""
        return result

    def cancel(self, session: Session) -> None:
        oc_id = session.backend_session_id
        key = self._server_key(session.repo_path)
        if oc_id and self._base_urls.get(key):
            self._http(key, "POST", f"/session/{oc_id}/abort")

    def close(self, session: Session) -> None:
        oc_id = session.backend_session_id
        key = self._server_key(session.repo_path)
        if oc_id and self._base_urls.get(key):
            self._http(key, "DELETE", f"/session/{oc_id}")

    def terminate_active_processes(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
            self._procs = {}
            self._base_urls = {}
            # Kill inside the lock so _ensure_server cannot start a new server
            # while the old processes are still alive and own their ports.
            if procs:
                terminate_many_popen(procs)

    @staticmethod
    def _message_transport_failed(result: ExecutionResult) -> bool:
        text = "\n".join(result.errors or []).lower()
        return (
            "opencode server unreachable" in text
            or "opencode server timed out" in text
            or "request timed out" in text
        )

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
    ) -> ExecutionResult:
        try:
            from config import config as _cfg
            # Use the opencode wall-clock budget (default 1800s / 30min) as the
            # HTTP socket timeout.  inactivity_timeout_sec is irrelevant here —
            # the server holds the connection open for the entire generation and
            # sends the complete response at once; there is no per-line output.
            timeout = int(getattr(_cfg.opencode, "timeout_seconds", 1800))
        except Exception:
            timeout = 1800

        body: Dict[str, Any] = {"parts": [{"type": "text", "text": message}]}
        # Set the model inline in the message body — the only reliable way on
        # opencode 1.16.2 (PATCH /session is a no-op that corrupts model state).
        # Only sent when we have a concrete model id; otherwise opencode resolves
        # it from the agent/global config (which already defaults correctly).
        if model_id:
            body["model"] = {"providerID": provider_id or "opencode", "modelID": model_id}
        response, err = self._http(key, "POST", f"/session/{oc_session_id}/message", body, timeout=timeout)

        elapsed = time.time() - start

        if err:
            return ExecutionResult(
                success=False, output="", errors=[err],
                backend_session_id=oc_session_id,
                execution_time=elapsed,
            )

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

            if not repo_path:
                return "repo_path is required to start an opencode server."

            p = Path(repo_path)
            if not p.exists() or not p.is_dir():
                return f"Repository path does not exist or is not a directory: {repo_path}"

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
                )
            except Exception as e:
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
                    return (
                        f"opencode server process exited immediately (exit={proc.returncode}). "
                        + (f"stderr: {stderr_tail}" if stderr_tail else "No stderr captured.")
                    )
                try:
                    with urllib.request.urlopen(f"{base_url}/session", timeout=1) as resp:
                        resp.read()
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
                return (
                    f"opencode server did not start within 15s on {base_url}. "
                    + (f"stderr: {stderr_tail}" if stderr_tail else "No stderr output captured.")
                )

            self._base_urls[key] = base_url
            logger.info("event=opencode_server_ready url=%s pid=%s cwd=%s", base_url, proc.pid, repo_path)
            return None

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

        `timeout` is the socket idle timeout (seconds with no data received).
        For long-running message POSTs the server streams nothing until done,
        so pass a value >= the expected max generation time.

        Connection errors (ECONNREFUSED, timeout on connect) mark that server
        as gone so _ensure_server will restart it on the next call.
        """
        base_url = self._base_urls.get(key, "")
        url = base_url + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}, None
                return json.loads(raw), None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                err_body = json.loads(raw)
                msg = err_body.get("data", {}).get("message") or err_body.get("name") or str(e)
            except Exception:
                msg = raw.decode(errors="replace") if raw else str(e)
            return {}, f"HTTP {e.code} from opencode server ({method} {path}): {msg}"
        except (TimeoutError, socket.timeout) as e:
            # A blocking /message call exceeded the HTTP idle timeout. The
            # opencode server may still be busy with that generation, so kill
            # the process instead of orphaning a wedged server behind a dropped
            # cache entry.
            with self._lock:
                proc = self._procs.pop(key, None)
                self._base_urls.pop(key, None)
            if proc is not None:
                terminate_many_popen([proc])
            return {}, (
                f"opencode server timed out ({method} {path}) after {timeout}s — "
                "killed server; will restart on next call"
            )
        except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
            # Server is gone or unhealthy — clear and terminate our reference so
            # _ensure_server restarts it, and avoid leaving an orphaned process.
            with self._lock:
                proc = self._procs.pop(key, None)
                self._base_urls.pop(key, None)
            if proc is not None:
                terminate_many_popen([proc])
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
