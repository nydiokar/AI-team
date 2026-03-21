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
from typing import List, Optional

from src.core.interfaces import CodingBackend, ExecutionResult, Session

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
        return self._run(session.repo_path, session.last_user_message, resume_id=None)

    def resume_session(self, session: Session, message: str) -> ExecutionResult:
        """Continue an existing session via --resume."""
        return self._run(session.repo_path, message, resume_id=session.backend_session_id or None)

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

    def _run(self, cwd: str, message: str, resume_id: Optional[str]) -> ExecutionResult:
        start = time.time()
        cmd = self._build_cmd(resume_id)
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
            return self._parse(stdout, stderr, proc.returncode, elapsed)
        except Exception as e:
            return ExecutionResult(
                success=False,
                output="",
                errors=[str(e)],
                execution_time=time.time() - start,
            )

    def _build_cmd(self, resume_id: Optional[str]) -> List[str]:
        if resume_id:
            cmd = [self._exe, "--resume", resume_id, "--output-format", "json", "-p"]
        else:
            cmd = [self._exe, "--output-format", "json", "-p"]
        cmd.extend(["--allowedTools", ",".join(_DEFAULT_TOOLS)])
        return cmd

    @staticmethod
    def _parse(stdout: str, stderr: str, returncode: int, elapsed: float) -> ExecutionResult:
        success = returncode == 0
        backend_session_id = ""
        output = stdout

        if stdout:
            try:
                data = json.loads(stdout)
                backend_session_id = data.get("session_id", "")
                output = data.get("result") or data.get("content") or stdout
            except Exception:
                # Try to fish out session_id from a non-clean JSON stream
                for line in stdout.splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            d = json.loads(line)
                            if "session_id" in d:
                                backend_session_id = d["session_id"]
                                break
                        except Exception:
                            pass

        errors = [stderr] if stderr and not success else []
        return ExecutionResult(
            success=success,
            output=output,
            backend_session_id=backend_session_id,
            errors=errors,
            execution_time=elapsed,
        )
