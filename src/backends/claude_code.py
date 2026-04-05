"""
ClaudeCodeBackend wraps the Claude Code CLI.

First turn:  claude -p "<message>" --output-format stream-json ...
Resume turn: claude --resume <backend_session_id> --output-format stream-json -p "<message>"

These methods are synchronous and are called via asyncio.to_thread() by the
orchestrator, so they must NOT use asyncio internally.

The backend_session_id is extracted from Claude's JSON output field `session_id`
and stored in the gateway Session record for subsequent resumes.
"""
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.interfaces import CodingBackend, ExecutionResult, Session

logger = logging.getLogger(__name__)

_DEFAULT_TOOLS = ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob", "Bash"]
_STATUS_LABELS = {
    "A": "created",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type_changed",
    "U": "unmerged",
    "?": "untracked",
}


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


def _normalize_path(raw_path: str) -> str:
    if " -> " in raw_path:
        return raw_path.split(" -> ", 1)[1].strip()
    return raw_path.strip()


def _status_code(status: str) -> str:
    status = (status or "").replace(" ", "")
    for char in status:
        if char != ".":
            return char
    return ""


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, "modified")


def _file_fingerprint(root: str, rel_path: str) -> str:
    path = Path(root) / rel_path
    if not path.exists():
        return "<missing>"
    if path.is_dir():
        return "<dir>"
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except Exception:
        try:
            stat = path.stat()
            return f"<stat:{stat.st_size}:{int(stat.st_mtime_ns)}>"
        except Exception:
            return "<unreadable>"


def _snapshot_worktree(cwd: str) -> Dict[str, Dict[str, str]]:
    """Capture the current dirty worktree state keyed by repo-relative path."""
    stdout = _run_git(cwd, ["status", "--porcelain=v1"])
    if stdout is None:
        return {}

    snapshot: Dict[str, Dict[str, str]] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        status = line[:2]
        path = _normalize_path(line[3:])
        snapshot[path] = {
            "status": status,
            "fingerprint": _file_fingerprint(cwd, path),
        }
    return snapshot


def _line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return sum(1 for _ in handle)
    except Exception:
        return 0


def _current_diff_stats(cwd: str, path: str, status_code: str) -> Dict[str, Optional[int]]:
    """Return current diff stats for a path.

    These stats are net stats against the repo baseline. For files that were
    already dirty before the turn, they are not guaranteed to be strictly
    incremental for just this turn.
    """
    if status_code in ("A", "?"):
        return {"added": _line_count(Path(cwd) / path), "deleted": 0}

    stdout = _run_git(cwd, ["diff", "--numstat", "--", path])
    if stdout:
        for line in stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                added_raw, deleted_raw = parts[0], parts[1]
                added = None if added_raw == "-" else int(added_raw)
                deleted = None if deleted_raw == "-" else int(deleted_raw)
                return {"added": added, "deleted": deleted}

    if status_code == "D":
        return {"added": 0, "deleted": None}
    return {"added": None, "deleted": None}


def _compute_turn_changes(cwd: str, before: Dict[str, Dict[str, str]], after: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    changes: List[Dict[str, Any]] = []
    for path in sorted(after.keys()):
        prev = before.get(path)
        curr = after[path]
        if prev and prev.get("status") == curr.get("status") and prev.get("fingerprint") == curr.get("fingerprint"):
            continue
        status = curr.get("status", "")
        code = _status_code(status)
        stats = _current_diff_stats(cwd, path, code)
        changes.append(
            {
                "path": path,
                "git_status": status,
                "change_type": _status_label(code),
                "added_lines": stats["added"],
                "deleted_lines": stats["deleted"],
            }
        )
    return changes


def _extract_output(payload: Any) -> str:
    """Best-effort extraction of the final user-visible answer from Claude payloads."""
    if isinstance(payload, str):
        return payload.strip()

    if isinstance(payload, list):
        for item in reversed(payload):
            text = _extract_output(item)
            if text:
                return text
        return ""

    if not isinstance(payload, dict):
        return ""

    for key in ("result", "content", "output", "message", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        text = _extract_output(value)
        if text:
            return text

    for key in ("messages", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            text = _extract_output(value)
            if text:
                return text

    if payload.get("type") in ("text", "message") and isinstance(payload.get("text"), str):
        return payload["text"].strip()

    return ""


def _extract_text_blocks(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    parts: List[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts).strip()


class ClaudeCodeBackend(CodingBackend):

    def __init__(self):
        self._exe = shutil.which("claude") or "claude"
        self._active_procs: set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()

    def create_session(self, session: Session) -> ExecutionResult:
        session_id = session.backend_session_id or str(uuid.uuid4())
        return self._run(session.repo_path, session.last_user_message, resume_id=None, session_id=session_id)

    def resume_session(self, session: Session, message: str) -> ExecutionResult:
        return self._run(session.repo_path, message, resume_id=session.backend_session_id or None, session_id=None)

    def run_oneoff(self, cwd: str, message: str) -> ExecutionResult:
        return self._run(cwd, message, resume_id=None)

    def cancel(self, session: Session) -> None:
        self.terminate_active_processes()

    def close(self, session: Session) -> None:
        pass

    def terminate_active_processes(self) -> None:
        with self._proc_lock:
            procs = list(self._active_procs)
        for proc in procs:
            self._terminate_process(proc)

    def _run(self, cwd: str, message: str, resume_id: Optional[str], session_id: Optional[str]) -> ExecutionResult:
        start = time.time()
        cmd = self._build_cmd(resume_id, session_id)
        before_snapshot = _snapshot_worktree(cwd) if cwd else {}
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd or None,
            )
            self._register_process(proc)
            stdout_bytes, stderr_bytes = proc.communicate(input=message.encode())
            stdout = stdout_bytes.decode(errors="replace")
            stderr = stderr_bytes.decode(errors="replace")
            elapsed = time.time() - start
            result = self._parse(stdout, stderr, proc.returncode, elapsed, known_session_id=session_id or resume_id or "")
            if cwd:
                after_snapshot = _snapshot_worktree(cwd)
                result.file_changes = _compute_turn_changes(cwd, before_snapshot, after_snapshot)
                result.files_modified = [item["path"] for item in result.file_changes]
            return result
        except Exception as e:
            return ExecutionResult(
                success=False,
                output="",
                errors=[str(e)],
                execution_time=time.time() - start,
            )
        finally:
            if "proc" in locals():
                self._unregister_process(proc)

    def _build_cmd(self, resume_id: Optional[str], session_id: Optional[str]) -> List[str]:
        if resume_id:
            cmd = [
                self._exe,
                "--resume",
                resume_id,
                "--verbose",
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--dangerously-skip-permissions",
                "-p",
            ]
        else:
            cmd = [
                self._exe,
                "--verbose",
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--dangerously-skip-permissions",
                "-p",
            ]
            if session_id:
                cmd.extend(["--session-id", session_id])
        cmd.extend(["--allowedTools", ",".join(_DEFAULT_TOOLS)])
        return cmd

    def _register_process(self, proc: subprocess.Popen) -> None:
        with self._proc_lock:
            self._active_procs.add(proc)

    def _unregister_process(self, proc: subprocess.Popen) -> None:
        with self._proc_lock:
            self._active_procs.discard(proc)

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                )
            else:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    @staticmethod
    def _parse(stdout: str, stderr: str, returncode: int, elapsed: float, known_session_id: str = "") -> ExecutionResult:
        success = returncode == 0
        backend_session_id = known_session_id or ""
        output = stdout.strip()
        parsed_output = None
        parsed_errors: List[str] = []

        if stdout:
            try:
                data = json.loads(stdout)
                backend_session_id = data.get("session_id", "") or backend_session_id
                parsed_output = data
                output = _extract_output(data)
                maybe_errors = data.get("errors")
                if isinstance(maybe_errors, list):
                    parsed_errors = [str(item).strip() for item in maybe_errors if str(item).strip()]
            except Exception:
                assistant_text = ""
                delta_parts: List[str] = []
                final_result: Optional[Dict[str, Any]] = None
                for line in stdout.splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            d: Dict[str, Any] = json.loads(line)
                            if "session_id" in d:
                                backend_session_id = d["session_id"]
                            if d.get("type") == "assistant":
                                message = d.get("message")
                                text = ""
                                if isinstance(message, dict):
                                    text = _extract_text_blocks(message.get("content"))
                                if text:
                                    assistant_text = text
                            elif d.get("type") == "stream_event":
                                event = d.get("event")
                                if isinstance(event, dict) and event.get("type") == "content_block_delta":
                                    delta = event.get("delta")
                                    if isinstance(delta, dict) and delta.get("type") == "text_delta":
                                        text = delta.get("text")
                                        if isinstance(text, str) and text:
                                            delta_parts.append(text)
                            elif d.get("type") == "result":
                                final_result = d
                                maybe_errors = d.get("errors")
                                if isinstance(maybe_errors, list):
                                    parsed_errors = [str(item).strip() for item in maybe_errors if str(item).strip()]
                            candidate = _extract_output(d)
                            if candidate:
                                output = candidate
                                parsed_output = d
                        except Exception:
                            pass
                if assistant_text:
                    output = assistant_text
                elif delta_parts:
                    output = "".join(delta_parts).strip()
                if final_result is not None:
                    if output:
                        final_result["assistant_text"] = output
                    parsed_output = final_result
                    maybe_errors = final_result.get("errors")
                    if isinstance(maybe_errors, list):
                        parsed_errors = [str(item).strip() for item in maybe_errors if str(item).strip()]
                if not output:
                    output = stdout.strip()

        errors = [stderr.strip()] if stderr and not success else []
        if not success and not errors and parsed_errors:
            errors = parsed_errors
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
