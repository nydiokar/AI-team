"""
CodexBackend — wraps the OpenAI Codex CLI (`codex`).

Resume uses `codex --session <backend_session_id>` if available.
Falls back to stateless run when no session ID is stored.

Synchronous — called via asyncio.to_thread() by the orchestrator.
"""
import json
import logging
import shutil
import subprocess
import time
from typing import List, Optional

from src.core.interfaces import CodingBackend, ExecutionResult, Session

logger = logging.getLogger(__name__)


class CodexBackend(CodingBackend):

    def __init__(self):
        self._exe = shutil.which("codex") or "codex"

    def create_session(self, session: Session) -> ExecutionResult:
        return self._run(session.repo_path, session.last_user_message, resume_id=None)

    def resume_session(self, session: Session, message: str) -> ExecutionResult:
        return self._run(session.repo_path, message, resume_id=session.backend_session_id or None)

    def run_oneoff(self, cwd: str, message: str) -> ExecutionResult:
        return self._run(cwd, message, resume_id=None)

    def cancel(self, session: Session) -> None:
        pass

    def close(self, session: Session) -> None:
        pass

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
        cmd = [self._exe, "--approval-mode", "auto-edit", "-q"]
        if resume_id:
            cmd += ["--session", resume_id]
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
                output = data.get("output") or data.get("content") or stdout
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
