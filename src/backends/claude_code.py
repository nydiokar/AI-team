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
from src.core.turn_liveness import TurnControl, turn_control

logger = logging.getLogger(__name__)

# Shared helper lives in claude_driver (single source of truth for stream-json
# parsing — kept for the backend `_parse` delegator + telemetry retry tests).
from src.backends.claude_driver import _parse_print_resume  # noqa: E402


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

    All turns go through the driver boundary (single turn pipeline, A102).
    """

    def __init__(self, driver_type: str = "auto"):
        # [A102/R2] The SDK continuous driver is the ONE Claude driver; the
        # legacy print/resume CLI driver and driver selection were deleted.
        # `driver_type` is accepted for signature compatibility and ignored.
        from src.backends.claude_driver import ClaudeSDKClientDriver
        self._driver = ClaudeSDKClientDriver()
        logger.info("event=backend_init driver=sdk")

    def _maybe_emit_telemetry(
        self,
        result: ExecutionResult,
        telemetry_context: Optional[TelemetryContext],
        telemetry_sink: Any,
    ) -> None:
        """Post-process raw_stdout and upload telemetry events (M3 Claude adapter).

        Uses ClaudeStreamJsonAdapter to parse the NDJSON lines collected in
        result.raw_stdout and sends the resulting events through telemetry_sink.
        Called at the boundary of each public execution method so it covers
        every turn on the SDK driver (ClaudeSDKClientDriver), including one-offs.

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
        logger.info("event=driver_turn action=%s session_id=%s driver=sdk", action, session_id)

    def _finish(self, session: Session, result: ExecutionResult, before_snapshot: Dict[str, Dict[str, str]], telemetry_context, telemetry_sink) -> ExecutionResult:
        """Shared post-turn processing for the session ops: observe driver /
        cache state, diff the worktree, and emit telemetry."""
        self._observe_driver_state(session, result)
        result = self._observe_cache_health(session, result)
        if session.repo_path:
            after_snapshot = _snapshot_worktree(session.repo_path)
            result.file_changes = _compute_turn_changes(session.repo_path, before_snapshot, after_snapshot)
            result.files_modified = [item["path"] for item in result.file_changes]
        self._maybe_emit_telemetry(result, telemetry_context, telemetry_sink)
        return result

    def create_session(self, session: Session, *, turn: Optional[TurnControl] = None, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        """[A102] Start the native session if needed and run the first prompt
        (``session.last_user_message``), tagged with ``turn.turn_uuid`` for echo
        correlation. ``turn`` carries the carrier's liveness policy; a synthesized
        one is used only by the legacy in-process caller (S2 removes it)."""
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        turn = turn or turn_control(str(uuid.uuid4()))
        self._log_driver_turn("create_session", session.session_id or "")
        proc_env = self._build_proc_env(session.session_id, telemetry_context)
        before_snapshot = _snapshot_worktree(session.repo_path) if session.repo_path else {}

        result = self._driver.start_session(
            session,
            session.last_user_message,
            turn=turn,
            model=_resolve_model(session),
            telemetry_context=telemetry_context,
            proc_env=proc_env,
        )
        return self._finish(session, result, before_snapshot, telemetry_context, telemetry_sink)

    def resume_session(self, session: Session, message: str, *, turn: Optional[TurnControl] = None, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        """[A102] Resume the live session with ``message``: busy/not quiescent ⇒
        typed ``OwnershipConflictError`` before submit (never interrupt); submit
        tagged with ``turn.turn_uuid``; lost ack ⇒ reconcile by id, never
        resubmit; return only OUR correlated reply; expiry or unattributable
        result ⇒ ``RecoveryRequiredError`` with a late result delivered via the
        proactive sink. (All of that is enforced by the driver's single send.)"""
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        turn = turn or turn_control(str(uuid.uuid4()))
        self._log_driver_turn("resume_session", session.session_id or "")
        proc_env = self._build_proc_env(session.session_id, telemetry_context)
        before_snapshot = _snapshot_worktree(session.repo_path) if session.repo_path else {}

        result = self._driver.send_turn(
            session,
            message,
            turn=turn,
            model=_resolve_model(session),
            telemetry_context=telemetry_context,
            proc_env=proc_env,
        )
        return self._finish(session, result, before_snapshot, telemetry_context, telemetry_sink)

    def compact_session(self, session: Session, *, turn: Optional[TurnControl] = None, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        """[A102] `/compact` on the continuous SDK driver (see
        ``ClaudeSDKClientDriver.compact_session``)."""
        from src.core.test_guard import assert_live_calls_allowed
        assert_live_calls_allowed("claude")
        turn = turn or turn_control(str(uuid.uuid4()))
        self._log_driver_turn("compact_session", session.session_id or "")
        proc_env = self._build_proc_env(session.session_id, telemetry_context)
        before_snapshot = _snapshot_worktree(session.repo_path) if session.repo_path else {}

        result = self._driver.compact_session(
            session,
            turn=turn,
            model=_resolve_model(session),
            telemetry_context=telemetry_context,
            proc_env=proc_env,
        )
        return self._finish(session, result, before_snapshot, telemetry_context, telemetry_sink)

    def supports_managed_turns(self) -> bool:
        """The single turn pipeline needs the SDK session's ``--replay-user-messages``
        echo correlation (``WORKER_MANAGED_TURNS``, default ON). A worker without
        it advertises no managed path."""
        from src.backends.claude_driver import _replay_user_messages_enabled

        return self._driver.driver_type() == "sdk" and _replay_user_messages_enabled()

    def provision_sender_capability(self, session_id: str, token: Optional[str]) -> bool:
        """[A82 Stage 5] Per-session sender tool on the SDK driver only."""
        return self._driver.provision_sender_capability(session_id, token)

    def forget_turn(self, session: Session, turn_uuid: str) -> bool:
        """[A102] The carrier learned this turn's row is terminal: drop its
        pending entry so the session can become quiescent again."""
        sessions = getattr(self._driver, "_sessions", None)
        sdk_sess = sessions.get(session.session_id) if sessions is not None else None
        forget = getattr(sdk_sess, "forget_turn", None)
        return bool(callable(forget) and forget(turn_uuid))

    def is_quiescent(self, session: Session) -> bool:
        probe = getattr(self._driver, "is_session_quiescent", None)
        return bool(callable(probe) and probe(session.session_id))

    def run_oneoff(self, cwd: str, message: str, *, turn: Optional[TurnControl] = None, telemetry_context=None, telemetry_sink=None) -> ExecutionResult:
        """[R1] A thin one-off: a throwaway SDK session running ``message`` as its
        first prompt (``create_session`` semantics), with its own ``turn_control``."""
        turn = turn or turn_control(str(uuid.uuid4()))
        proc_env = self._build_proc_env(None, telemetry_context)
        before_snapshot = _snapshot_worktree(cwd) if cwd else {}
        result = self._driver.run_oneoff(cwd, message, turn=turn, model=None, proc_env=proc_env)
        if cwd:
            after_snapshot = _snapshot_worktree(cwd)
            result.file_changes = _compute_turn_changes(cwd, before_snapshot, after_snapshot)
            result.files_modified = [item["path"] for item in result.file_changes]
        self._maybe_emit_telemetry(result, telemetry_context, telemetry_sink)
        return result

    def set_proactive_sink(self, sink: Any) -> None:
        """Register a sink for autonomous turns (background-job continuations).
        Delegates to the SDK driver."""
        setter = getattr(self._driver, "set_proactive_sink", None)
        if callable(setter):
            setter(sink)

    def cancel(self, session: Session, turn_uuid: Optional[str] = None) -> bool:
        """[A102] With a ``turn_uuid``: abort EXACTLY that turn if the CLI is
        running it, arm the interrupt for its echo if it has not begun, and never
        touch another turn (ARM first so a prompt not yet registered is never
        submitted; then deliver to the live pending entry and disarm). Without one:
        a session-wide interrupt of whatever is in flight (kept for the legacy
        in-process caller S2 removes). Returns True iff delivered or durably armed."""
        if turn_uuid is None:
            self._driver.cancel(session)
            return True
        from src.backends.claude_driver import arm_managed_cancel, disarm_managed_cancel

        if not turn_uuid:
            return False
        arm_managed_cancel(turn_uuid)
        sessions = getattr(self._driver, "_sessions", None)
        sdk_sess = sessions.get(session.session_id) if sessions is not None else None
        cancel = getattr(sdk_sess, "cancel_turn", None)
        if callable(cancel) and cancel(turn_uuid):
            disarm_managed_cancel(turn_uuid)  # delivered to the registered prompt
        return True

    # ------------------------------------------------------------------ #
    # [A102 S1] Deprecated managed-* shims — one-liners that build a
    # TurnControl from the carrier's ownership and call the natural method.
    # The carrier (src/worker/agent.py) still calls these until S2 deletes
    # both the shims and this seam.
    # ------------------------------------------------------------------ #
    def run_managed_turn(self, session: Session, message: str, ownership, *, telemetry_context=None, telemetry_sink=None, on_process=None) -> ExecutionResult:
        return self.resume_session(session, message, turn=turn_control(getattr(ownership, "turn_uuid", None) or "", ownership=ownership, on_process=on_process), telemetry_context=telemetry_context, telemetry_sink=telemetry_sink)

    def run_managed_compaction(self, session: Session, ownership, *, telemetry_context=None, telemetry_sink=None, on_process=None) -> ExecutionResult:
        return self.compact_session(session, turn=turn_control(getattr(ownership, "turn_uuid", None) or "", ownership=ownership, on_process=on_process), telemetry_context=telemetry_context, telemetry_sink=telemetry_sink)

    def cancel_managed_turn(self, session: Session, turn_uuid: str) -> bool:
        return self.cancel(session, turn_uuid)

    def forget_managed_turn(self, session: Session, turn_uuid: str) -> bool:
        return self.forget_turn(session, turn_uuid)

    def close(self, session: Session) -> None:
        self._driver.close(session)

    def live_session_count(self) -> int:
        """Count of pooled live backend sessions (SDK driver only; else 0)."""
        counter = getattr(self._driver, "live_session_count", None)
        return counter() if callable(counter) else 0

    def mark_sessions_lost(self) -> None:
        """Called on worker restart — all live SDK sessions are orphaned."""
        from src.backends.claude_driver import ClaudeSDKClientDriver
        if isinstance(self._driver, ClaudeSDKClientDriver):
            # Clear the session map; driver_status is updated by the orchestrator
            for sid in list(self._driver._sessions.keys()):
                self._driver.mark_lost(sid)

    def terminate_active_processes(self) -> None:
        # Close all live SDK sessions (the one Claude driver).
        for sdk_sess in list(self._driver._sessions.values()):
            sdk_sess.close()

    # ------------------------------------------------------------------
    # Backward-compatibility delegators
    # The canonical implementations live in claude_driver.py.
    # ------------------------------------------------------------------

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

