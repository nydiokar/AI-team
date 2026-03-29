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
        cmd = self._build_cmd(resume_id, cwd)
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
