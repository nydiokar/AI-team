"""
ClaudeCodeBackend — wraps the Claude Code CLI.

First turn:  claude -p "<message>" --output-format json ...
Resume turn: claude --resume <backend_session_id> -p "<message>" --output-format json ...

The backend_session_id is extracted from Claude's JSON output field `session_id`
and stored in the gateway Session record for subsequent resumes.
"""
import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import List, Optional

from src.core.interfaces import CodingBackend, ExecutionResult, Session

logger = logging.getLogger(__name__)

_DEFAULT_TOOLS = ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob", "Bash"]


class ClaudeCodeBackend(CodingBackend):

    def __init__(self):
        self._exe = shutil.which("claude") or "claude"

    # ------------------------------------------------------------------
    # CodingBackend interface
    # ------------------------------------------------------------------

    def create_session(self, session: Session) -> ExecutionResult:
        """First turn — no resume flag, capture the native session ID from output."""
        return asyncio.get_event_loop().run_until_complete(
            self._run(session.repo_path, session.last_user_message, resume_id=None)
        )

    def resume_session(self, session: Session, message: str) -> ExecutionResult:
        """Continue an existing session via --resume."""
        return asyncio.get_event_loop().run_until_complete(
            self._run(session.repo_path, message, resume_id=session.backend_session_id or None)
        )

    def run_oneoff(self, cwd: str, message: str) -> ExecutionResult:
        """Stateless single turn."""
        return asyncio.get_event_loop().run_until_complete(
            self._run(cwd, message, resume_id=None)
        )

    def cancel(self, session: Session) -> None:
        # Claude Code has no remote cancel API; subprocess cancellation is
        # handled by the orchestrator via asyncio task cancellation.
        pass

    def close(self, session: Session) -> None:
        # No persistent server-side state to clean up for Claude Code.
        pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(self, cwd: str, message: str, resume_id: Optional[str]) -> ExecutionResult:
        start = time.time()
        cmd = self._build_cmd(message, resume_id)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or None,
            )
            stdout_b, stderr_b = await proc.communicate(message.encode())
            stdout = stdout_b.decode(errors="replace")
            stderr = stderr_b.decode(errors="replace")
            elapsed = time.time() - start
            return self._parse(stdout, stderr, proc.returncode, elapsed)
        except Exception as e:
            return ExecutionResult(
                success=False,
                output="",
                errors=[str(e)],
                execution_time=time.time() - start,
            )

    def _build_cmd(self, message: str, resume_id: Optional[str]) -> List[str]:
        cmd = [self._exe, "--output-format", "json", "-p"]
        if resume_id:
            cmd = [self._exe, "--resume", resume_id, "--output-format", "json", "-p"]
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
