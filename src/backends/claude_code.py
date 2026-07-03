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
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

from src.core.process_utils import ensure_node_on_path
from src.core.interfaces import CodingBackend, ExecutionResult, Session
from src.core.telemetry import TelemetryContext, new_telemetry_id, telemetry_subprocess_env

logger = logging.getLogger(__name__)

# Shared helpers live in claude_driver (single source of truth).
from src.backends.claude_driver import (  # noqa: E402
    _extract_output,
    _extract_text_blocks,
    _mcp_jobs_configured,
    _parse_print_resume,
)


def _resolve_model(session: Session) -> Optional[str]:
    """Resolve the model for this session via the shared catalog logic."""
    try:
        from config.models import resolve_model
        return resolve_model(session)
    except Exception:
        return None
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
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
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


class ClaudeCodeBackend(CodingBackend):
    """CodingBackend implementation for the Claude Code CLI.

    Delegates to a ClaudeDriver (SDK continuous driver by default, print/resume
    as fallback). The driver choice is made once at construction time and applies
    to all sessions managed by this backend instance.

    The existing _run/_build_cmd methods are kept for run_oneoff and for the
    test suite that asserts on _build_cmd output. New multi-turn session calls
    go through the driver boundary.
    """

    def __init__(self, driver_type: str = "auto"):
        # "auto" means defer to config; explicit values bypass config
        if driver_type == "auto":
            try:
                from config import config as _cfg
                driver_type = getattr(_cfg.claude, "driver_type", "sdk")
            except Exception:
                driver_type = "sdk"
        from src.backends.claude_driver import build_driver, ClaudePrintResumeDriver
        self._driver = build_driver(driver_type)
        active = self._driver.driver_type()
        if active == "print_resume":
            logger.warning(
                "event=backend_degraded driver=print_resume "
                "— ClaudeCodeBackend is running on the LEGACY CLI driver. "
                "Long sessions burn tokens on context reconstruction, are not "
                "persistent, and are subject to inactivity timeouts. "
                "Verify claude_agent_sdk is installed in the venv or set CLAUDE_DRIVER_TYPE=sdk."
            )
        else:
            logger.info("event=backend_init driver=%s", active)
        # Fallback driver: one-off calls go through this, not a legacy _run.
        self._fallback = ClaudePrintResumeDriver()

    def _maybe_emit_telemetry(
        self,
        result: ExecutionResult,
        telemetry_context: Optional[TelemetryContext],
        telemetry_sink: Any,
    ) -> None:
        """Post-process raw_stdout and upload telemetry events (M3 Claude adapter).

        Uses ClaudeStreamJsonAdapter to parse the NDJSON lines collected in
        result.raw_stdout and sends the resulting events through telemetry_sink.
        Called at the boundary of each public execution method so it covers both
        the SDK driver path (ClaudeSDKClientDriver) and the legacy CLI path
        (ClaudePrintResumeDriver / run_oneoff).

        Contract:
        - Never raises into the caller (spec §8.2).
        - No-op when telemetry_context, telemetry_sink, or raw_stdout are absent.
        - Emits exactly ONE model.request.usage event per invocation (double-count
          guard is inside ClaudeStreamJsonAdapter).
        """
        if telemetry_context is None or telemetry_sink is None:
            return
        raw_stdout = getattr(result, "raw_stdout", None) or ""
        if not raw_stdout:
            return
        try:
            from src.core.telemetry_adapters.claude_stream_json import ClaudeStreamJsonAdapter
            adapter = ClaudeStreamJsonAdapter(
                telemetry_context,
                emitter_process_instance_id=new_telemetry_id("proc"),
            )
            events = adapter.coverage_events()
            for line in raw_stdout.splitlines():
                events.extend(adapter.consume_line(line))
            # Flush any pending assistant usage not superseded by a result event
            # (e.g. stream was truncated by an inactivity kill before type=result).
            events.extend(adapter.flush_pending_usage())
            if events:
                telemetry_sink.emit_many(events)
        except Exception:
            logger.debug(
                "event=claude_telemetry_post_process_failed "
                "turn_id=%s invocation_id=%s",
                getattr(telemetry_context, "turn_id", "?"),
                getattr(telemetry_context, "invocation_id", "?"),
                exc_info=True,
            )

    def _log_driver_turn(self, action: str, session_id: str) -> None:
        """Log which driver is handling this turn — WARNING when legacy CLI is active."""
        active = self._driver.driver_type()
        if active == "print_resume":
            logger.warning(
                "event=legacy_driver_active action=%s session_id=%s driver=print_resume "
                "— using LEGACY CLI driver, not SDK. Sessions are stateless; long turns "
                "reconstruct full context from disk (token-heavy). "
                "Check that claude_agent_sdk is installed and CLAUDE_DRIVER_TYPE=sdk.",
                action, session_id,
            )
        else:
            logger.info("event=driver_turn action=%s session_id=%s driver=%s", action, session_id, active)

    def create_session(self, session: Session, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        self._log_driver_turn("create_session", session.session_id or "")
        proc_env = self._build_proc_env(session.backend_session_id or str(uuid.uuid4()), telemetry_context)
        before_snapshot = _snapshot_worktree(session.repo_path) if session.repo_path else {}

        result = self._driver.start_session(
            session,
            session.last_user_message,
            model=_resolve_model(session),
            telemetry_context=telemetry_context,
            proc_env=proc_env,
        )
        self._observe_driver_state(session, result)
        result = self._observe_cache_health(session, result)
        if session.repo_path:
            after_snapshot = _snapshot_worktree(session.repo_path)
            result.file_changes = _compute_turn_changes(session.repo_path, before_snapshot, after_snapshot)
            result.files_modified = [item["path"] for item in result.file_changes]
        self._maybe_emit_telemetry(result, telemetry_context, telemetry_sink)
        return result

    def resume_session(self, session: Session, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        # Guards are checked BEFORE the live-call gate so they work in test mode too.
        self._log_driver_turn("resume_session", session.session_id or "")

        # Guard: if session was lost after worker restart, don't silently resume
        # via print/resume into stale context.
        if session.driver_status == "lost" and self._driver.driver_type() != "print_resume":
            return ExecutionResult(
                success=False,
                output="",
                errors=[
                    "Claude session was lost after a worker restart and cannot be resumed "
                    "by the continuous driver. Start a new session or explicitly request "
                    "fallback resume."
                ],
                error_class="session_lost",
            )

        # Guard: if cache is unhealthy twice, block silent print/resume continuation
        if (
            session.cache_health == "unhealthy"
            and session.cache_unhealthy_count >= 2
            and self._driver.driver_type() == "print_resume"
        ):
            return ExecutionResult(
                success=False,
                output="",
                errors=[
                    f"Claude session cache is unhealthy ({session.cache_unhealthy_count} times). "
                    "Context is being fully recreated every turn, burning subscription quota. "
                    "Start a new session to reset cache health."
                ],
                error_class="cache_unhealthy",
            )

        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        proc_env = self._build_proc_env(session.backend_session_id, telemetry_context)
        before_snapshot = _snapshot_worktree(session.repo_path) if session.repo_path else {}

        result = self._driver.send_turn(
            session,
            message,
            model=_resolve_model(session),
            telemetry_context=telemetry_context,
            proc_env=proc_env,
        )
        self._observe_driver_state(session, result)
        result = self._observe_cache_health(session, result)
        if session.repo_path:
            after_snapshot = _snapshot_worktree(session.repo_path)
            result.file_changes = _compute_turn_changes(session.repo_path, before_snapshot, after_snapshot)
            result.files_modified = [item["path"] for item in result.file_changes]
        self._maybe_emit_telemetry(result, telemetry_context, telemetry_sink)
        return result

    def run_oneoff(self, cwd: str, message: str, *, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        proc_env = self._build_proc_env(None, telemetry_context)
        before_snapshot = _snapshot_worktree(cwd) if cwd else {}
        result = self._fallback.run_oneoff(cwd, message, model=None, proc_env=proc_env)
        if cwd:
            after_snapshot = _snapshot_worktree(cwd)
            result.file_changes = _compute_turn_changes(cwd, before_snapshot, after_snapshot)
            result.files_modified = [item["path"] for item in result.file_changes]
        self._maybe_emit_telemetry(result, telemetry_context, telemetry_sink)
        return result

    def cancel(self, session: Session) -> None:
        self._driver.cancel(session)

    def close(self, session: Session) -> None:
        self._driver.close(session)

    def mark_sessions_lost(self) -> None:
        """Called on worker restart — all live SDK sessions are orphaned."""
        from src.backends.claude_driver import ClaudeSDKClientDriver
        if isinstance(self._driver, ClaudeSDKClientDriver):
            # Clear the session map; driver_status is updated by the orchestrator
            for sid in list(self._driver._sessions.keys()):
                self._driver.mark_lost(sid)

    def terminate_active_processes(self) -> None:
        # For SDK driver, close all live sessions
        from src.backends.claude_driver import ClaudeSDKClientDriver, ClaudePrintResumeDriver
        if isinstance(self._driver, ClaudeSDKClientDriver):
            for sdk_sess in list(self._driver._sessions.values()):
                sdk_sess.close()
        elif isinstance(self._driver, ClaudePrintResumeDriver):
            self._driver.terminate_active_processes()

    # ------------------------------------------------------------------
    # Backward-compatibility delegators
    # These keep existing tests and callers working without modification.
    # The canonical implementations live in claude_driver.py.
    # ------------------------------------------------------------------

    def _build_cmd(self, resume_id: Optional[str], session_id: Optional[str], model: Optional[str] = None) -> List[str]:
        """Thin delegator — single source of truth is ClaudePrintResumeDriver._build_cmd."""
        return self._fallback._build_cmd(resume_id, session_id, model)

    @staticmethod
    def _parse(
        stdout: str,
        stderr: str,
        returncode: int,
        elapsed: float,
        known_session_id: str = "",
    ) -> ExecutionResult:
        """Thin delegator — single source of truth is _parse_print_resume in claude_driver."""
        return _parse_print_resume(stdout, stderr, returncode, elapsed, known_session_id)

    @staticmethod
    def _build_proc_env(session_id: Optional[str], telemetry_context: Optional[TelemetryContext]) -> dict:
        proc_env = ensure_node_on_path()
        if session_id:
            proc_env["SESSION_ID"] = session_id
        proc_env.update(telemetry_subprocess_env(telemetry_context))
        return proc_env

    @staticmethod
    def _observe_driver_state(session: Session, result: ExecutionResult) -> None:
        """Persist the selected driver mode on the Session object after a turn."""
        if session.driver_type == "sdk":
            session.driver_status = "live" if result.success else (session.driver_status or "")
        elif session.driver_type == "print_resume":
            session.driver_status = "closed"

    @staticmethod
    def _observe_cache_health(session: Session, result: ExecutionResult) -> ExecutionResult:
        """Parse cache stats from result and mutate session health fields in-place."""
        from src.backends.claude_driver import parse_cache_stats_from_ndjson, CacheStats
        stats = parse_cache_stats_from_ndjson(result.raw_stdout)
        if stats is None:
            return result
        if stats.is_unhealthy:
            session.cache_health = "unhealthy"
            session.cache_unhealthy_count += 1
            logger.warning(
                "Cache unhealthy for session %s: creation=%d hit_ratio=%.2f (count=%d)",
                session.session_id,
                stats.cache_creation,
                stats.hit_ratio,
                session.cache_unhealthy_count,
            )
        else:
            session.cache_health = "healthy"
        return result

