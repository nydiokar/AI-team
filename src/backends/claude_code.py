"""
ClaudeCodeBackend — wraps the Claude Code CLI.

First turn:  claude -p "<message>" --output-format json ...
Resume turn: claude --resume <backend_session_id> --output-format json -p "<message>"

These methods are synchronous and are called via asyncio.to_thread() by the
orchestrator, so they must NOT use asyncio internally.

The backend_session_id is extracted from Claude's JSON output field `session_id`
and stored in the gateway Session record for subsequent resumes.
"""
import json
import logging
import shutil
import subprocess
import time
import uuid
from typing import List, Optional

from src.core.interfaces import CodingBackend, ExecutionResult, Session


def _detect_changed_files(cwd: str) -> List[str]:
    """Return list of modified/added/deleted files via git status --porcelain."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        files = []
        for line in result.stdout.splitlines():
            if line.strip():
                # porcelain format: "XY filename" — filename starts at col 3
                files.append(line[3:].strip())
        return files
    except Exception:
        return []

logger = logging.getLogger(__name__)

_DEFAULT_TOOLS = ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob", "Bash"]


class ClaudeCodeBackend(CodingBackend):

    def __init__(self):
        self._exe = shutil.which("claude") or "claude"

    # ------------------------------------------------------------------
    # CodingBackend interface  (all sync — called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def create_session(self, session: Session) -> ExecutionResult:
        """First turn — no resume flag, capture the native session ID from output."""
        session_id = session.backend_session_id or str(uuid.uuid4())
        return self._run(session.repo_path, session.last_user_message, resume_id=None, session_id=session_id)

    def resume_session(self, session: Session, message: str) -> ExecutionResult:
        """Continue an existing session via --resume."""
        return self._run(session.repo_path, message, resume_id=session.backend_session_id or None, session_id=None)

    def run_oneoff(self, cwd: str, message: str) -> ExecutionResult:
        """Stateless single turn."""
        return self._run(cwd, message, resume_id=None)

    def cancel(self, session: Session) -> None:
        # Subprocess cancellation is handled by the orchestrator via asyncio task cancellation.
        pass

    def close(self, session: Session) -> None:
        # No persistent server-side state to clean up for Claude Code.
        pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self, cwd: str, message: str, resume_id: Optional[str], session_id: Optional[str]) -> ExecutionResult:
        start = time.time()
        cmd = self._build_cmd(resume_id, session_id)
        try:
            proc = subprocess.run(
                cmd,
                input=message.encode(),
                capture_output=True,
                cwd=cwd or None,
            )
            stdout = proc.stdout.decode(errors="replace")
            stderr = proc.stderr.decode(errors="replace")
            elapsed = time.time() - start
            result = self._parse(stdout, stderr, proc.returncode, elapsed, known_session_id=session_id or resume_id or "")
            if cwd:
                result.files_modified = _detect_changed_files(cwd)
            return result
        except Exception as e:
            return ExecutionResult(
                success=False,
                output="",
                errors=[str(e)],
                execution_time=time.time() - start,
            )

    def _build_cmd(self, resume_id: Optional[str], session_id: Optional[str]) -> List[str]:
        if resume_id:
            cmd = [self._exe, "--resume", resume_id, "--output-format", "text", "--dangerously-skip-permissions", "-p"]
        else:
            cmd = [self._exe, "--output-format", "text", "--dangerously-skip-permissions", "-p"]
            if session_id:
                cmd.extend(["--session-id", session_id])
        cmd.extend(["--allowedTools", ",".join(_DEFAULT_TOOLS)])
        return cmd

    @staticmethod
    def _parse(stdout: str, stderr: str, returncode: int, elapsed: float, known_session_id: str = "") -> ExecutionResult:
        success = returncode == 0
        backend_session_id = known_session_id or ""
        output = stdout.strip()
        parsed_output = None

        if stdout:
            try:
                data = json.loads(stdout)
                backend_session_id = data.get("session_id", "")
                parsed_output = data
                output = (data.get("result") or data.get("content") or "").strip()
            except Exception:
                # Keep plain text stdout as the user-visible result for session turns.
                for line in stdout.splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            d = json.loads(line)
                            if "session_id" in d:
                                backend_session_id = d["session_id"]
                            if parsed_output is None:
                                parsed_output = d
                            candidate = (d.get("result") or d.get("content") or "").strip()
                            if candidate:
                                output = candidate
                                break
                        except Exception:
                            pass

        errors = [stderr.strip()] if stderr and not success else []
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
