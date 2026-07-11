"""
Main gateway orchestrator.

The current intended product path is session-first:
Telegram -> gateway session -> Claude Code / Codex native resume.
"""
import asyncio
import json
import logging
import time
import shutil
import subprocess
import re
import socket
import threading
from pathlib import Path
from typing import Callable, Dict, List, Any, Optional, Tuple
from datetime import datetime
import uuid
import random
import contextlib

import sys
import os

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

from src.core import (
    ITaskOrchestrator, Task, TaskResult, TaskStatus, TaskType, TaskPriority,
    SessionStatus,
)
from src.services import (
    TaskParser, AsyncFileWatcher, SessionStore, PathResolver,
    SessionService, WorkflowService, NotificationService,
)
from src.bridges import LlamaMediator
from src.backends.registry import build_backends
from config import config
from src.validation.engine import ValidationEngine

logger = logging.getLogger(__name__)


class HarnessAdmissionBlocked(Exception):
    """Raised by `_enqueue_task` when the task-harness Level-3 admission gate
    refuses a task at the queue choke point (flag on + `harness_level: 3` +
    not `approved: true`).

    Raised — not returned — so no caller can mistake a blocked task for an
    accepted one (there is no `task_id` to hand back). Callers that face an
    operator (Telegram, control API) catch this and surface a clear
    "needs operator approval" result instead of a generic error.
    """

    def __init__(self, task_id: str, reason: str = "harness_level3_needs_approval"):
        self.task_id = task_id
        self.reason = reason
        super().__init__(f"task {task_id} blocked at admission: {reason}")


class TaskOrchestrator(ITaskOrchestrator):
    """Main gateway coordinator.

    Responsibilities:
    - Watch `tasks/` for new `.task.md` files and parse them into `Task` objects
    - Queue and execute tasks concurrently with bounded worker pool
    - Route session tasks into backend-native Claude/Codex resume flows
    - Keep LLAMA limited to optional helper duties such as summarization
    - Persist artifacts (`results/*.json`, `summaries/*.txt`) and maintain a lightweight index
    - Emit structured events to `logs/events.ndjson` for observability

    Threading/async model:
    - File system events come from watchdog thread → marshalled into asyncio loop
    - Workers are asyncio Tasks consuming from an in-memory queue
    """
    
    def __init__(self):
        # Initialize core components
        self.task_parser = TaskParser()
        self.file_watcher = AsyncFileWatcher(config.system.tasks_dir)
        self.llama_mediator = LlamaMediator()
        self.session_store = SessionStore()
        self.session_service = SessionService(
            self.session_store,
            remote_close_dispatcher=self._dispatch_remote_close,
        )
        self.workflow_service = WorkflowService()
        self._backends = build_backends()
        from src.control.telemetry_sink import build_runtime_telemetry_sink
        self._telemetry_sink = build_runtime_telemetry_sink(
            node_id=socket.gethostname(),
            logs_dir=config.system.logs_dir,
            is_gateway=True,
        )
        with contextlib.suppress(Exception):
            replay = getattr(self._telemetry_sink, "replay_spool", None)
            if callable(replay):
                replay()
        
        # Task management
        self.task_queue = asyncio.Queue(maxsize=config.system.max_queue_size)
        self.active_tasks: Dict[str, Task] = {}
        self.task_results: Dict[str, TaskResult] = {}
        
        # System state
        self.running = False
        self.worker_tasks: List[asyncio.Task] = []
        # Embedded mesh task server (started only when MESH_ENABLED) — shares the
        # gateway event loop so get_registry() is the same singleton dispatch uses.
        self._embedded_task_server = None
        # Embedded control API (read surface for the Web UI) — shares the gateway
        # event loop so it reads the live SessionService / NodeRegistry. U1.
        self._embedded_control_api = None
        
        # Component status
        self.component_status = {
            "claude_available": False,
            "llama_available": False,
            "file_watcher_running": False
        }
        # In-memory lock to prevent duplicate processing of the same task file
        self._inflight_paths: set[str] = set()
        
        logger.info("TaskOrchestrator initialized")
        self.validation_engine = ValidationEngine()
        # Ensure logs directory exists for event emission
        try:
            Path(config.system.logs_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        # Queue persistence
        self._state_path = Path(config.system.logs_dir) / "state.json"
        self._pending_files: set[str] = set()
        self._load_state()
        # Artifact index path (task_id -> latest artifact path)
        self._artifact_index_path = Path(config.system.results_dir) / "index.json"
        # Lazy-initialized context loader (simple functional helper encapsulated here)
        self._context_loader = None
        # Task ids that have already had compact prior-context injected into their
        # prompt (opt-in `continues:` continuation). Instance-local so the guard
        # never leaks into task.metadata / the remote payload / persisted artifacts.
        self._compact_injected_ids: set[str] = set()
        # Job completion polling (T3)
        self._last_job_poll = datetime.now().isoformat()
        self._last_remote_job_poll = datetime.now().isoformat()
        self._remote_job_poll_started_epoch = time.time()
        self._processed_terminal_jobs: set[str] = set()
        self._watched_jobs_cache_lock = threading.Lock()
        self._watched_jobs_remote_cache: Dict[
            tuple[Optional[str], Optional[str], int],
            tuple[float, Dict[str, List[Dict[str, Any]]]],
        ] = {}
        self._watched_jobs_remote_cache_ttl_sec = 2.0
        # Cancellation and runtime tracking
        self._task_cancel_events: Dict[str, asyncio.Event] = {}
        self._running_exec_tasks: Dict[str, asyncio.Task] = {}
        self._shutdown_interrupted_tasks: set[str] = set()
        self._stale_busy_reconcile_task: Optional[asyncio.Task] = None
        self._mesh_reconcile_in_progress: bool = False
        
        # Initialize Telegram interface if configured
        self.telegram_interface = None
        if config.telegram.bot_token:
            try:
                from src.telegram.interface import TelegramInterface
                self.telegram_interface = TelegramInterface(
                    bot_token=config.telegram.bot_token,
                    orchestrator=self,
                    allowed_users=config.telegram.allowed_users
                )
                logger.info("Telegram interface initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize Telegram interface: {e}")
                self.telegram_interface = None
        else:
            logger.info("Telegram interface not configured (no bot token)")

        # Notification dispatcher — single call site for all outbound
        # notifications (Telegram today, Web UI tomorrow).  Passes self
        # so the notifer reads ``self.telegram_interface`` dynamically.
        self.notifier = NotificationService(orchestrator=self)

    @staticmethod
    def _extract_text_from_payload(payload: Any) -> str:
        """Best-effort extraction of a user-visible answer from structured payloads."""
        if isinstance(payload, str):
            text = payload.strip()
            if not text:
                return ""
            if text.startswith("{") or text.startswith("["):
                try:
                    return TaskOrchestrator._extract_text_from_payload(json.loads(text))
                except Exception:
                    return text
            return text

        if isinstance(payload, list):
            for item in reversed(payload):
                text = TaskOrchestrator._extract_text_from_payload(item)
                if text:
                    return text
            return ""

        if not isinstance(payload, dict):
            return ""

        for key in ("result", "content", "output", "message", "text"):
            value = payload.get(key)
            text = TaskOrchestrator._extract_text_from_payload(value)
            if text:
                return text

        for key in ("messages", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                text = TaskOrchestrator._extract_text_from_payload(value)
                if text:
                    return text

        return ""

    @classmethod
    def _extract_rate_limit_info(cls, result: TaskResult) -> Optional[Dict[str, Any]]:
        """Parse the first rejected rate_limit_event from raw_stdout NDJSON, or None."""
        stdout = getattr(result, "raw_stdout", "") or ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line or "rate_limit_event" not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") != "rate_limit_event":
                continue
            info = obj.get("rate_limit_info", {})
            if info.get("status") == "rejected":
                return info
        return None

    @classmethod
    def _session_reply_text(cls, result: TaskResult) -> str:
        """User-facing text for Telegram session completions."""
        for candidate in (
            result.output,
            cls._extract_text_from_payload(result.parsed_output),
            result.raw_stdout,
        ):
            text = cls._extract_text_from_payload(candidate)
            if text:
                return text

        return (
            "Claude completed the run but returned no final reply text.\n\n"
            "Check the artifact JSON for raw stdout/stderr and backend metadata."
        )

    @classmethod
    def _failure_text(cls, result: TaskResult) -> str:
        """Aggregate likely error-bearing text from the result payload."""
        parts: List[str] = []

        def _append(value: Any) -> None:
            if value is None:
                return
            text = cls._extract_text_from_payload(value)
            if text:
                parts.append(text)
            elif isinstance(value, str) and value.strip():
                parts.append(value.strip())

        for err in (result.errors or []):
            _append(err)
        _append(getattr(result, "raw_stderr", ""))
        _append(getattr(result, "raw_stdout", ""))
        _append(getattr(result, "parsed_output", None))
        _append(getattr(result, "output", ""))
        return "\n".join(parts)

    @classmethod
    def _short_failure_reason(cls, result: TaskResult) -> str:
        """Return a concise, user-facing failure reason."""
        if result.success:
            return ""

        texts: List[str] = [str(err).strip() for err in (result.errors or []) if str(err).strip()]
        haystack = cls._failure_text(result)
        haystack_lower = haystack.lower()

        if "cancelled" in haystack_lower:
            return "Task cancelled"
        if cls._is_missing_backend_conversation(result):
            return "Claude session expired"
        if any(s in haystack_lower for s in ("rate_limit_event", "rate limit", "rate-limit", "too many requests", "hit your limit", "\"error\":\"rate_limit\"", "overagestatus")):
            info = cls._extract_rate_limit_info(result)
            if info:
                limit_type = info.get("rateLimitType", "")
                resets_at = info.get("resetsAt")
                type_label = {"five_hour": "5-hour", "hourly": "hourly", "daily": "daily"}.get(limit_type, limit_type.replace("_", "-") if limit_type else "")
                prefix = f"Claude {type_label} usage limit reached" if type_label else "Claude usage limit reached"
                if resets_at:
                    try:
                        reset_dt = datetime.fromtimestamp(int(resets_at))
                        reset_str = reset_dt.strftime("%H:%M")
                        return f"{prefix} — resets at {reset_str}"
                    except Exception:
                        pass
                reset_match = re.search(r"resets?\s+([^\n\"\}·]{1,50})", haystack, flags=re.IGNORECASE)
                if reset_match:
                    return f"{prefix} — resets {reset_match.group(1).strip()}"
                return prefix
            reset_match = re.search(r"resets?\s+([^\n\"\}·]{1,50})", haystack, flags=re.IGNORECASE)
            if reset_match:
                return f"Claude usage limit reached — resets {reset_match.group(1).strip()}"
            return "Claude usage limit reached"
        if any(s in haystack_lower for s in ("prompt is too long", "blocking_limit", "context_window", "context window")):
            return "Session context full — use /compact or start a new session"
        if any(s in haystack_lower for s in ("not logged in", "authentication", "unauthorized", "forbidden")):
            return "Claude authentication error"
        if any(s in haystack_lower for s in ("timeout", "timed out", "inactivity")):
            # Pass through the richer error text when it's already actionable
            for t in texts:
                tl = t.lower()
                if "timed out" in tl or "timeout" in tl or "inactivity" in tl:
                    compact = " ".join(t.split())
                    if len(compact) > 20:
                        return compact[:300]
            return "Claude timeout"
        if any(s in haystack_lower for s in ("connection reset", "connection aborted", "network error", "temporarily unavailable", "service unavailable")):
            return "Claude network error"
        if any(isinstance(e, str) and "interactive_prompt_detected" in e for e in (result.errors or [])):
            return "Claude needs interactive approval"

        exit_code_text: Optional[str] = None
        for text in texts:
            low = text.lower()
            if low.startswith("claude exited with code "):
                exit_code_text = text  # defer — prefer any richer error first
                continue
            compact = " ".join(text.split())
            if compact:
                return compact[:120]

        # Surface raw_stderr before giving up — it often holds the real reason
        raw_stderr = (getattr(result, "raw_stderr", "") or "").strip()
        if raw_stderr:
            first_line = next((ln.strip() for ln in raw_stderr.splitlines() if ln.strip()), "")
            if first_line:
                suffix = f" ({exit_code_text})" if exit_code_text else ""
                return f"{first_line[:200]}{suffix}"

        # At minimum, be honest about the exit code instead of "Claude failed"
        if exit_code_text:
            return exit_code_text[:120]

        return "Claude failed"

    def _resolve_task_backend(self, task: Task) -> str:
        """Resolve the backend associated with a task before it finishes."""
        session_id = (task.metadata or {}).get("session_id", "").strip()
        if session_id:
            session = self.session_store.get(session_id)
            if session and session.backend:
                return str(session.backend).strip().lower()
        backend_name = str((task.metadata or {}).get("backend") or "claude").strip().lower()
        return backend_name or "claude"

    @staticmethod
    def _backend_event_name(backend_name: str, phase: str) -> str:
        backend = (backend_name or "claude").strip().lower() or "claude"
        return f"{backend}_{phase}"

    def _mark_local_claude_driver_sessions_lost_after_restart(self, host: str) -> int:
        """A gateway restart orphaned live SDK clients owned by this process."""
        marked = 0
        for session in self.session_store.list_all():
            if session.backend != "claude":
                continue
            if session.driver_type != "sdk" or session.driver_status != "live":
                continue
            if session.machine_id not in ("", host):
                continue
            if session.status not in (SessionStatus.IDLE, SessionStatus.AWAITING_INPUT):
                continue
            session.driver_status = "lost"
            self.session_store.save(session)
            marked += 1
        if marked:
            logger.warning("event=local_driver_sessions_marked_lost host=%s count=%d", host, marked)
        backend = self._backends.get("claude")
        marker = getattr(backend, "mark_sessions_lost", None)
        if callable(marker):
            with contextlib.suppress(Exception):
                marker()
        return marked

    async def _recover_stale_busy_sessions(self) -> None:
        """Recover BUSY sessions after a gateway restart.

        Uses the DB to distinguish three cases instead of blindly marking ERROR:
        1. Task completed in DB → restore session to AWAITING_INPUT, propagate result.
        2. Task still pending/claimed in DB → skip (worker will finish it).
        3. No DB record or DB unavailable → mark ERROR (legacy fallback).

        When DB is unavailable, falls back to the original behaviour: mark all
        stale BUSY sessions as ERROR.
        """
        host = socket.gethostname()
        active_task_ids = set(self.active_tasks.keys())
        self._mark_local_claude_driver_sessions_lost_after_restart(host)

        db = None
        try:
            from src.control.db import get_db
            db = get_db()
        except Exception:
            pass

        for session in self.session_store.list_all():
            # A18: a session caught mid-hold (PAUSED_PINNED_NODE_OFFLINE) by a
            # gateway restart has lost its in-memory liveness poll. It was never
            # dispatched off-host (the hold polls *before* dispatch), so there is
            # nothing to reattach — surface the honest, resumable terminal state
            # so the operator can retry / re-pin, instead of wedging it in a
            # transient PAUSED state forever. (Never occurs while the feature is
            # disabled, i.e. MESH_AFFINITY_OFFLINE_GRACE_SEC=0.)
            if session.status == SessionStatus.PAUSED_PINNED_NODE_OFFLINE:
                session.status = SessionStatus.PINNED_NODE_OFFLINE
                session.last_result_summary = (
                    "Pinned node was offline and the gateway restarted during the "
                    "affinity hold; retry when the node is back, or re-pin."
                )
                self.session_store.save(session)
                self._emit_event("affinity_hold_interrupted_by_restart", None, {
                    "session_id": session.session_id,
                    "machine_id": session.machine_id,
                })
                continue
            if session.status != SessionStatus.BUSY:
                continue
            is_remote = bool(session.machine_id and session.machine_id != host)
            if session.last_task_id and session.last_task_id in active_task_ids:
                continue

            task_id = session.last_task_id
            if db is not None and task_id:
                row = db.get_task(task_id)
                if row:
                    status = row.get("status")
                    if status == "completed":
                        # Task completed successfully while gateway was down.
                        # Restore session and propagate the result.
                        await self._recover_completed_session(session, row)
                        continue
                    elif status in ("pending", "claimed"):
                        # Worker is still working on it. For a remote (mesh)
                        # session the worker lives on another node and keeps
                        # running across our restart, so reattach a poll loop
                        # that will deliver its real result. For a local session
                        # the in-process worker is gone, so just defer.
                        if is_remote:
                            logger.info(
                                "event=session_recovery_reattach session_id=%s task_id=%s status=%s node=%s",
                                session.session_id, task_id, status, session.machine_id,
                            )
                            asyncio.create_task(self._reattach_remote_task(session, row))
                        else:
                            logger.info(
                                "event=session_recovery_deferred session_id=%s task_id=%s status=%s",
                                session.session_id, task_id, status,
                            )
                        continue
                    # Other terminal status (failed, failed_node_offline) — fall
                    # through to ERROR marking below.

            # A remote session with no usable DB row falls through to ERROR like
            # any other: we genuinely don't know the task's state.

            # No DB record available (or DB unavailable), or task failed.
            session.status = SessionStatus.ERROR
            session.last_result_summary = "Interrupted by gateway restart; partial changes may exist."
            self.session_store.save(session)
            result = TaskResult(
                task_id=task_id or f"session_{session.session_id}",
                success=False,
                output="",
                errors=["interrupted by gateway restart"],
                files_modified=[],
                execution_time=0.0,
                timestamp=datetime.now().isoformat(),
            )
            setattr(result, "backend_name", session.backend or "claude")
            self._write_session_summary(session, result)
            self._append_session_event(session.session_id, task_id or "", result)
            self._emit_event(
                "session_interrupted_recovered",
                None,
                {"session_id": session.session_id, "task_id": task_id, "backend": session.backend},
            )
            await self.notifier.notify_error(
                "Task interrupted by gateway restart",
                task_id=task_id or session.session_id,
                chat_id=session.telegram_chat_id,
            )

    async def _recover_completed_session(self, session: Any, task_row: Dict[str, Any]) -> None:
        """Restore a session whose task completed in DB while the gateway was down."""
        result_raw = task_row.get("result")
        result_dict: Dict[str, Any] = {}
        if result_raw:
            try:
                result_dict = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
            except Exception:
                pass

        if not result_dict:
            logger.warning(
                "event=recovery_missing_result session_id=%s task_id=%s",
                session.session_id, task_row.get("id"),
            )

        exec_time = result_dict.get("execution_time", 0.0)
        if not isinstance(exec_time, (int, float)):
            exec_time = 0.0

        session.status = SessionStatus.AWAITING_INPUT
        full_out = (result_dict.get("output", "") or "").strip() or "Task completed (recovered)"
        session.last_result_summary = full_out[-400:] if len(full_out) > 400 else full_out
        session.last_files_modified = result_dict.get("files_modified") or []
        # Propagate the backend_session_id the worker established, exactly as the
        # live dispatch path does (_dispatch_to_node). Without this the recovered
        # session has no backend_session_id and the next turn can't resume the
        # remote Claude session — it would silently start a fresh one.
        recovered_bsid = result_dict.get("backend_session_id", "")
        if recovered_bsid:
            session.backend_session_id = recovered_bsid
        artifact_path = task_row.get("artifact_path") or ""
        if artifact_path:
            session.last_artifact_path = artifact_path
        session.task_history.append({
            "task_id": task_row["id"],
            "timestamp": result_dict.get("timestamp", datetime.now().isoformat()),
            "success": True,
            "execution_time": round(exec_time, 2),
            "user_message": session.last_user_message,
            "result_summary": full_out,
            "files_modified": session.last_files_modified[:20],
        })
        session.task_history = session.task_history[-20:]
        self.session_store.save(session)

        result = TaskResult(
            task_id=task_row["id"],
            success=True,
            output=result_dict.get("output", ""),
            errors=[],
            files_modified=result_dict.get("files_modified") or [],
            execution_time=exec_time,
            timestamp=datetime.now().isoformat(),
        )
        setattr(result, "backend_name", session.backend or "claude")
        self._write_session_summary(session, result)
        self._append_session_event(session.session_id, task_row["id"], result)
        self._emit_event(
            "session_recovered_completed",
            None,
            {"session_id": session.session_id, "task_id": task_row["id"], "backend": session.backend},
        )

        await self.notifier.notify_task_outcome(
            task_row["id"],
            result,
            session=session,
            chat_id=session.telegram_chat_id,
            prefix="_(recovered after a gateway restart)_\n\n",
        )

    def _start_stale_busy_reconciler(self) -> None:
        """Start the periodic M3 reconciliation loop when mesh routing is active."""
        interval = int(getattr(config.mesh, "session_reconcile_interval_sec", 60) or 0)
        if not config.mesh.enabled or interval <= 0:
            return
        if self._stale_busy_reconcile_task and not self._stale_busy_reconcile_task.done():
            return
        self._stale_busy_reconcile_task = asyncio.create_task(
            self._stale_busy_reconciliation_loop(interval)
        )

    async def _stale_busy_reconciliation_loop(self, interval_sec: int) -> None:
        logger.info("event=stale_busy_reconciler_started interval=%ds", interval_sec)
        try:
            while self.running:
                try:
                    await self._reconcile_stale_busy_sessions_once()
                except Exception as e:
                    logger.debug("event=stale_busy_reconcile_failed err=%s", e)
                await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            logger.info("event=stale_busy_reconciler_stopped")
            raise

    async def _reconcile_stale_busy_sessions_once(self) -> int:
        """Mark BUSY sessions with no active task row as ERROR."""
        try:
            from src.control.db import get_db
            db = get_db()
        except Exception:
            db = None
        if db is None:
            return 0

        rows = await asyncio.to_thread(db.list_stale_busy_sessions)
        reconciled = 0
        active_task_ids = set(self.active_tasks.keys())
        for row in rows:
            session_id = row.get("session_id", "")
            task_id = row.get("last_task_id", "") or ""
            if task_id and task_id in active_task_ids:
                continue

            session = self.session_store.get(session_id)
            if session is None or session.status != SessionStatus.BUSY:
                continue

            if task_id:
                task_row = db.get_task(task_id)
                status = task_row.get("status") if task_row else None
                if status == "completed":
                    await self._recover_completed_session(session, task_row)
                    reconciled += 1
                    continue
                if status in ("pending", "claimed"):
                    continue
                if status in ("failed", "failed_node_offline"):
                    error_msg = (task_row.get("error") if task_row else "") or f"Task {status}"
                    session.last_result_summary = error_msg[-400:]

            session.status = SessionStatus.ERROR
            if not session.last_result_summary:
                session.last_result_summary = (
                    "Marked error by mesh reconciliation: session was busy with no active task."
                )
            self.session_store.save(session)

            result = TaskResult(
                task_id=task_id or f"session_{session_id}",
                success=False,
                output="",
                errors=[session.last_result_summary or "stale busy session: no pending or claimed mesh task"],
                files_modified=[],
                execution_time=0.0,
                timestamp=datetime.now().isoformat(),
            )
            setattr(result, "backend_name", session.backend or "claude")
            self._append_session_event(session_id, task_id, result)
            self._emit_event(
                "stale_busy_session_reconciled",
                None,
                {
                    "session_id": session_id,
                    "task_id": task_id,
                    "machine_id": session.machine_id,
                    "backend": session.backend,
                },
            )
            logger.warning(
                "event=stale_busy_session_reconciled session_id=%s task_id=%s node=%s",
                session_id,
                task_id,
                session.machine_id,
            )
            reconciled += 1

        return reconciled

    async def _reattach_remote_task(self, session: Any, task_row: Dict[str, Any]) -> None:
        """Reattach to a remote task still in-flight after a gateway restart.

        The worker on `session.machine_id` kept running across our restart and
        owns the task's terminal state in the DB. We poll the row until it
        reaches a terminal status, then report the worker's *real* result to
        Telegram — never a fabricated one. This is the startup half of the
        detach/reattach handoff (the shutdown half lives in _dispatch_to_node).

        Pending pickup is bounded by the same oneoff_queue_timeout. Once the
        worker has claimed the row, that queue timeout no longer applies.
        """
        import asyncio as _aio
        from src.control.db import get_db

        task_id = task_row.get("id") or ""
        db = get_db()
        if db is None or not task_id:
            return

        pickup_timeout_sec = getattr(config.mesh, "oneoff_queue_timeout_sec", 600)
        pickup_deadline = time.time() + pickup_timeout_sec
        poll_interval = 3.0

        while True:
            if not self.running:
                # Gateway is shutting down again; detach quietly. The next
                # startup will reattach from the still-claimed DB row.
                return
            row = db.get_task(task_id)
            status = row.get("status") if row else None
            if status == "completed":
                await self._recover_completed_session(session, row)
                return
            if status in ("failed", "failed_node_offline"):
                result_raw = row.get("result") if row else None
                try:
                    result_dict = json.loads(result_raw) if isinstance(result_raw, str) else (result_raw or {})
                except Exception:
                    result_dict = {}
                error_msg = (row.get("error") if row else "") or f"Task {status}"
                session.status = SessionStatus.ERROR
                session.last_result_summary = error_msg[-400:]
                self.session_store.save(session)
                result = TaskResult(
                    task_id=task_id,
                    success=False,
                    output=result_dict.get("output", "") if result_dict else "",
                    errors=result_dict.get("errors") or [error_msg],
                    files_modified=result_dict.get("files_modified") or [],
                    execution_time=result_dict.get("execution_time", 0.0),
                    timestamp=result_dict.get("timestamp", datetime.now().isoformat()) if result_dict else datetime.now().isoformat(),
                    return_code=result_dict.get("return_code", 1) if result_dict else 1,
                    raw_stdout=result_dict.get("output", "") if result_dict else "",
                    raw_stderr=(result_dict.get("error_detail", "") if result_dict else ""),
                )
                setattr(result, "error_detail", result_dict.get("error_detail", "") if result_dict else "")
                setattr(result, "usage", result_dict.get("usage") if result_dict else None)
                setattr(result, "backend_name", session.backend or "claude")
                self._write_session_summary(session, result)
                self._append_session_event(session.session_id, task_id, result)
                self._emit_event(
                    "session_recovered_failed",
                    None,
                    {"session_id": session.session_id, "task_id": task_id, "backend": session.backend},
                )
                await self.notifier.notify_error(
                    f"Task failed on remote node while gateway was restarting: {error_msg}",
                    task_id=task_id,
                    chat_id=session.telegram_chat_id,
                )
                return
            if status != "claimed" and time.time() >= pickup_deadline:
                break
            # still pending/claimed before pickup timeout, or claimed execution
            # after pickup — keep waiting for the worker's terminal state.
            await _aio.sleep(poll_interval)

        # Timed out waiting for a terminal state. Don't fabricate a result —
        # surface the uncertainty and unblock the session.
        logger.warning(
            "event=reattach_timeout session_id=%s task_id=%s node=%s",
            session.session_id, task_id, session.machine_id,
        )
        session.status = SessionStatus.ERROR
        session.last_result_summary = (
            "Lost contact with the remote node after a gateway restart; "
            "the task's outcome is unknown."
        )
        self.session_store.save(session)
        await self.notifier.notify_error(
            "Lost contact with the remote node after a restart; task outcome unknown.",
            task_id=task_id,
            chat_id=session.telegram_chat_id,
        )

    async def _job_completion_poller(self) -> None:
        """Poll for terminal watched jobs and push Telegram notifications.

        Runs as a background task during the gateway's lifetime. Checks every
        30s for jobs that reached terminal state since the last poll.
        """
        while self.running:
            try:
                from src.control.db import get_db
                db = get_db()
                if db is None:
                    await asyncio.sleep(30)
                    continue

                # The task server owns terminal job state; the gateway owns
                # routing those terminal jobs to session-visible notifications.
                terminal = db.get_terminal_jobs_since(self._last_job_poll)
                if terminal:
                    self._last_job_poll = datetime.now().isoformat()

                for job in terminal:
                    await self._process_terminal_job(job)

                remote_terminal = self._remote_terminal_jobs_since(self._last_remote_job_poll)
                if remote_terminal:
                    self._last_remote_job_poll = datetime.now().isoformat()

                for job in remote_terminal:
                    await self._process_terminal_job(job)
            except Exception as e:
                logger.debug("event=job_poller_error err=%s", e)

            try:
                await asyncio.wait_for(asyncio.sleep(30), timeout=30)
            except asyncio.TimeoutError:
                pass

    def _remote_jobs_client(self):
        """Return a task-server client for CONTROLLER_URL, if this gateway has one."""
        controller_url = os.environ.get("CONTROLLER_URL", "").strip().rstrip("/")
        token = config.mesh.worker_token
        if not controller_url or not token:
            return None
        try:
            from src.control.task_server_client import TaskServerClient
            return TaskServerClient(controller_url, token, timeout=2)
        except Exception as e:
            logger.debug("event=remote_jobs_client_unavailable err=%s", e)
            return None

    def list_watched_jobs(
        self,
        limit: int = 20,
        session_id: Optional[str] = None,
        ownership: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return watched jobs visible to this gateway.

        The local Web UI can run on a machine whose MCP/worker points at a
        remote task server via CONTROLLER_URL. In that topology, local SQLite is
        empty but the real watched jobs live on the controller. Merge both views
        by job id so System > Jobs reflects the actual registration target.
        When session_id is supplied, return only jobs owned by that session so
        Session/Project surfaces do not have to filter a global operator list.
        """
        running: List[Dict[str, Any]] = []
        recent: List[Dict[str, Any]] = []
        try:
            from src.control.db import get_db
            db = get_db()
            if db is not None:
                running.extend(
                    db.list_jobs(
                        status="running",
                        session_id=session_id,
                        ownership=ownership,
                        limit=limit,
                    )
                )
                recent.extend(
                    db.list_jobs(
                        session_id=session_id,
                        ownership=ownership,
                        limit=limit,
                    )
                )
        except Exception as e:
            logger.debug("event=local_jobs_list_failed err=%s", e)

        remote = self._cached_remote_watched_jobs(
            limit=limit,
            session_id=session_id,
            ownership=ownership,
        )
        running.extend(remote["running"])
        recent.extend(remote["recent"])

        return {
            "running": self._dedupe_jobs(running, limit),
            "recent": self._dedupe_jobs(recent, limit),
        }

    def _cached_remote_watched_jobs(
        self,
        *,
        limit: int,
        session_id: Optional[str],
        ownership: Optional[str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        if not hasattr(self, "_watched_jobs_cache_lock"):
            self._watched_jobs_cache_lock = threading.Lock()
        if not hasattr(self, "_watched_jobs_remote_cache"):
            self._watched_jobs_remote_cache = {}
        if not hasattr(self, "_watched_jobs_remote_cache_ttl_sec"):
            self._watched_jobs_remote_cache_ttl_sec = 2.0

        cache_key = (session_id, ownership, limit)
        now = time.monotonic()
        cached = self._watched_jobs_remote_cache.get(cache_key)
        if cached and now - cached[0] <= self._watched_jobs_remote_cache_ttl_sec:
            return cached[1]

        if not self._watched_jobs_cache_lock.acquire(blocking=False):
            return cached[1] if cached else {"running": [], "recent": []}

        try:
            cached = self._watched_jobs_remote_cache.get(cache_key)
            now = time.monotonic()
            if cached and now - cached[0] <= self._watched_jobs_remote_cache_ttl_sec:
                return cached[1]

            client = self._remote_jobs_client()
            if client is None:
                return {"running": [], "recent": []}

            result = {
                "running": client.list_jobs(
                    status="running",
                    session_id=session_id,
                    ownership=ownership,
                    limit=limit,
                ),
                "recent": client.list_jobs(
                    session_id=session_id,
                    ownership=ownership,
                    limit=limit,
                ),
            }
            self._watched_jobs_remote_cache[cache_key] = (now, result)
            return result
        finally:
            self._watched_jobs_cache_lock.release()

    def _dedupe_jobs(self, jobs: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        seen: set[str] = set()
        out: List[Dict[str, Any]] = []
        for job in jobs:
            job_id = str(job.get("id") or "")
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            out.append(job)
            if len(out) >= limit:
                break
        return out

    def _remote_terminal_jobs_since(self, since: str) -> List[Dict[str, Any]]:
        client = self._remote_jobs_client()
        if client is None:
            return []
        started_after = float(getattr(self, "_remote_job_poll_started_epoch", 0.0) or 0.0)
        terminal: List[Dict[str, Any]] = []
        for job in client.list_jobs(limit=50):
            job_id = str(job.get("id") or "")
            if not job_id or job_id in self._processed_terminal_jobs:
                continue
            if str(job.get("status") or "") not in {"done", "failed", "lost"}:
                continue
            started_epoch = job.get("started_epoch")
            if isinstance(started_epoch, (int, float)) and started_epoch < started_after:
                continue
            if started_epoch is None and str(job.get("updated_at") or "") <= since:
                continue
            terminal.append(job)
        return terminal

    def _job_notification_payload(self, job: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(job.get("id") or "")
        label = str(job.get("label") or job_id or "unknown")
        status = str(job.get("status") or "done")
        exit_code = job.get("exit_code")
        tail = str(job.get("tail") or "")
        success = status == "done"

        prompt = f"Watched job finished: {label}"
        lines = [f"Watched job `{label}` {status}."]
        if exit_code is not None:
            lines.append(f"Exit code: `{exit_code}`")
        if job.get("notify_agent"):
            lines.append("Agent continuation requested.")
        if tail:
            lines.append(f"\nLast log lines:\n```\n{tail[-1500:]}\n```")

        return {
            "job_id": job_id,
            "label": label,
            "status": status,
            "success": success,
            "prompt": prompt,
            "reply": "\n".join(lines),
        }

    def _record_job_session_turn(self, job: Dict[str, Any], session: Any, payload: Dict[str, Any]) -> None:
        job_id = str(payload["job_id"])
        reply = str(payload["reply"])
        prompt = str(payload["prompt"])
        success = bool(payload["success"])
        now = datetime.now().isoformat()

        try:
            from src.control.db import get_db
            db = get_db()
            if db is not None:
                db.enqueue_task(
                    task_id=job_id,
                    session_id=session.session_id,
                    machine_id=getattr(session, "machine_id", None),
                    backend=getattr(session, "backend", None) or "unknown",
                    action="watched_job",
                    payload={
                        "task": {"id": job_id, "title": prompt, "prompt": prompt},
                        "job": {
                            "id": job_id,
                            "label": payload["label"],
                            "status": payload["status"],
                        },
                    },
                )
                result_dict = {
                    "success": success,
                    "output": reply,
                    "errors": [] if success else [reply],
                    "files_modified": [],
                    "execution_time": 0.0,
                    "timestamp": now,
                    "return_code": job.get("exit_code"),
                }
                if success:
                    db.complete_task(job_id, result_dict, None)
                else:
                    db.fail_task(job_id, reply, result=result_dict)
                try:
                    with db._write() as conn:
                        conn.execute(
                            """
                            UPDATE mesh_tasks
                            SET created_at = ?, completed_at = ?, updated_at = ?
                            WHERE id = ? AND action = 'watched_job'
                            """,
                            (now, now, now, job_id),
                        )
                except Exception as e:
                    logger.debug("event=job_session_turn_time_update_failed job_id=%s err=%s", job_id, e)
                db.enrich_task(
                    job_id,
                    prompt=prompt,
                    reply_text=reply,
                    parsed_output={"type": "watched_job", "job": job},
                    files_modified=[],
                    return_code=job.get("exit_code"),
                )
                db.append_event(
                    session_id=session.session_id,
                    task_id=job_id,
                    success=success,
                    execution_time=0.0,
                    error="" if success else reply,
                )
        except Exception as e:
            logger.warning("event=job_session_turn_db_failed job_id=%s err=%s", job_id, e)

        try:
            history = list(getattr(session, "task_history", None) or [])
            exists = any(
                item.get("task_id") == job_id
                for item in history
                if isinstance(item, dict)
            )
            if not exists:
                history.append({
                    "task_id": job_id,
                    "timestamp": now,
                    "success": success,
                    "execution_time": 0.0,
                    "user_message": prompt,
                    "result_summary": reply,
                    "files_modified": [],
                })
                session.task_history = history[-20:]
            session.last_task_id = job_id
            session.last_result_summary = reply[-400:] if len(reply) > 400 else reply
            session.last_summary = session.last_result_summary
            session.last_files_modified = []
            self.session_store.save(session)
        except Exception as e:
            logger.warning("event=job_session_turn_save_failed job_id=%s err=%s", job_id, e)

    async def _process_terminal_job(self, job: Dict[str, Any]) -> None:
        job_id_key = str(job.get("id") or "")
        processed = getattr(self, "_processed_terminal_jobs", None)
        if processed is None:
            processed = set()
            self._processed_terminal_jobs = processed
        if job_id_key in processed:
            return
        processed.add(job_id_key)

        if not job.get("notify") and not job.get("notify_agent"):
            return

        payload = self._job_notification_payload(job)
        job_id = str(payload["job_id"])
        session_id = str(job.get("session_id") or "")
        session = self.session_store.get(session_id) if session_id else None
        if session is None:
            logger.info("event=job_notify_skipped job_id=%s reason=no_session", job_id)
            return

        self._record_job_session_turn(job, session, payload)

        if job.get("notify"):
            result = TaskResult(
                task_id=job_id,
                success=bool(payload["success"]),
                output=str(payload["reply"]),
                errors=[] if payload["success"] else [str(payload["reply"])],
                files_modified=[],
                execution_time=0.0,
                timestamp=datetime.now().isoformat(),
                return_code=job.get("exit_code") or 0,
            )
            try:
                await self.notifier.notify_task_outcome(
                    job_id,
                    result,
                    session=session,
                    chat_id=getattr(session, "telegram_chat_id", None),
                )
            except Exception as e:
                logger.warning("event=job_notify_failed job_id=%s err=%s", job_id, e)

        if job.get("notify_agent"):
            description = (
                f"The watched job `{payload['label']}` finished with status "
                f"`{payload['status']}`.\n\n{payload['reply']}"
            )
            try:
                await self.submit_instruction(
                    description,
                    session_id=session.session_id,
                    cwd=getattr(session, "repo_path", None),
                    source="watched_job",
                    extra_metadata={"job_id": job_id, "source": "watched_job"},
                    # [M2] This follow-up task is dispatched BY the watched-job
                    # subsystem — record that provenance on its flow_runs row
                    # (flag-guarded; no-op when HARNESS_FLOW_DRIVE is OFF).
                    dispatched_by=f"watched_job:{job_id}",
                )
            except Exception as e:
                logger.warning("event=job_notify_agent_failed job_id=%s err=%s", job_id, e)

    @staticmethod
    def _format_file_change_lines(result: TaskResult, limit: int = 20) -> List[str]:
        changes = list(getattr(result, "file_changes", None) or [])
        if changes:
            lines: List[str] = []
            for item in changes[:limit]:
                path = item.get("path", "")
                change_type = str(item.get("change_type", "modified")).capitalize()
                added = item.get("added_lines")
                deleted = item.get("deleted_lines")
                stats = ""
                if added is not None or deleted is not None:
                    stats = f" (+{added if added is not None else '?'}/-{deleted if deleted is not None else '?'})"
                lines.append(f"  `{path}` [{change_type}{stats}]")
            if len(changes) > limit:
                lines.append(f"  _...and {len(changes) - limit} more_")
            return lines

        files = result.files_modified or []
        lines = [f"  `{f}`" for f in files[:limit]]
        if len(files) > limit:
            lines.append(f"  _...and {len(files) - limit} more_")
        return lines

    @staticmethod
    def _is_missing_backend_conversation(result: TaskResult) -> bool:
        texts = list(result.errors or [])
        po = getattr(result, "parsed_output", None)
        if isinstance(po, dict):
            maybe_errors = po.get("errors")
            if isinstance(maybe_errors, list):
                texts.extend(str(item) for item in maybe_errors)
        haystack = "\n".join(str(item) for item in texts).lower()
        return "no conversation found with session id" in haystack
    
    async def start(self):
        """Start all system components.

        Actions:
        - Check component availability (Claude CLI, LLAMA)
        - Spawn worker coroutines up to `config.system.max_concurrent_tasks`
        - Resume any pending files captured in persisted state
        - Start the file watcher to ingest newly created task files
        """
        if self.running:
            logger.warning("Orchestrator is already running")
            return
        
        logger.info("Starting Telegram Coding Gateway...")

        # Mark running BEFORE starting workers so they don't immediately exit
        self.running = True

        # Start task processing workers
        for i in range(config.system.max_concurrent_tasks):
            worker = asyncio.create_task(self._task_worker(f"worker-{i}"))
            self.worker_tasks.append(worker)

        # Start the embedded mesh task server (no-op unless MESH_ENABLED)
        await self._start_embedded_task_server()

        # Start the embedded control API (read surface for the Web UI)
        await self._start_embedded_control_api()

        # Resume pending before starting watcher to avoid duplicate/racy processing
        try:
            for file_path in list(self._pending_files):
                p = Path(file_path)
                processed_dir = Path(config.system.tasks_dir) / "processed"
                if p.exists() and p.parent != processed_dir:
                    await self._handle_new_task_file(file_path)
                else:
                    self._pending_files.discard(file_path)
            self._save_state()
        except Exception as e:
            logger.warning(f"event=state_resume_failed error={e}")

        # Start file watcher after resuming pending
        await self.file_watcher.start_async(self._handle_new_task_file)
        self.component_status["file_watcher_running"] = True

        # Check component availability now that all components are up
        await self._check_component_status()
        self.reconcile_spooled_mesh_completions(limit=100)
        asyncio.create_task(self._warm_llama_helpers())
        
        # Start Telegram interface if available
        if self.telegram_interface:
            try:
                await self.telegram_interface.start()
                logger.info("Telegram interface started")
            except Exception as e:
                logger.error(f"Failed to start Telegram interface: {e}")
                await self.stop()
                raise
        await self._recover_stale_busy_sessions()
        self._start_stale_busy_reconciler()

        # Start the job completion poller (T3 — Watched Jobs)
        asyncio.create_task(self._job_completion_poller())

        # Log startup status
        self._log_startup_status()
        
        logger.info("Telegram Coding Gateway started successfully!")
    
    async def stop(self):
        """Stop orchestrator and all workers.

        Ensures graceful cancellation of workers and stops the file watcher.
        """
        if not self.running:
            return
        
        logger.info("Stopping Telegram Coding Gateway...")
        
        self.running = False

        interrupted_ids = list(self.active_tasks.keys())
        for task_id in interrupted_ids:
            self._shutdown_interrupted_tasks.add(task_id)
            ev = self._task_cancel_events.get(task_id)
            if ev is None:
                ev = asyncio.Event()
                self._task_cancel_events[task_id] = ev
            ev.set()
            exec_task = self._running_exec_tasks.get(task_id)
            if exec_task is not None and not exec_task.done():
                exec_task.cancel()

        if interrupted_ids:
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if not any(task_id in self.active_tasks for task_id in interrupted_ids):
                    break
                await asyncio.sleep(0.1)

        # Terminate any live backend child processes before worker cancellation.
        for backend in self._backends.values():
            terminate = getattr(backend, "terminate_active_processes", None)
            if callable(terminate):
                try:
                    terminate()
                except Exception as e:
                    logger.warning(f"Failed to terminate backend processes: {e}")
        
        # Stop Telegram interface if available
        if self.telegram_interface:
            try:
                await self.telegram_interface.stop()
                logger.info("Telegram interface stopped")
            except Exception as e:
                logger.error(f"Failed to stop Telegram interface: {e}")
        
        # Stop file watcher
        await self.file_watcher.stop_async()
        self.component_status["file_watcher_running"] = False

        # Stop the embedded mesh task server (no-op if it was never started)
        await self._stop_embedded_task_server()

        # Stop the embedded control API (no-op if it was never started)
        await self._stop_embedded_control_api()

        if self._stale_busy_reconcile_task and not self._stale_busy_reconcile_task.done():
            self._stale_busy_reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stale_busy_reconcile_task
        self._stale_busy_reconcile_task = None

        # Cancel worker tasks
        for worker in self.worker_tasks:
            worker.cancel()
        
        # Wait for workers to finish
        await asyncio.gather(*self.worker_tasks, return_exceptions=True)
        self.worker_tasks.clear()
        
        logger.info("Telegram Coding Gateway stopped")

    async def _start_embedded_task_server(self) -> None:
        """Start the in-process mesh task server in single-process / fallback mode.

        State Separation Phase 2: by default the task server now runs as its own
        process (server_main.py / ai-team-server) and this is a no-op. It only
        starts embedded when `MESH_EMBEDDED_SERVER=true` — the single-process or
        mesh-broken fallback mode. Running it on the gateway's event loop makes
        the HTTP handlers and the orchestrator share one get_registry() singleton.

        When embedded is off, node discovery in the remote path falls through to
        the shared DB (see _process_task_remote), so dispatch still works.
        """
        if not config.mesh.enabled:
            return
        if not config.mesh.embedded_server:
            logger.info(
                "event=embedded_task_server_skipped reason=standalone_mode "
                "(set MESH_EMBEDDED_SERVER=true to embed; otherwise run ai-team-server)"
            )
            return
        if self._embedded_task_server is not None:
            return
        host = config.mesh.tailscale_ip or "127.0.0.1"
        port = config.mesh.task_server_port
        try:
            from src.control.embedded_server import EmbeddedTaskServer
            server = EmbeddedTaskServer(host=host, port=port)
            await server.start()
            self._embedded_task_server = server
            # Bind the proactive-turn hook so autonomous (background-job) turns a
            # worker reports get delivered through the gateway's notification
            # fan-out. Capture the running loop here (we ARE on it) so the hook,
            # invoked from the server's threadpool, can marshal the async notify
            # back onto it.
            try:
                self._loop = asyncio.get_running_loop()
                from src.control import task_server as _task_server
                _task_server.bind_proactive_hook(self._handle_proactive_turn)
            except Exception as e:
                logger.warning(f"event=proactive_hook_bind_failed err={e}")
            logger.info(
                f"event=embedded_task_server_up host={host} port={port}"
            )
        except Exception as e:
            # Don't take the whole gateway down if the task server fails to bind;
            # log loudly so the operator notices mesh routing is degraded.
            logger.error(f"event=embedded_task_server_start_failed err={e}")
            self._embedded_task_server = None

    async def _stop_embedded_task_server(self) -> None:
        if self._embedded_task_server is None:
            return
        try:
            await self._embedded_task_server.stop()
        except Exception as e:
            logger.warning(f"event=embedded_task_server_stop_failed err={e}")
        finally:
            self._embedded_task_server = None

    async def _start_embedded_control_api(self) -> None:
        """Start the in-process Control API (read surface for the Web UI). U1.

        Serves /api/sessions|tasks|nodes|events on dashboard_port from inside the
        gateway, sharing this process's SessionService and NodeRegistry — the
        replacement for the standalone dashboard_main.py process. Disabled by
        CONTROL_API_ENABLED=false. A bind failure logs loudly but never takes the
        gateway down (same posture as the embedded task server).
        """
        if not config.mesh.control_api_enabled:
            logger.info("event=control_api_skipped reason=disabled (CONTROL_API_ENABLED=false)")
            return
        if self._embedded_control_api is not None:
            return
        # Bind host: CONTROL_API_HOST wins; else the Tailscale IP (reachable only by
        # tailnet devices — the private-network auth layer); else localhost. Never
        # default to 0.0.0.0 (that would expose the UI+API on every interface).
        host = config.mesh.control_api_host or config.mesh.tailscale_ip or "127.0.0.1"
        port = config.mesh.dashboard_port
        try:
            from src.control.embedded_server import EmbeddedControlServer
            server = EmbeddedControlServer(orchestrator=self, host=host, port=port)
            await server.start()
            self._embedded_control_api = server
            logger.info(f"event=control_api_up host={host} port={port}")
        except Exception as e:
            logger.error(f"event=control_api_start_failed err={e}")
            self._embedded_control_api = None

    async def _stop_embedded_control_api(self) -> None:
        if self._embedded_control_api is None:
            return
        try:
            await self._embedded_control_api.stop()
        except Exception as e:
            logger.warning(f"event=control_api_stop_failed err={e}")
        finally:
            self._embedded_control_api = None

    async def reload_worker_pool(self):
        """Reload worker pool size from environment configuration at runtime"""
        try:
            # Reload config from environment
            config.reload_from_env()
            target_workers = config.system.max_concurrent_tasks
            current_workers = len(self.worker_tasks)
            
            if target_workers == current_workers:
                logger.info(f"Worker pool unchanged: {current_workers} workers")
                return
            
            logger.info(f"Adjusting worker pool: {current_workers} -> {target_workers}")
            
            if target_workers > current_workers:
                # Add more workers
                for i in range(current_workers, target_workers):
                    worker = asyncio.create_task(self._task_worker(f"worker-{i}"))
                    self.worker_tasks.append(worker)
                logger.info(f"Added {target_workers - current_workers} workers")
                self._emit_event("worker_pool_scaled", None, {"from": current_workers, "to": target_workers})
                
            elif target_workers < current_workers:
                # Remove excess workers
                workers_to_remove = current_workers - target_workers
                for i in range(workers_to_remove):
                    worker = self.worker_tasks.pop()
                    worker.cancel()
                logger.info(f"Removed {workers_to_remove} workers")
                self._emit_event("worker_pool_scaled", None, {"from": current_workers, "to": target_workers})
                
        except Exception as e:
            logger.error(f"Failed to reload worker pool: {e}")
            self._emit_event("worker_pool_reload_failed", None, {"error": str(e)})
    
    async def _check_component_status(self):
        """Check availability of core components and cache status.

        Populates `self.component_status` with:
        - claude_available: Claude CLI detected and responsive
        - llama_available: optional Ollama helper path is fully usable
        - file_watcher_running: based on watcher state
        """
        
        # Check Claude Code CLI
        self.component_status["claude_available"] = self._check_claude_cli_available()
        
        # Check LLAMA availability
        llama_status = self.llama_mediator.get_status(probe=False)
        self.component_status["llama_available"] = bool(llama_status.get("helpers_enabled"))
        
        logger.info(f"Component status: {self.component_status}")

    async def _warm_llama_helpers(self) -> None:
        """Initialize optional Ollama helpers off the startup hot path."""
        try:
            llama_status = await asyncio.to_thread(self.llama_mediator.get_status, True)
            self.component_status["llama_available"] = bool(llama_status.get("helpers_enabled"))
            logger.info(
                "LLAMA helper warm-up finished: "
                f"helpers_enabled={self.component_status['llama_available']} "
                f"probe_attempted={llama_status.get('probe_attempted')}"
            )
        except Exception as e:
            logger.warning(f"LLAMA helper warm-up failed: {e}")
    
    def _log_startup_status(self):
        """Log detailed startup status"""
        status_lines = [
            "=== Telegram Coding Gateway Status ===",
            f"Claude Code CLI: {'[OK] Available' if self.component_status['claude_available'] else '[--] Not found'}",
            f"Ollama helpers: {'[OK] Available' if self.component_status['llama_available'] else '[--] Optional helper disabled'}",
            f"External task watcher: {'[OK] Running' if self.component_status['file_watcher_running'] else '[--] Stopped'}",
            f"Task Workers: {len(self.worker_tasks)} active",
            f"Watch Directory: {Path(config.system.tasks_dir).resolve()}",
            "===================================="
        ]
        
        for line in status_lines:
            logger.info(line)

    def _load_state(self) -> None:
        """Load pending state from logs/state.json"""
        try:
            if self._state_path.exists():
                import json
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                pending = data.get("pending_files", [])
                if isinstance(pending, list):
                    self._pending_files = set(map(str, pending))
        except Exception as e:
            logger.warning(f"event=state_load_failed error={e}")

    def _save_state(self) -> None:
        """Persist minimal pending state to logs/state.json"""
        try:
            import json
            payload = {
                "pending_files": sorted(self._pending_files),
                "updated": datetime.now().isoformat(),
            }
            self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"event=state_save_failed error={e}")

    def _update_artifact_index(self, task_id: str, artifact_path: Path) -> None:
        """Persist minimal index mapping task_id to latest artifact path."""
        try:
            import json
            idx = {}
            if self._artifact_index_path.exists():
                try:
                    idx = json.loads(self._artifact_index_path.read_text(encoding="utf-8"))
                except Exception:
                    idx = {}
            idx[str(task_id)] = str(artifact_path)
            self._artifact_index_path.parent.mkdir(parents=True, exist_ok=True)
            self._artifact_index_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"event=artifact_index_save_failed task_id={task_id} error={e}")

    def _check_claude_cli_available(self) -> bool:
        """Best-effort check that Claude CLI exists and is authenticated."""
        exe = shutil.which("claude") or "claude"
        try:
            result = subprocess.run(
                [exe, "auth", "status"],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=10,
                creationflags=_NO_WINDOW,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _make_task(
        self,
        description: str,
        task_type: Optional[str] = None,
        target_files: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        source: str = "runtime",
        extra_metadata: Optional[Dict] = None,
    ) -> Task:
        """Create an in-memory task object for direct queueing."""
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        parsed = self._parse_description_simple(description)

        if task_type:
            parsed["type"] = task_type
        if target_files:
            parsed["target_files"] = target_files

        explicit_cwd = (cwd or "").strip()
        resolved_cwd = ""
        if explicit_cwd:
            resolved = PathResolver.from_config().resolve_execution_path(explicit_cwd)
            resolved_cwd = resolved or ""

        task_type_enum = TaskType.ANALYZE
        raw_type = str(parsed.get("type", "analyze")).strip().lower()
        for candidate in TaskType:
            if candidate.value == raw_type:
                task_type_enum = candidate
                break

        metadata: Dict = {
            "session_id": session_id or "",
            "cwd": resolved_cwd,
            "source": source,
            "task_origin": "runtime",
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        task = Task(
            id=task_id,
            type=task_type_enum,
            priority=TaskPriority.MEDIUM,
            status=TaskStatus.PENDING,
            created=datetime.now().isoformat(),
            title=parsed.get("title", "Runtime task"),
            target_files=list(parsed.get("target_files", []) or []),
            prompt=parsed.get("prompt", description),
            success_criteria=["Task completed successfully", "Results validated"],
            context=f"Generated from {source}: {description}",
            metadata=metadata,
        )
        return task

    async def _enqueue_task(self, task: Task) -> str:
        """Queue a task object directly without writing a task file.

        This is the choke point every ingestion lane passes through
        (`submit_instruction` from Telegram/Web, the `.task.md` auto-pickup path,
        and internal runtime tasks). The task-harness Level-3 admission gate runs
        HERE — before any queue/telemetry side-effect — so an un-approved Level-3
        task is refused at admission on every lane, not just `.task.md`. The gate
        is flag-gated OFF by default (`HARNESS_LEVEL3_GUARD`), so default behavior
        is byte-identical: absent flag / absent field / level ≤ 2 ⇒ pass-through.
        """
        # [Harness] Admission control (spec docs/Task_harness_workflow.md §14).
        if not self._harness_level3_allows_autopickup(task):
            logger.warning(
                f"event=task_blocked reason=harness_level3_needs_approval "
                f"task_id={task.id} source={(task.metadata or {}).get('source', 'runtime')}"
            )
            self._emit_event(
                "task_blocked",
                task,
                {"task_id": task.id, "reason": "harness_level3_needs_approval"},
            )
            raise HarnessAdmissionBlocked(task.id)

        logger.info(f"event=task_created task_id={task.id} source={(task.metadata or {}).get('source', 'runtime')}")
        self._emit_event("task_created", task, {"source": (task.metadata or {}).get("source", "runtime")})
        self._emit_event("parsed", task)

        # [FlowRun A19] Best-effort dispatch-start record. This is a RECORD only —
        # nothing reads current_stage to drive behavior. Wrapped so a DB write
        # failure can NEVER fail or delay the real task (best-effort telemetry).
        flow_run_id = self._record_flow_run_start(task)
        self._emit_turn_telemetry(
            "turn.accepted",
            task,
            {
                "task_id": task.id,
                "source": (task.metadata or {}).get("source", "runtime"),
            },
        )

        try:
            self.task_queue.put_nowait(task)
            self.active_tasks[task.id] = task
            self._emit_turn_telemetry(
                "turn.queued",
                task,
                {"priority": getattr(task.priority, "value", str(task.priority))},
            )
            logger.info(f"Queued runtime task: {task.id} ({task.type.value}, {task.priority.value})")
            # [FlowRun A19/A22] Best-effort stage transition. When HARNESS_FLOW_DRIVE
            # is OFF this is A19's exact `queued` write (byte-identical). When ON the
            # task is now admitted/queued ⇒ the objective is locked in: write the
            # §11 `objective_lock` stage instead (SHADOW record; nothing reads it).
            if self._harness_flow_drive_enabled():
                self._record_flow_stage(flow_run_id, "objective_lock")
            else:
                self._record_flow_stage(flow_run_id, "queued")
        except asyncio.QueueFull:
            priority_val = getattr(task.priority, "value", str(task.priority))
            if priority_val == "low":
                logger.warning(f"event=dropped_low_priority task_id={task.id} reason=queue_full")
                self._emit_event("dropped_low_priority", task, {"reason": "queue_full"})
                raise RuntimeError("Task queue is full")
            logger.warning(f"event=throttled task_id={task.id} reason=queue_full priority={priority_val}")
            self._emit_event("throttled", task, {"reason": "queue_full", "priority": priority_val})
            try:
                await asyncio.wait_for(self.task_queue.put(task), timeout=5.0)
                self.active_tasks[task.id] = task
                self._emit_turn_telemetry(
                    "turn.queued",
                    task,
                    {"priority": priority_val},
                )
                logger.info(f"Queued throttled runtime task: {task.id} ({task.type.value}, {priority_val})")
            except asyncio.TimeoutError as exc:
                logger.error(f"event=dropped_after_throttle task_id={task.id}")
                self._emit_event("dropped_after_throttle", task, {"timeout": 5.0})
                raise RuntimeError("Task queue is full") from exc
        return task.id

    def _record_flow_run_start(self, task: Task) -> Optional[str]:
        """Best-effort FlowRun dispatch-start record (A19, v0.4 §13 item 1).

        Writes one flow_runs row at dispatch-start. This is a RECORD only — no
        code reads current_stage to drive behavior. Any failure is swallowed and
        logged: a telemetry write must never fail or delay a real task. Returns
        the flow_run_id on success, or None if the write was skipped/failed.

        The initial stage is `intent` (the first §11 stage) when HARNESS_FLOW_DRIVE
        is ON, and A19's legacy `dispatch_start` when it is OFF — so OFF behavior is
        byte-identical to A19. The generated flow_run_id is stashed on
        ``task.metadata[_FLOW_RUN_META_KEY]`` so later transition points on the
        worker loop can resolve it without new plumbing.
        """
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return None
            drive_on = self._harness_flow_drive_enabled()

            # OFF path — byte-identical to A19: exactly one dispatch-start RECORD
            # per task, no admission, no stash. Nothing reads current_stage.
            if not drive_on:
                return db.create_flow_run(task.id, "dispatch_start")

            # ---- Flag ON: A36 Case-admission policy -------------------------
            # The retired per-turn mint is replaced. A turn now: (A) BIRTHS a Case
            # iff it is a dispatched child (M2 lineage) or an explicit managed
            # root; (B) ATTACHES to the session's open Case; or (C) runs Case-less
            # (Pattern A: standalone session, many Tasks, no Case). Only (A)
            # creates a flow_run — so a reused session no longer shatters into one
            # fake Case per turn.
            # [M2] Dispatch lineage — RECORD only. A stamped child carries
            # parent_flow_run_id / dispatched_by / dispatch_file (see
            # _stamp_child_dispatch_lineage); persisted onto the child's row so
            # child→parent is recoverable via db.list_child_flow_runs.
            lineage = self._dispatch_lineage_fields(task)
            parent_fid = lineage.get("parent_flow_run_id")
            session_id = str((task.metadata or {}).get("session_id") or "").strip()
            managed = bool((task.metadata or {}).get(self._MANAGED_CASE_META_KEY))

            # (B) ATTACH — a non-birthing turn on a session that already owns an
            # open Case joins it as a Task: a per-turn `task` link + a
            # `task.attached` event, and NOTHING else (no new flow_run, no second
            # session link). The Case id is stashed under `_CASE_ID_META_KEY`
            # (NOT `_FLOW_RUN_META_KEY`) precisely so the per-turn stage/terminal
            # helpers do not fire on the shared Case and auto-close it.
            if not lineage and not managed and session_id:
                open_case_id = db.find_open_case_for_session(session_id)
                if open_case_id:
                    self._stash_task_meta(task, self._CASE_ID_META_KEY, open_case_id)
                    self._record_flow_link(
                        open_case_id, "task", task.id, "task", created_by="system",
                    )
                    self._record_flow_event(
                        open_case_id, "task.attached", "system",
                        entity_type="task", entity_id=task.id,
                    )
                    self._set_session_case_affiliation(session_id, open_case_id)
                    return None

            # (C) Standalone — no dispatch lineage, not managed, no open Case to
            # join ⇒ create nothing. Ordinary ad-hoc interaction needs no Case.
            if not lineage and not managed:
                return None

            # (A) BIRTH a Case (flow_run): a dispatched task (M2 lineage — a child
            # with parent_flow_run_id, or a watched-job dispatch carrying only
            # dispatched_by) or an explicit managed root. The A26/A29 authoritative
            # machinery below now runs ONLY on a genuine Case birth — never per
            # ordinary turn.
            objective = (task.metadata or {}).get(self._MANAGED_CASE_OBJECTIVE_KEY)
            criteria = (task.metadata or {}).get(self._MANAGED_CASE_CRITERIA_KEY)
            create_fields = dict(lineage)
            if criteria:
                create_fields["completion_criteria"] = criteria
            flow_run_id = db.create_flow_run(
                task.id, "intent", objective_lock=objective, **create_fields,
            )
            if flow_run_id:
                self._stash_task_meta(task, self._FLOW_RUN_META_KEY, flow_run_id)
                # [A26] flow.created event + root_task link at the moment of birth.
                self._record_flow_event(
                    flow_run_id, "flow.created", "system",
                    to_state="intent", entity_type="task", entity_id=task.id,
                )
                self._record_flow_link(
                    flow_run_id, "task", task.id, "root_task", created_by="system",
                )
                # [A29] The session running this Case is its WORKER session — an
                # AUTHORITATIVE relationship. Absent session_id ⇒ no link (oneoff).
                if session_id:
                    self._record_flow_link(
                        flow_run_id, "session", session_id, "worker",
                        created_by="system",
                    )
                    self._record_flow_event(
                        flow_run_id, "session.attached", "system",
                        entity_type="session", entity_id=session_id,
                        payload={"role": "worker"},
                    )
                    self._set_session_case_affiliation(
                        session_id, flow_run_id, role="worker",
                    )
                # Child-flow lineage: CONSUME the edge A26a stamped (do NOT add a
                # second stamping hook). flow_links(child_flow) on the PARENT is the
                # authoritative child→parent ledger; flow_runs.parent_flow_run_id
                # (already written above) stays a convenience index.
                if parent_fid:
                    self._record_flow_link(
                        parent_fid, "flow", flow_run_id, "child_flow",
                        created_by=lineage.get("dispatched_by"),
                    )
                    self._record_flow_event(
                        parent_fid, "task.dispatched", "system",
                        entity_type="flow", entity_id=flow_run_id,
                        payload={
                            "dispatched_by": lineage.get("dispatched_by"),
                            "dispatch_file": lineage.get("dispatch_file"),
                            "child_task_id": task.id,
                        },
                    )
            return flow_run_id
        except Exception as e:
            logger.warning("event=flow_run_start_failed task_id=%s err=%s", task.id, e)
            return None

    def _stash_task_meta(self, task: "Task", key: str, value: str) -> None:
        """Best-effort stash of a value on ``task.metadata[key]``. Never raises."""
        try:
            if getattr(task, "metadata", None) is None:
                task.metadata = {}
            task.metadata[key] = value
        except Exception:
            pass

    def _set_session_case_affiliation(
        self,
        session_id: str,
        case_id: str,
        role: Optional[str] = None,
    ) -> None:
        """[A36] Persist a session's DURABLE Case affiliation (best-effort).

        Writes ``current_case_id`` + ``case_role`` on the Session so membership
        survives across turns, replacing the per-read most-recent-link derive.
        Isolated and idempotent: a no-op when the value is already current (so a
        long-lived attachment writes ONCE, not per turn), and any failure logs and
        returns — a session write must never fail or delay admission. When ``role``
        is None it is resolved from the authoritative session→case link, defaulting
        to 'worker'. Cleared on Case close (A37).
        """
        try:
            sid = (session_id or "").strip()
            if not sid or not case_id:
                return
            store = getattr(self, "session_store", None)
            if store is None:
                return
            session = store.get(sid)
            if session is None:
                return
            # Steady-state fast path: already affiliated to this Case and no
            # explicit role override ⇒ nothing to change. Return BEFORE resolving
            # the role, so a long-lived attachment costs zero extra DB reads per
            # turn (the role lookup only runs on a genuine first attach / switch).
            already_here = getattr(session, "current_case_id", None) == case_id
            if role is None:
                if already_here:
                    return
                role = self._resolve_session_case_role(case_id, sid)
            if already_here and getattr(session, "case_role", None) == role:
                return  # steady-state: no redundant write
            session.current_case_id = case_id
            session.case_role = role
            store.save(session)
        except Exception as e:
            logger.warning(
                "event=session_case_affiliation_failed session_id=%s err=%s",
                session_id, e,
            )

    def _resolve_session_case_role(self, case_id: str, session_id: str) -> str:
        """Role a session holds in a Case, read from the authoritative link.

        Defaults to 'worker' when no explicit link role is found. Never raises.
        """
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return "worker"
            links = db.list_flow_links(
                flow_run_id=case_id, entity_type="session", entity_id=session_id,
            )
            if links:
                return str(links[0].get("role") or "worker")
        except Exception:
            pass
        return "worker"

    def open_case(
        self,
        objective: str,
        session_id: str,
        role: str = "manager",
        completion_criteria: Optional[str] = None,
    ) -> Optional[str]:
        """[A36] Orchestrator seam over ``db.open_case`` — the sanctioned Case birth.

        Creates a managed Case and durably affiliates the session to it. This is
        the entrypoint the Manager role (M3.1) drives; it is NOT called inside the
        per-turn enqueue path (that is admission's job). Best-effort/isolated:
        returns the new flow_run_id, or None if the DB is unavailable / the write
        failed (a Case-birth failure must never crash the caller).
        """
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return None
            flow_run_id = db.open_case(
                objective, session_id, role=role,
                completion_criteria=completion_criteria,
            )
            self._set_session_case_affiliation(session_id, flow_run_id, role=role)
            return flow_run_id
        except Exception as e:
            logger.warning(
                "event=open_case_failed session_id=%s err=%s", session_id, e,
            )
            return None

    def close_case(
        self,
        flow_run_id: str,
        *,
        outcome: str = "closed",
        actor: str = "operator",
        criteria_reconciliation: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """[A37] Orchestrator seam over ``db.close_case`` — authoritative closure.

        Returns ``{"ok", "closed", "reason"}``: ``ok`` False with a human ``reason``
        when the Case cannot honestly close (unresolved approval / open child work /
        unmet completion_criteria) or the id is unknown — a structured refusal, not
        an exception. On a real close, clears the durable Case affiliation of every
        session linked to the Case (A36 item 4), best-effort/isolated.
        """
        from src.control.db import get_db, CaseCloseBlocked
        db = get_db()
        if db is None:
            return {"ok": False, "closed": False, "reason": "db_unavailable"}
        try:
            closed = db.close_case(
                flow_run_id, outcome=outcome, actor=actor,
                criteria_reconciliation=criteria_reconciliation,
            )
        except CaseCloseBlocked as e:
            return {"ok": False, "closed": False, "reason": e.reason}
        except ValueError as e:
            return {"ok": False, "closed": False, "reason": str(e)}
        if closed:
            try:
                for link in db.list_flow_links(
                    flow_run_id=flow_run_id, entity_type="session",
                ):
                    self._clear_session_case_affiliation(
                        str(link.get("entity_id") or ""), flow_run_id,
                    )
            except Exception as e:
                logger.warning(
                    "event=case_affiliation_clear_failed flow_run_id=%s err=%s",
                    flow_run_id, e,
                )
        return {"ok": True, "closed": bool(closed), "reason": None}

    def _clear_session_case_affiliation(self, session_id: str, case_id: str) -> None:
        """[A37] Clear a session's durable Case affiliation on Case close.

        Only clears when the session still points at THIS Case — a session that has
        already moved to another Case is left untouched. Best-effort; never raises.
        """
        try:
            sid = (session_id or "").strip()
            if not sid:
                return
            store = getattr(self, "session_store", None)
            if store is None:
                return
            session = store.get(sid)
            if session is None:
                return
            if getattr(session, "current_case_id", None) != case_id:
                return  # already moved on / not affiliated — leave it
            session.current_case_id = None
            session.case_role = None
            store.save(session)
        except Exception as e:
            logger.warning(
                "event=session_case_clear_failed session_id=%s err=%s",
                session_id, e,
            )

    def _record_flow_stage(self, flow_run_id: Optional[str], stage: str) -> None:
        """Best-effort FlowRun stage-transition update (A19). Swallows failures.

        SHADOW ONLY: this WRITES current_stage; nothing reads it to decide what
        runs. Wrapped so a write failure logs and returns — it can never raise
        into task execution.
        """
        if not flow_run_id:
            return
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return
            db.update_flow_stage(flow_run_id, stage)
        except Exception as e:
            logger.warning("event=flow_run_stage_failed flow_run_id=%s err=%s", flow_run_id, e)

    def _record_flow_link(
        self,
        flow_run_id: Optional[str],
        entity_type: str,
        entity_id: str,
        role: str,
        created_by: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """[A26] Best-effort authoritative case↔entity link. Swallows failures.

        SHADOW/RECORD ONLY — a relationship row, never read to drive execution.
        Idempotent at the DB layer (unique-keyed). Wrapped so any failure logs and
        returns; a link write can NEVER raise into task execution.
        """
        if not flow_run_id:
            return
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return
            db.create_flow_link(
                flow_run_id, entity_type, entity_id, role,
                created_by=created_by, metadata=metadata,
            )
        except Exception as e:
            logger.warning(
                "event=flow_link_failed flow_run_id=%s role=%s err=%s",
                flow_run_id, role, e,
            )

    def _record_flow_event(
        self,
        flow_run_id: Optional[str],
        event_type: str,
        actor: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """[A26] Best-effort append-only case lifecycle event. Swallows failures.

        SHADOW/RECORD ONLY — audit trail, never read to drive execution. Wrapped
        so any failure logs and returns; an event write can NEVER raise into task
        execution.
        """
        if not flow_run_id:
            return
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return
            db.append_flow_event(
                flow_run_id, event_type, actor,
                from_state=from_state, to_state=to_state,
                entity_type=entity_type, entity_id=entity_id, payload=payload,
            )
        except Exception as e:
            logger.warning(
                "event=flow_event_failed flow_run_id=%s type=%s err=%s",
                flow_run_id, event_type, e,
            )

    # [FlowRun A22] Metadata key under which the flow_run_id is stashed on a task
    # so the worker-loop transition points (execution / impl_review / closure)
    # can resolve it. Underscored so it never collides with a user metadata field.
    _FLOW_RUN_META_KEY = "__flow_run_id"

    # [A36] Metadata keys for Case admission. `_CASE_ID_META_KEY` stashes the
    # SHARED Case a turn ATTACHED to — deliberately DISTINCT from
    # `_FLOW_RUN_META_KEY` so the per-turn stage/terminal helpers (which key off
    # `_FLOW_RUN_META_KEY`) never fire on the shared Case and auto-close it. The
    # `_MANAGED_CASE_*` keys mark a task as an explicit managed-Case root (the
    # producer is the Manager role / open_case dispatch at M3.1); when present,
    # `_record_flow_run_start` BIRTHS a Case for the task rather than attaching.
    _CASE_ID_META_KEY = "__case_id"
    _MANAGED_CASE_META_KEY = "__managed_case"
    _MANAGED_CASE_OBJECTIVE_KEY = "__managed_case_objective"
    _MANAGED_CASE_CRITERIA_KEY = "__managed_case_criteria"

    # [M2] Dispatch-lineage metadata keys. When a parent flow/task dispatches a
    # child task, these are stamped onto the CHILD task's metadata (flag-guarded,
    # by _stamp_child_dispatch_lineage) so _record_flow_run_start can persist them
    # onto the child's flow_runs row. Underscored so they never collide with a
    # user metadata field. RECORD only — nothing reads them to drive execution.
    _PARENT_FLOW_RUN_META_KEY = "__parent_flow_run_id"
    _DISPATCHED_BY_META_KEY = "__dispatched_by"
    _DISPATCH_FILE_META_KEY = "__dispatch_file"

    def _dispatch_lineage_fields(self, task: "Task") -> Dict[str, str]:
        """[M2] Extract the flow_runs lineage columns from a child task's metadata.

        Reads the lineage keys a spawn site stamped on the child (via
        _stamp_child_dispatch_lineage) and maps them to create_flow_run kwargs
        (parent_flow_run_id / dispatched_by / dispatch_file). Only present keys
        are returned, so an unstamped task yields ``{}`` (⇒ NULL columns). Pure
        read of metadata; never raises (returns ``{}`` on any error).
        """
        try:
            meta = getattr(task, "metadata", None) or {}
            fields: Dict[str, str] = {}
            parent = meta.get(self._PARENT_FLOW_RUN_META_KEY)
            if parent:
                fields["parent_flow_run_id"] = parent
            dispatched_by = meta.get(self._DISPATCHED_BY_META_KEY)
            if dispatched_by:
                fields["dispatched_by"] = dispatched_by
            dispatch_file = meta.get(self._DISPATCH_FILE_META_KEY)
            if dispatch_file:
                fields["dispatch_file"] = dispatch_file
            return fields
        except Exception:
            return {}

    def _stamp_child_dispatch_lineage(
        self,
        child_task: "Task",
        parent_task: Optional["Task"] = None,
        *,
        parent_flow_run_id: Optional[str] = None,
        dispatched_by: Optional[str] = None,
        dispatch_file: Optional[str] = None,
    ) -> None:
        """[M2] Stamp dispatch-lineage onto a CHILD task before it is enqueued.

        Called at the seam where one flow/task dispatches another. The parent's
        own flow_run_id is stashed on ``parent_task.metadata[_FLOW_RUN_META_KEY]``
        (set by _record_flow_run_start); this copies it — plus dispatched_by and
        dispatch_file — onto the child so its flow_runs row records the link.

        [A32] A caller that only has the parent's *loose* flow_run_id (e.g. the
        HTTP ``/api/instructions`` path, where a Manager session passes its own
        case id but there is no in-process parent ``Task`` object) may pass
        ``parent_flow_run_id`` directly. An explicit value takes precedence over
        one derived from ``parent_task``; either seam records the same edge.

        Flag-guarded: when HARNESS_FLOW_DRIVE is OFF this is a NO-OP — the child's
        metadata is left untouched ⇒ byte-identical to today. SHADOW/best-effort:
        wrapped so any failure logs and returns; it can NEVER raise into the
        dispatch path. Nothing reads the stamped keys to drive execution.
        """
        try:
            if not self._harness_flow_drive_enabled():
                return
            # Explicit loose id (A32 HTTP seam) wins; else derive from parent_task.
            if parent_task is not None:
                pmeta = getattr(parent_task, "metadata", None) or {}
                if not parent_flow_run_id:
                    parent_flow_run_id = pmeta.get(self._FLOW_RUN_META_KEY)
                if dispatched_by is None:
                    dispatched_by = getattr(parent_task, "id", None)
            if not (parent_flow_run_id or dispatched_by or dispatch_file):
                return
            if child_task.metadata is None:
                child_task.metadata = {}
            if parent_flow_run_id:
                child_task.metadata[self._PARENT_FLOW_RUN_META_KEY] = parent_flow_run_id
            if dispatched_by:
                child_task.metadata[self._DISPATCHED_BY_META_KEY] = dispatched_by
            if dispatch_file:
                child_task.metadata[self._DISPATCH_FILE_META_KEY] = dispatch_file
        except Exception as e:
            logger.warning(
                "event=dispatch_lineage_stamp_failed child_task_id=%s err=%s",
                getattr(child_task, "id", "?"), e,
            )

    @staticmethod
    def _harness_flow_drive_enabled() -> bool:
        """Whether authoritative stage transitions are written (A22).

        Opt-in via ``HARNESS_FLOW_DRIVE`` (truthy: 1/true/yes/on); default OFF.
        When OFF, flow-stage behavior is byte-identical to A19 (legacy
        `dispatch_start`/`queued` record only). When ON, the §11 vocabulary
        (FLOW_STAGES) is written at each harness transition — a SHADOW record:
        NO code path reads current_stage to decide what runs.
        """
        flag = os.environ.get("HARNESS_FLOW_DRIVE", "").strip().lower()
        return flag in ("1", "true", "yes", "on")

    def _flow_stage_transition(self, task: "Task", stage: str) -> None:
        """[A22] Single flag-guarded, best-effort stage-transition helper.

        Called at each harness transition on the loop/driver surface. When
        HARNESS_FLOW_DRIVE is OFF this is a no-op (⇒ byte-identical to A19). When
        ON it resolves the flow_run_id stashed on the task and writes the given
        FLOW_STAGES vocabulary stage (update_flow_stage also stamps updated_at).

        SHADOW ONLY — this only ever WRITES current_stage. It is wrapped so any
        failure logs and returns; it can NEVER raise into task execution.
        """
        try:
            if not self._harness_flow_drive_enabled():
                return
            meta = getattr(task, "metadata", None) or {}
            flow_run_id = meta.get(self._FLOW_RUN_META_KEY)
            if not flow_run_id:
                return
            self._record_flow_stage(flow_run_id, stage)
            # [A26] Mirror the transition into the append-only case audit trail.
            # current_stage stays the mutable summary; flow_events is the trail.
            self._record_flow_event(
                flow_run_id, "flow.stage_changed", "system", to_state=stage,
            )
        except Exception as e:
            logger.warning(
                "event=flow_stage_transition_failed task_id=%s stage=%s err=%s",
                getattr(task, "id", "?"), stage, e,
            )

    def _flow_terminal_outcome(
        self, task: "Task", *, success: bool, error_class: str = "",
    ) -> None:
        """[A37] Record a task's terminal outcome as a `task.finished` case event.

        **Task-only** (the A37 correction of A29): a task ending updates TASK state
        only — it does NOT write ``flow_runs.status``. ``Task finished != Case
        completed``: a completed or failed task leaves its Case OPEN; a Case's
        status changes solely via an authoritative ``close_case`` (or a real
        reviewer at M3.2), never as a task-end side effect.

        Emits one append-only ``task.finished`` event (compact outcome reference)
        onto the task's owning Case — resolving the Case id from either the birth
        key (``_FLOW_RUN_META_KEY``, a dispatched/managed root) or the attach key
        (``_CASE_ID_META_KEY``, an ordinary turn on a shared Case) so both the
        first and Nth turn of a Case leave an honest audit trail. Flag-guarded
        (no-op when OFF ⇒ byte-identical) and best-effort/isolated — a write
        failure logs and returns; it can NEVER raise into task execution.
        """
        try:
            if not self._harness_flow_drive_enabled():
                return
            meta = getattr(task, "metadata", None) or {}
            # Birth case (owns a flow_run) OR the shared Case an ordinary turn
            # attached to — either way the task ran under this Case.
            flow_run_id = meta.get(self._FLOW_RUN_META_KEY) or meta.get(self._CASE_ID_META_KEY)
            if not flow_run_id:
                return
            self._record_flow_event(
                flow_run_id, "task.finished", "system",
                entity_type="task", entity_id=getattr(task, "id", None),
                payload={
                    "outcome": "success" if success else "failed",
                    "error_class": (error_class or None) if not success else None,
                },
            )
        except Exception as e:
            logger.warning(
                "event=flow_terminal_outcome_failed task_id=%s err=%s",
                getattr(task, "id", "?"), e,
            )

    async def submit_instruction(
        self,
        description: str,
        task_type: Optional[str] = None,
        target_files: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        source: str = "telegram",
        extra_metadata: Optional[Dict] = None,
        parent_task: Optional["Task"] = None,
        parent_flow_run_id: Optional[str] = None,
        dispatched_by: Optional[str] = None,
        dispatch_file: Optional[str] = None,
    ) -> str:
        """Direct runtime entrypoint for Telegram/CLI instructions.

        [M2] When this call is a child dispatch (a parent flow/task spawning
        another), pass ``parent_task`` (whose metadata carries the parent
        flow_run_id) and/or ``dispatched_by`` / ``dispatch_file``. These are
        stamped onto the child task's flow_runs row for lineage — but ONLY when
        HARNESS_FLOW_DRIVE is ON; otherwise stamping is a no-op ⇒ byte-identical.

        [A32] When the caller only has the parent's *loose* flow_run_id (the HTTP
        ``/api/instructions`` seam — a Manager session dispatching a worker via
        ``mcp_manager``), pass ``parent_flow_run_id`` directly; it is stamped onto
        the child's flow_runs row exactly like the ``parent_task``-derived edge.
        """
        task = self._make_task(
            description=description,
            task_type=task_type,
            target_files=target_files,
            session_id=session_id,
            cwd=cwd,
            source=source,
            extra_metadata=extra_metadata,
        )
        # [M2/A32] Stamp dispatch lineage before enqueue (flag-guarded no-op when OFF).
        if parent_task is not None or parent_flow_run_id or dispatched_by or dispatch_file:
            self._stamp_child_dispatch_lineage(
                task,
                parent_task,
                parent_flow_run_id=parent_flow_run_id,
                dispatched_by=dispatched_by,
                dispatch_file=dispatch_file,
            )
        return await self._enqueue_task(task)

    async def compact_session(self, session_id: str):
        """Send /compact to the backend for the given session, collapsing context."""
        from src.core.interfaces import ExecutionResult
        session = self.session_store.get(session_id)
        if not session:
            return ExecutionResult(success=False, output="", errors=["Session not found"])
        if not session.backend_session_id:
            return ExecutionResult(success=False, output="", errors=["Session has no backend context yet"])
        backend = self._backends.get(session.backend)
        if not backend:
            return ExecutionResult(success=False, output="", errors=[f"Unknown backend: {session.backend}"])

        # If the session is pinned to a remote mesh node, dispatch there — running
        # the backend locally would use the wrong cwd (the Pi doesn't have the
        # Windows/remote path that session.repo_path points to).
        if config.mesh.enabled and session.machine_id and session.machine_id != socket.gethostname():
            compact_task = Task(
                id=f"compact-{session_id[:8]}-{int(time.time())}",
                type=TaskType.ANALYZE,
                priority=TaskPriority.HIGH,
                status=TaskStatus.PENDING,
                created=datetime.now().isoformat(),
                title="Compact session context",
                target_files=[],
                prompt="/compact",
                success_criteria=["Context compacted"],
                context="",
                metadata={"session_id": session_id, "source": "compact", "task_origin": "runtime"},
            )
            backend_name = session.backend or "claude"
            self._mesh_enqueue_task(compact_task, backend_name)
            result = await self._process_task_remote(compact_task, session, time.time(), config.system.task_timeout)
            from src.core.interfaces import ExecutionResult as ER
            return ER(
                success=result.success,
                output=result.output,
                errors=result.errors or [],
            )

        return await asyncio.to_thread(backend.compact_session, session)

    def load_compact_context(self, task_id: str) -> Dict[str, Any]:
        """Load compact, prompt-ready context for a given task_id.

        Delegates to a lightweight internal loader that reads the latest
        artifact via `results/index.json` with a scan fallback. Keeps output
        under small token/char caps.
        """
        if self._context_loader is None:
            from src.control.db import get_db
            self._context_loader = _ContextLoader(self._artifact_index_path, Path(config.system.results_dir), get_db)
        return self._context_loader.load(task_id)

    # Hard cap on the assembled prior-context prefix, independent of the loader's
    # own per-field caps. Keeps the injected reference block from dominating the
    # prompt even if a future loader returns larger fields.
    _COMPACT_PREFIX_MAX_CHARS = 4000
    _COMPACT_MAX_FILES = 20

    async def _maybe_inject_compact_context(self, task: "Task") -> None:
        """Opt-in: prepend bounded prior context when a task declares `continues:`.

        No-op (prompt untouched, no loader call) unless `task.metadata["continues"]`
        is a non-empty task id. Injects at most once per task id. Any failure is
        swallowed and the original prompt is left intact — a continuation must never
        crash a turn. The prior context is fenced as reference-only; the original
        instruction is preserved verbatim inside `<current_instruction>`.
        """
        try:
            meta = task.metadata or {}
            raw = meta.get("continues", "")
            # [F6] Coerce/validate cheaply; reject non-str (e.g. a YAML list) and blanks.
            if not isinstance(raw, str):
                if raw:
                    logger.info(f"event=compact_context_skipped reason=continues_not_string task_id={task.id}")
                return
            parent_id = raw.strip()
            if not parent_id:
                return
            # [F5/R1] Instance-local guard — inject once, never via task.metadata.
            if task.id in self._compact_injected_ids:
                return
            # [F4] Self-reference is meaningless.
            if parent_id == task.id:
                logger.info(f"event=compact_context_skipped reason=self_reference task_id={task.id}")
                return

            # [F2] Loader is sync + does DB/file IO; keep it off the event loop.
            ctx = await asyncio.to_thread(self.load_compact_context, parent_id)

            # [F4] Nothing usable to inject.
            summary = (ctx.get("summary") or "").strip()
            files = [f for f in (ctx.get("files_modified") or []) if f]
            if ctx.get("source") == "none" or (not summary and not files):
                logger.info(f"event=compact_context_skipped reason=no_prior_context task_id={task.id} parent={parent_id}")
                return

            prefix = self._build_compact_prefix(parent_id, summary, files, ctx.get("errors") or [])
            if not prefix:
                return

            original = task.prompt or ""
            task.prompt = f"{prefix}\n\n<current_instruction>\n{original}\n</current_instruction>"
            self._compact_injected_ids.add(task.id)
            logger.info(
                f"event=compact_context_injected task_id={task.id} parent={parent_id} "
                f"prefix_chars={len(prefix)} files={len(files)}"
            )
        except Exception as e:
            # [F4] Never raise into process_task; proceed with the original prompt.
            logger.warning(f"event=compact_context_error task_id={getattr(task, 'id', '?')} error={e}")

    @staticmethod
    def _defuse_fence(text: str) -> str:
        """Neutralize fence tokens inside interpolated prior-task content.

        Prior summary/files/errors are a *prior task's stored output* and are not
        trusted structure. If they contained a literal `</prior_context>` or a
        `<current_instruction>` marker, they could break out of the reference fence
        and be read as a live instruction. Strip the angle brackets on those tokens
        so the fence can't be escaped.
        """
        for tok in ("</prior_context>", "<prior_context", "<current_instruction>", "</current_instruction>"):
            text = text.replace(tok, tok.replace("<", "(").replace(">", ")"))
        return text

    def _build_compact_prefix(self, parent_id: str, summary: str, files: list, errors: list) -> str:
        """Assemble the bounded, fenced prior-context block (reference only)."""
        lines = [f'<prior_context source="task {self._defuse_fence(str(parent_id))}">']
        if summary:
            lines.append(f"summary: {self._defuse_fence(summary)}")
        if files:
            shown = [self._defuse_fence(str(f)) for f in files[: self._COMPACT_MAX_FILES]]
            more = "" if len(files) <= self._COMPACT_MAX_FILES else f" (+{len(files) - self._COMPACT_MAX_FILES} more)"
            lines.append("files_modified: " + ", ".join(shown) + more)
        if errors:
            first_err = self._defuse_fence(str(errors[0]))
            lines.append(f"prior_errors: {first_err}")
        lines.append("(Reference only. Your actual instruction follows.)")
        lines.append("</prior_context>")
        block = "\n".join(lines)
        # [F3] Hard total cap regardless of field caps.
        if len(block) > self._COMPACT_PREFIX_MAX_CHARS:
            block = block[: self._COMPACT_PREFIX_MAX_CHARS - len("\n…(truncated)\n</prior_context>")]
            block = block.rstrip() + "\n…(truncated)\n</prior_context>"
        return block

    async def _handle_new_task_file(self, file_path: str):
        """Handle detection of a new `.task.md` file.

        Debounces duplicates, validates format, parses into `Task`, emits events,
        and enqueues for processing.
        """
        try:
            # Normalize to absolute path so relative vs absolute variants
            # of the same file don't bypass the inflight dedup check.
            path_key = str(Path(file_path).resolve())
            if path_key in self._inflight_paths:
                logger.info(f"event=task_skipped reason=already_inflight file={file_path}")
                return
            self._inflight_paths.add(path_key)
            # Track as pending for persistence
            self._pending_files.add(path_key)
            self._save_state()

            logger.info(f"event=task_received file={file_path}")
            self._emit_event("task_received", None, {"file": file_path})
            
            # Validate task file format
            errors = self.task_parser.validate_task_format(file_path)
            if errors:
                logger.error(f"Invalid task file format: {errors}")
                # Remove from pending if file is gone or invalid
                try:
                    self._pending_files.discard(path_key)
                    self._save_state()
                except Exception:
                    pass
                # Release lock on invalid format to allow future corrections
                try:
                    self._inflight_paths.discard(path_key)
                except Exception:
                    pass
                return
            
            # Parse task
            task = self.task_parser.parse_task_file(file_path)
            task.status = TaskStatus.PENDING
            # Track source file path for post-processing archival
            try:
                if getattr(task, "metadata", None) is None:
                    task.metadata = {}
                task.metadata["__file_path"] = file_path
                task.metadata.setdefault("source", "task_file")
                task.metadata.setdefault("task_origin", "file")
            except Exception:
                pass
            logger.info(f"event=parsed task_id={task.id} type={task.type.value} priority={task.priority.value}")

            # Admission (incl. the Level-3 harness gate) now lives in
            # `_enqueue_task` — the choke point shared by every ingestion lane. A
            # blocked Level-3 `.task.md` raises HarnessAdmissionBlocked there; here
            # we just release this lane's file-tracking state so an `approved: true`
            # re-write can be picked up later. The file is left un-enqueued.
            try:
                await self._enqueue_task(task)
            except HarnessAdmissionBlocked:
                try:
                    self._pending_files.discard(path_key)
                    self._inflight_paths.discard(path_key)
                    self._save_state()
                except Exception:
                    pass
                return

        except Exception as e:
            logger.error(f"Error processing task file {file_path}: {e}")
            # Best-effort release of lock on exception
            try:
                self._inflight_paths.discard(str(file_path))
                # Also drop from pending and persist to avoid stuck entries
                self._pending_files.discard(str(file_path))
                self._save_state()
            except Exception:
                pass

    @staticmethod
    def _harness_level3_allows_autopickup(task: "Task") -> bool:
        """Task-harness Level-3 admission predicate (spec §14).

        The single decision function behind the admission gate in `_enqueue_task`
        (every ingestion lane) and the `.task.md` file lane. Pure over
        `task.metadata`, so it is trivially testable.

        Returns True (allow) in every case EXCEPT: the guard flag is enabled AND
        the task declares `harness_level: 3` AND it is not `approved: true`.
        Level ≤ 2 and any task without a `harness_level` field are always allowed
        — behavior is byte-identical to before when the field is absent or the
        flag is unset.

        The guard is opt-in via `HARNESS_LEVEL3_GUARD` (truthy: 1/true/yes/on).
        The convention (a documented rule the dispatch prompt obeys) is the primary
        control; this is the enforcement backstop for when a drafter ignores it.
        """
        flag = os.environ.get("HARNESS_LEVEL3_GUARD", "").strip().lower()
        if flag not in ("1", "true", "yes", "on"):
            return True  # guard off ⇒ legacy behavior

        meta = getattr(task, "metadata", None) or {}
        raw_level = meta.get("harness_level", None)
        if raw_level is None:
            return True  # field absent ⇒ unchanged

        # Coerce level defensively (YAML may give int or str); only "3" gates.
        try:
            level = int(str(raw_level).strip())
        except (TypeError, ValueError):
            return True  # unparseable level ⇒ don't invent a block
        if level != 3:
            return True  # Level ≤ 2 auto-enqueues

        approved = meta.get("approved", False)
        if isinstance(approved, str):
            approved = approved.strip().lower() in ("1", "true", "yes", "on")
        return bool(approved)

    async def _task_worker(self, worker_name: str):
        """Worker coroutine that processes tasks from the queue.

        Each worker pulls tasks, calls `process_task`, and persists artifacts.
        """
        logger.info(f"Task worker {worker_name} started")
        
        while self.running:
            try:
                # Get task from queue with timeout
                task = await asyncio.wait_for(
                    self.task_queue.get(), 
                    timeout=1.0
                )
                
                # Ensure cancel event exists for this task
                cancel_ev = self._task_cancel_events.get(task.id)
                if cancel_ev is None:
                    cancel_ev = asyncio.Event()
                    self._task_cancel_events[task.id] = cancel_ev

                # If cancellation was requested before start, mark and skip
                if cancel_ev.is_set():
                    task.status = TaskStatus.FAILED
                    logger.info(f"event=cancelled_before_start worker={worker_name} task_id={task.id}")
                    self._emit_event("cancelled", task, {"worker": worker_name, "when": "before_start"})
                    self._emit_turn_telemetry(
                        "turn.cancel_requested",
                        task,
                        {"reason_code": "cancelled_before_start"},
                    )
                    self._emit_turn_telemetry(
                        "turn.completed",
                        task,
                        {
                            "status": "cancelled",
                            "timeout_status": "none",
                            "exit_code": None,
                        },
                        flush=True,
                    )
                    self.task_queue.task_done()
                    # Release inflight locks and pending state, similar to completion path
                    try:
                        if getattr(task, "metadata", None):
                            self._inflight_paths.discard(task.metadata.get("__file_path", ""))
                            self._pending_files.discard(task.metadata.get("__file_path", ""))
                            self._save_state()
                    except Exception:
                        pass
                    continue

                backend_name = self._resolve_task_backend(task)
                start_event = self._backend_event_name(backend_name, "started")
                logger.info(f"event={start_event} worker={worker_name} task_id={task.id}")
                self._emit_event(start_event, task, {"worker": worker_name, "backend": backend_name})
                self._emit_turn_telemetry("turn.started", task, backend=backend_name)
                self._mesh_enqueue_task(task, backend_name)

                # [FlowRun A22] Harness transition → `execution`. Flag-guarded,
                # best-effort SHADOW write (no-op when HARNESS_FLOW_DRIVE is OFF);
                # nothing below reads current_stage to decide what runs.
                self._flow_stage_transition(task, "execution")

                # Process the task
                result = await self.process_task(task)

                # Detached: the gateway is shutting down while a remote worker
                # keeps running and owns this task's real state in the DB. This
                # is NOT a failure — do not notify Telegram, do not write a
                # terminal artifact, do not mark the task FAILED. Leave the DB
                # row as 'claimed' so startup reattach reports the worker's real
                # result. Just release in-process bookkeeping and move on.
                if getattr(result, "detached", False):
                    logger.info(
                        "event=task_detached worker=%s task_id=%s reason=gateway_shutdown",
                        worker_name, task.id,
                    )
                    # Release in-process bookkeeping but DO NOT touch the DB row,
                    # Telegram, or session status — the remote worker owns this
                    # task and startup reattach will report its real result.
                    try:
                        self._running_exec_tasks.pop(task.id, None)
                        self.active_tasks.pop(task.id, None)
                        if getattr(task, "metadata", None):
                            self._inflight_paths.discard(task.metadata.get("__file_path", ""))
                    except Exception:
                        pass
                    self.task_queue.task_done()
                    continue

                # Store result
                self.task_results[task.id] = result

                # Update task status
                task.status = TaskStatus.COMPLETED if result.success else TaskStatus.FAILED

                # Log completion
                status = "SUCCESS" if result.success else "FAILED"
                finish_backend = getattr(result, "backend_name", backend_name)
                finish_event = self._backend_event_name(finish_backend, "finished")
                logger.info(f"event={finish_event} task_id={task.id} status={status} duration_s={result.execution_time:.2f} class={getattr(result,'error_class','')}")
                self._emit_event(
                    finish_event,
                    task,
                    {"status": status, "duration_s": result.execution_time, "error_class": getattr(result, "error_class", ""), "backend": finish_backend},
                )
                final_status = (
                    "success"
                    if result.success
                    else "timed_out"
                    if getattr(result, "error_class", "") == "timeout"
                    else "cancelled"
                    if any("cancelled" in str(error).lower() for error in (result.errors or []))
                    else "failed"
                )
                self._emit_turn_telemetry(
                    "turn.result_recorded",
                    task,
                    {
                        "status": final_status,
                        "error_code": getattr(result, "error_class", "") or None,
                    },
                    invocation_id=getattr(result, "telemetry_invocation_id", None),
                    backend=finish_backend,
                )
                # [A37] No auto `impl_review`/`closure` stage stamp. Those stages
                # were fabricated on EVERY task even though no reviewer/closer ran
                # (Task finished != Case completed). A task-end writes ONLY the task
                # outcome now; a Case's stage/status changes only via a real
                # reviewer (M3.2) or an authoritative close_case.
                self._emit_turn_telemetry(
                    "turn.completed",
                    task,
                    {
                        "status": final_status,
                        "timeout_status": (
                            "gateway_timeout" if final_status == "timed_out" else "none"
                        ),
                        "exit_code": getattr(result, "return_code", None),
                    },
                    invocation_id=getattr(result, "telemetry_invocation_id", None),
                    backend=finish_backend,
                    flush=True,
                )
                # [A37] Terminal OUTCOME — task-only. Records the task's result as a
                # `task.finished` case audit event WITHOUT touching flow_runs.status.
                # A completed/failed task leaves its Case OPEN; closure is a separate
                # authoritative decision (close_case), never a task-end side effect.
                self._flow_terminal_outcome(
                    task,
                    success=bool(result.success),
                    error_class=str(getattr(result, "error_class", "") or ""),
                )

                # Send notification via the central notification dispatcher
                try:
                    session_id_for_notify = (task.metadata or {}).get("session_id", "").strip()
                    notify_chat_id: Optional[int] = None
                    if session_id_for_notify:
                        _s = self.session_store.get(session_id_for_notify)
                        if _s:
                            notify_chat_id = _s.telegram_chat_id

                    await self.notifier.notify_task_outcome(
                        task.id,
                        result,
                        session=self.session_store.get(session_id_for_notify) if session_id_for_notify else None,
                        chat_id=notify_chat_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to send completion notification: {e}")
                
                # Write artifacts
                artifact_path: Optional[str] = None
                try:
                    self._write_artifacts(task.id, result, task=task)
                    artifact_path = str(Path(config.system.results_dir) / f"{task.id}.json")
                    logger.info(f"event=artifacts_written task_id={task.id}")
                    self._emit_event("artifacts_written", task)
                except Exception as e:
                    logger.error(f"event=artifacts_error task_id={task.id} error={e}")
                    self._emit_event("artifacts_error", task, {"error": str(e)})
                self._mesh_complete_task(task, result, artifact_path)

                # Update session record + write compact summary + per-session event log
                try:
                    session_id = (task.metadata or {}).get("session_id", "").strip()
                    if session_id:
                        session = self.session_store.get(session_id)
                        if session:
                            session.last_task_id = task.id
                            if not result.success:
                                full_out = self._short_failure_reason(result) or "(failed)"
                            else:
                                full_out = self._session_reply_text(result).strip()
                            # last_result_summary is a short preview used by Telegram
                            # and the session list — keep it brief (last 400 chars).
                            session.last_result_summary = full_out[-400:] if len(full_out) > 400 else full_out
                            session.last_summary = session.last_result_summary
                            session.last_files_modified = result.files_modified or []
                            artifact_path = str(Path(config.system.results_dir) / f"{task.id}.json")
                            session.last_artifact_path = artifact_path
                            session.task_history.append({
                                "task_id": task.id,
                                "timestamp": result.timestamp,
                                "success": result.success,
                                "execution_time": round(result.execution_time or 0.0, 2),
                                "user_message": session.last_user_message,
                                "result_summary": full_out,
                                "files_modified": session.last_files_modified[:20],
                            })
                            session.task_history = session.task_history[-20:]
                            if result.success:
                                session.status = SessionStatus.AWAITING_INPUT
                            elif "cancelled" in [str(err).lower() for err in (result.errors or [])]:
                                session.status = SessionStatus.CANCELLED
                            else:
                                session.status = SessionStatus.ERROR
                            self.session_store.save(session)

                            # Compact summary  state/summaries/<session_id>.md
                            self._write_session_summary(session, result)

                            # Per-session event log  logs/session_events/<session_id>.log
                            self._append_session_event(session_id, task.id, result)
                except Exception as e:
                    logger.warning(f"session_update_failed task_id={task.id} error={e}")

                # Archive processed task file to avoid reprocessing
                try:
                    source_path_str = None
                    if getattr(task, "metadata", None):
                        source_path_str = task.metadata.get("__file_path")
                    if source_path_str:
                        source_path = Path(source_path_str)
                        processed_dir = Path(config.system.tasks_dir) / "processed"
                        processed_dir.mkdir(parents=True, exist_ok=True)
                        target_name = f"{task.id}.{task.status.value}.task.md"
                        target_path = processed_dir / target_name
                        # Only move if source exists and is not already in processed
                        if source_path.exists() and source_path.parent != processed_dir:
                            source_path.replace(target_path)
                            logger.info(f"event=task_archived task_id={task.id} to={target_path}")
                            self._emit_event("task_archived", task, {"to": str(target_path)})
                except Exception as e:
                    logger.warning(f"event=task_archive_failed task_id={task.id} error={e}")
                    self._emit_event("task_archive_failed", task, {"error": str(e)})
                finally:
                    # Release in-flight lock now that processing is complete
                    try:
                        if getattr(task, "metadata", None):
                            self._inflight_paths.discard(task.metadata.get("__file_path", ""))
                            # Clear pending and persist
                            self._pending_files.discard(task.metadata.get("__file_path", ""))
                            self._save_state()
                    except Exception:
                        pass
                
                # Cleanup cancellation and running maps
                try:
                    self._task_cancel_events.pop(task.id, None)
                    self._running_exec_tasks.pop(task.id, None)
                    self._shutdown_interrupted_tasks.discard(task.id)
                    self.active_tasks.pop(task.id, None)
                except Exception:
                    pass
                # Mark task as done in queue
                self.task_queue.task_done()
                
            except asyncio.TimeoutError:
                # No tasks available, continue
                continue
            except asyncio.CancelledError:
                logger.info(f"Worker {worker_name} cancelled")
                break
            except Exception as e:
                logger.error(f"Worker {worker_name} error: {e}")
                # Continue processing other tasks
                continue
        
        logger.info(f"Task worker {worker_name} stopped")
    
    async def process_task(self, task: Task) -> TaskResult:
        """Process a single task through the complete pipeline.

        Steps:
        1) Execute the task via backend-native session resume or stateless Claude bridge
        2) Summarize results with LLAMA (or fallback) for `summaries/*.txt`
        3) Run validation engine and attach metadata
        4) Persist `results/*.json` and emit events
        """
        start_time = time.time()
        
        try:
            task.status = TaskStatus.PROCESSING

            # Opt-in continuation: if this task declares `continues: <prior_task_id>`
            # (via .task.md frontmatter or submit_instruction extra_metadata), prepend
            # bounded, fenced prior context to the prompt exactly once, before the
            # retry loop and the remote/local branch so every execution path carries
            # it. Tasks without `continues:` are byte-identical to before.
            await self._maybe_inject_compact_context(task)

            # Keep the user's prompt intact. Native Claude/Codex runtime should decide
            # how to approach the task rather than our local prompt-rewrite layer.
            logger.debug(f"Executing task {task.id}")
            max_retries = getattr(config.validation, "max_retries", 2)
            retry_delay = 1.0
            backoff_mult = max(1, getattr(config.validation, "backoff_multiplier", 2))
            attempt = 0
            last_result: Optional[TaskResult] = None
            session_recreated = False
            next_spawn_reason = "initial"
            # Per-task timeout override via frontmatter metadata `timeout_sec`, else system default
            try:
                timeout_s = int(task.metadata.get("timeout_sec", config.system.task_timeout)) if getattr(task, "metadata", None) else config.system.task_timeout
            except Exception:
                timeout_s = config.system.task_timeout
            cancel_ev = self._task_cancel_events.get(task.id)

            # Resolve session up front so we can decide whether this task is
            # pinned to a remote mesh node before entering the local retry loop.
            session_id = (task.metadata or {}).get("session_id", "").strip()
            session = self.session_store.get(session_id) if session_id else None

            # Mesh routing: only sessions explicitly pinned to a remote node
            # (`session.machine_id` set) with MESH_ENABLED=true take this path.
            # Everything else falls through to the untouched local retry loop
            # below — zero behavior change for ordinary local sessions.
            _host = socket.gethostname()
            _pinned_elsewhere = bool(
                session and session.machine_id and session.machine_id != _host
            )
            route_remote = bool(config.mesh.enabled and _pinned_elsewhere)

            # Affinity guard (A11): a session pinned to a *different* node must NOT
            # execute in this host's local worker pool. Before A11, if `route_remote`
            # came out False for any reason (mesh flag not seen at this call site,
            # etc.) while the session named another node, the task silently ran
            # locally on the wrong machine — corrupting backend_session_id continuity
            # and producing a null/duplicate gateway_node_id (the #9 smoke failure).
            # Make that case loud instead of silent: log the exact sub-conditions and
            # refuse local execution.
            if _pinned_elsewhere and not route_remote:
                logger.error(
                    "event=affinity_unrouted task_id=%s session_id=%s machine_id=%s host=%s "
                    "mesh_enabled=%s — refusing local execution of a remote-pinned session",
                    task.id, getattr(session, "session_id", None),
                    getattr(session, "machine_id", None), _host, config.mesh.enabled,
                )
                self._emit_event(
                    "affinity_unrouted",
                    task,
                    {
                        "session_id": getattr(session, "session_id", None),
                        "machine_id": getattr(session, "machine_id", None),
                        "host": _host,
                        "mesh_enabled": bool(config.mesh.enabled),
                    },
                )
                if config.mesh.enabled:
                    # Mesh is on and the node is named — honor the pin via the remote
                    # path (which fails loudly if the node is offline; no local fallback).
                    route_remote = True
                else:
                    # Mesh disabled but the session is pinned elsewhere: we cannot
                    # honor affinity and must not run on the wrong host. Fail honestly.
                    last_result = TaskResult(
                        task_id=task.id,
                        success=False,
                        output="",
                        errors=[
                            f"Session pinned to node {session.machine_id!r} but mesh is "
                            f"disabled on {_host!r}; cannot execute without violating "
                            f"session affinity."
                        ],
                        files_modified=[],
                        execution_time=time.time() - start_time,
                        timestamp=datetime.now().isoformat(),
                    )
                    setattr(last_result, "backend_name", getattr(session, "backend", None))
                    last_result.error_class = self._classify_error(last_result)
                    last_result.retries = 0
                    route_remote = True  # skip the local loop below

            if route_remote and last_result is None:
                last_result = await self._process_task_remote(task, session, start_time, timeout_s)

            # Defense-in-depth (A11/A18 invariant): the local worker loop must
            # never execute a turn for a session pinned to a *different* host —
            # that would fork backend_session_id continuity onto the wrong box.
            # The affinity guard above already forces route_remote=True for such
            # sessions (so this loop is unreachable for them); assert it rather
            # than trust the guard alone. The mesh claim filter (db.py) is the
            # other, independent line of defense at claim time.
            if not route_remote:
                assert not _pinned_elsewhere, (
                    f"affinity invariant violated: session {getattr(session, 'session_id', None)!r} "
                    f"pinned to {getattr(session, 'machine_id', None)!r} reached the local worker "
                    f"loop on host {_host!r}"
                )

            while not route_remote:
                attempt += 1
                from src.core.telemetry import TelemetryContext
                local_action = (
                    "resume_session"
                    if session and session.backend_session_id
                    else "create_session"
                    if session
                    else "run_oneoff"
                )
                telemetry_context = TelemetryContext.create(
                    turn_id=task.id,
                    node_id=socket.gethostname(),
                    session_id=session_id or None,
                    backend=session.backend if session else self._resolve_task_backend(task),
                    model=session.model if session else None,
                    source="gateway",
                    attempt=attempt,
                    spawn_reason=next_spawn_reason,
                    retry_of_invocation_id=(
                        getattr(last_result, "telemetry_invocation_id", None)
                        if last_result is not None
                        else None
                    ),
                )
                next_spawn_reason = "retry"
                self._emit_turn_telemetry(
                    "invocation.created",
                    task,
                    {
                        "attempt": attempt,
                        "spawn_reason": telemetry_context.spawn_reason,
                        "action": local_action,
                        "retry_of_invocation_id": telemetry_context.retry_of_invocation_id,
                    },
                    invocation_id=telemetry_context.invocation_id,
                    backend=telemetry_context.backend,
                    model=telemetry_context.model,
                )
                self._emit_turn_telemetry(
                    "invocation.started",
                    task,
                    {"action": local_action},
                    invocation_id=telemetry_context.invocation_id,
                    backend=telemetry_context.backend,
                    model=telemetry_context.model,
                )
                # Run execution as a task to allow timeout/cancel
                # Use session backend (with native resume) when task belongs to a session.
                # For non-session tasks, use the native backend directly instead of
                # the legacy Claude bridge/task-file execution path.
                if session:
                    session.status = SessionStatus.BUSY
                    self.session_store.save(session)
                    backend_name = session.backend
                    backend = self._backends.get(backend_name, self._backends["claude"])
                    session.last_user_message = task.prompt
                    if session.backend_session_id:
                        from src.core.backend_call import call_backend
                        exec_task = asyncio.create_task(
                            asyncio.to_thread(
                                call_backend,
                                backend.resume_session,
                                session,
                                task.prompt,
                                telemetry_context=telemetry_context,
                                telemetry_sink=self._telemetry_sink,
                            )
                        )
                    else:
                        from src.core.backend_call import call_backend
                        exec_task = asyncio.create_task(
                            asyncio.to_thread(
                                call_backend,
                                backend.create_session,
                                session,
                                telemetry_context=telemetry_context,
                                telemetry_sink=self._telemetry_sink,
                            )
                        )
                else:
                    backend_name = str((task.metadata or {}).get("backend") or "claude").strip().lower()
                    backend = self._backends.get(backend_name, self._backends["claude"])
                    cwd_override = str((task.metadata or {}).get("cwd") or "").strip()
                    if not cwd_override:
                        cwd_override = str(getattr(config.claude, "base_cwd", "") or "").strip()
                    from src.core.backend_call import call_backend
                    exec_task = asyncio.create_task(
                        asyncio.to_thread(
                            call_backend,
                            backend.run_oneoff,
                            cwd_override,
                            task.prompt,
                            telemetry_context=telemetry_context,
                            telemetry_sink=self._telemetry_sink,
                        )
                    )
                self._running_exec_tasks[task.id] = exec_task
                # Wait for whichever happens first
                wait_set = {exec_task}
                cancel_waiter: Optional[asyncio.Task] = None
                timeout_waiter: Optional[asyncio.Task] = None
                heartbeat_task: Optional[asyncio.Task] = None
                try:
                    if cancel_ev is not None:
                        cancel_waiter = asyncio.create_task(cancel_ev.wait())
                        wait_set.add(cancel_waiter)
                    if timeout_s and timeout_s > 0:
                        timeout_waiter = asyncio.create_task(asyncio.sleep(timeout_s))
                        wait_set.add(timeout_waiter)
                    heartbeat_interval = getattr(config.system, "task_heartbeat_interval_sec", 300)
                    if self.telegram_interface and heartbeat_interval > 0:
                        heartbeat_task = asyncio.create_task(
                            self._send_task_heartbeats(task, session, start_time, heartbeat_interval, timeout_s)
                        )
                    done, pending = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
                    if exec_task in done:
                        raw = exec_task.result()
                        # Normalize ExecutionResult (from backends) to TaskResult
                        from src.core.interfaces import ExecutionResult as _ER
                        if isinstance(raw, _ER):
                            # Persist backend session ID back onto the session record.
                            # Save on both success and failure — if the backend created/
                            # recreated a session (e.g. after server restart) but the
                            # first message failed, we still want the new ID persisted so
                            # the next turn can resume rather than starting fresh again.
                            if session and (
                                raw.backend_session_id
                                or session.cache_health != "unknown"
                                or session.driver_status
                            ):
                                # _observe_cache_health mutates cache_health /
                                # cache_unhealthy_count in place on this session
                                # object during the backend call; persist them
                                # (alongside any new backend_session_id) so the
                                # cache_unhealthy_count>=2 guard survives across
                                # turns rather than resetting each time.
                                if raw.backend_session_id:
                                    session.backend_session_id = raw.backend_session_id
                                self.session_store.save(session)
                            result = TaskResult(
                                task_id=task.id,
                                success=raw.success,
                                output=raw.output,
                                errors=raw.errors,
                                files_modified=raw.files_modified,
                                execution_time=raw.execution_time,
                                timestamp=datetime.now().isoformat(),
                                file_changes=getattr(raw, "file_changes", []),
                                raw_stdout=getattr(raw, "raw_stdout", ""),
                                raw_stderr=getattr(raw, "raw_stderr", ""),
                                parsed_output=getattr(raw, "parsed_output", None),
                                return_code=getattr(raw, "return_code", 0),
                            )
                            setattr(result, "backend_name", backend_name)
                            if raw.telemetry is not None:
                                setattr(
                                    result,
                                    "telemetry_invocation_id",
                                    raw.telemetry.invocation_id,
                                )
                        else:
                            result = raw
                    elif cancel_waiter and cancel_waiter in done:
                        # Cooperative cancellation
                        self._emit_turn_telemetry(
                            "turn.cancel_requested",
                            task,
                            {
                                "reason_code": (
                                    "gateway_shutdown"
                                    if task.id in self._shutdown_interrupted_tasks
                                    else "user_cancel"
                                )
                            },
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        self._emit_turn_telemetry(
                            "process.termination_requested",
                            task,
                            {"reason_code": "gateway_cancel"},
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        if session:
                            with contextlib.suppress(Exception):
                                backend.cancel(session)
                        exec_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await exec_task
                        execution_time = time.time() - start_time
                        self._emit_event("cancelled", task, {"when": "during_execution"})
                        interrupted = task.id in self._shutdown_interrupted_tasks
                        if session:
                            session.status = SessionStatus.ERROR if interrupted else SessionStatus.CANCELLED
                            self.session_store.save(session)
                        result = TaskResult(
                            task_id=task.id,
                            success=False,
                            output="",
                            errors=["interrupted by gateway restart" if interrupted else "cancelled"],
                            files_modified=[],
                            execution_time=execution_time,
                            timestamp=datetime.now().isoformat(),
                        )
                        setattr(result, "backend_name", backend_name)
                        setattr(result, "telemetry_invocation_id", telemetry_context.invocation_id)
                        self._emit_turn_telemetry(
                            "invocation.completed",
                            task,
                            {
                                "status": "failed",
                                "duration_ms": round(execution_time * 1000),
                                "error_code": "cancelled",
                            },
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        return result
                    else:
                        # Timeout
                        self._emit_turn_telemetry(
                            "process.termination_requested",
                            task,
                            {"reason_code": "gateway_timeout"},
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        if session:
                            with contextlib.suppress(Exception):
                                backend.cancel(session)
                        exec_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await exec_task
                        execution_time = time.time() - start_time
                        self._emit_event("timeout", task, {"timeout_s": timeout_s})
                        self._emit_turn_telemetry(
                            "turn.timeout_requested",
                            task,
                            {
                                "timeout_kind": "gateway_timeout",
                                "timeout_ms": timeout_s * 1000,
                            },
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        if session:
                            session.status = SessionStatus.ERROR
                            self.session_store.save(session)
                        elapsed_min = int(execution_time // 60)
                        timeout_min = int(timeout_s // 60)
                        timeout_error = (
                            f"Task timed out after {elapsed_min}m (limit: {timeout_min}m). "
                            f"Claude was still running when the gateway cut it off. "
                            f"To allow more time set GATEWAY_TASK_TIMEOUT_SEC (currently {timeout_s}). "
                            f"You can retry with a larger scope split or use /session_cancel then resubmit."
                        )
                        result = TaskResult(
                            task_id=task.id,
                            success=False,
                            output="",
                            errors=[timeout_error],
                            files_modified=[],
                            execution_time=execution_time,
                            timestamp=datetime.now().isoformat(),
                        )
                        setattr(result, "backend_name", backend_name)
                        setattr(result, "telemetry_invocation_id", telemetry_context.invocation_id)
                        self._emit_turn_telemetry(
                            "invocation.completed",
                            task,
                            {
                                "status": "failed",
                                "duration_ms": round(execution_time * 1000),
                                "error_code": "timeout",
                            },
                            invocation_id=telemetry_context.invocation_id,
                            backend=backend_name,
                        )
                        return result
                finally:
                    # Cancel any pending helper waiters
                    for w in (cancel_waiter, timeout_waiter, heartbeat_task):
                        if w and not w.done():
                            w.cancel()
                error_class = self._classify_error(result)
                result.error_class = error_class
                result.retries = attempt - 1
                setattr(result, "telemetry_invocation_id", telemetry_context.invocation_id)
                self._emit_turn_telemetry(
                    "invocation.completed",
                    task,
                    {
                        "status": "success" if result.success else "failed",
                        "duration_ms": round((result.execution_time or 0.0) * 1000),
                        "exit_code": getattr(result, "return_code", None),
                        "error_code": error_class or None,
                    },
                    invocation_id=telemetry_context.invocation_id,
                    backend=backend_name,
                    model=telemetry_context.model,
                )

                if session and not result.success and not session_recreated and self._is_missing_backend_conversation(result):
                    stale_id = session.backend_session_id
                    session.backend_session_id = ""
                    session.status = SessionStatus.BUSY
                    self.session_store.save(session)
                    session_recreated = True
                    logger.warning(
                        "event=session_recreated task_id=%s stale_backend_session_id=%s reason=missing_conversation",
                        task.id,
                        stale_id,
                    )
                    self._emit_event(
                        "session_recreated",
                        task,
                        {"stale_backend_session_id": stale_id, "reason": "missing_conversation"},
                    )
                    last_result = result
                    next_spawn_reason = "session_recreate"
                    continue

                # Determine retry strategy per error class
                strategy = self._get_retry_strategy(error_class)
                max_retries = strategy.get("max_retries", max_retries)
                if attempt == 1:
                    retry_delay = strategy.get("initial_delay", retry_delay)
                    backoff_mult = strategy.get("backoff_multiplier", backoff_mult)
                if (not result.success) and attempt <= max_retries:
                    jitter = random.uniform(0.85, 1.35)
                    delay = max(0.0, retry_delay * jitter)
                    logger.warning(f"event=retry task_id={task.id} attempt={attempt} class={error_class} delay_s={delay:.2f}")
                    self._emit_event("retry", task, {"attempt": attempt, "class": error_class, "delay_s": delay})
                    self._emit_turn_telemetry(
                        "invocation.retry_scheduled",
                        task,
                        {
                            "retry_reason": error_class,
                            "delay_ms": round(delay * 1000),
                            "next_attempt": attempt + 1,
                            "retry_of_invocation_id": telemetry_context.invocation_id,
                        },
                        invocation_id=telemetry_context.invocation_id,
                        backend=backend_name,
                    )
                    await asyncio.sleep(delay)
                    retry_delay = retry_delay * backoff_mult if retry_delay > 0 else strategy.get("initial_delay", 1.0) * backoff_mult
                    last_result = result
                    next_spawn_reason = "retry"
                    continue
                last_result = result
                break
            
            # Step 4: Summarize results with LLAMA — skip for session tasks so
            # Claude's actual response is preserved unmodified in output.
            if not session_id:
                logger.debug(f"Step 4: Summarizing results for task {task.id}")
                summary = self.llama_mediator.summarize_result(last_result, task)
                last_result.output = summary + "\n\n" + last_result.output
                logger.info(f"event=summarized task_id={task.id}")
                self._emit_event("summarized", task)
            else:
                logger.debug(f"Step 4: Skipping LLAMA summarization for session task {task.id}")
            
            # Step 5: Validation pass (MVP) — skip sentence-transformer similarity
            # for session tasks; the llama check is meaningless there and triggers
            # the expensive SentenceTransformer encode on every turn.
            try:
                if session_id:
                    llama_validation = self.validation_engine.validate_task_result(
                        result=last_result,
                        expected_files=task.target_files or [],
                        task_type=task.type,
                    ).__dict__
                    validation_summary = {"llama": {"valid": True, "skipped": True}, "result": llama_validation}
                else:
                    validation_summary = {
                        "llama": self.validation_engine.validate_llama_output(
                            input_text=task.prompt or "",
                            output=last_result.output or "",
                            task_type=task.type,
                        ).__dict__,
                        "result": self.validation_engine.validate_task_result(
                            result=last_result,
                            expected_files=task.target_files or [],
                            task_type=task.type,
                        ).__dict__,
                    }
                # Attach lightweight validation data into parsed_output for artifacts
                if isinstance(last_result.parsed_output, dict):
                    last_result.parsed_output.setdefault("validation", validation_summary)
                else:
                    last_result.parsed_output = {"content": last_result.output, "validation": validation_summary}
                # Also surface at top level for artifact visibility
                setattr(last_result, "validation", validation_summary)
                logger.info(
                    f"event=validated task_id={task.id} "
                    f"valid_llama={validation_summary['llama']['valid']} "
                    f"valid_result={validation_summary['result']['valid']}"
                )
                self._emit_event("validated", task, {
                    "valid_llama": validation_summary["llama"]["valid"],
                    "valid_result": validation_summary["result"]["valid"],
                })
            except Exception as _:
                # Non-fatal; continue
                pass

            return last_result
            
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"Task processing failed for {task.id}: {e}")
            return TaskResult(
                task_id=task.id,
                success=False,
                output="",
                errors=[str(e)],
                files_modified=[],
                execution_time=execution_time,
                timestamp=datetime.now().isoformat()
            )

    async def _process_task_remote(
        self,
        task: "Task",
        session: Any,
        start_time: float,
        timeout_s: int,
    ) -> "TaskResult":
        """Execute a mesh-pinned session's task on its assigned remote node.

        Only reachable when `MESH_ENABLED=true` AND `session.machine_id` is
        set — see the routing check in `process_task`. Mirrors the bookkeeping
        `process_task`'s local retry loop performs (BUSY status, heartbeats,
        cancellation, error classification, session error state) around a
        single call to `_dispatch_to_node`, which enqueues the task, waits for
        the pinned worker to claim and post a result, and returns a terminal
        `TaskResult`.

        Session affinity is a hard requirement: if the pinned node is not
        registered or not online, this fails loudly with no local fallback —
        silently running on this machine would corrupt `backend_session_id`
        continuity, since backend sessions are machine-local.
        """
        from src.control.node_registry import get_registry
        from src.core.observability import set_log_context

        # Correlate every log line + event emitted during this remote dispatch
        # with the task and session, across this gateway's logs and (by the same
        # task_id) the worker's logs on the remote machine.
        set_log_context(task_id=task.id, session_id=session.session_id)

        backend_name = session.backend
        session.status = SessionStatus.BUSY
        session.last_user_message = task.prompt
        self.session_store.save(session)

        def _routing_failure(msg: str) -> "TaskResult":
            logger.error("event=mesh_routing_failed task_id=%s session_id=%s machine_id=%s reason=%s",
                         task.id, session.session_id, session.machine_id, msg)
            self._emit_event("mesh_routing_failed", task, {"machine_id": session.machine_id, "reason": msg})
            result = TaskResult(
                task_id=task.id,
                success=False,
                output="",
                errors=[msg],
                files_modified=[],
                execution_time=time.time() - start_time,
                timestamp=datetime.now().isoformat(),
            )
            setattr(result, "backend_name", backend_name)
            return result

        registry = get_registry()

        def _check_pinned_liveness() -> Tuple[Any, bool]:
            """Return ``(node_handle_or_None, is_online)`` for the pinned node.

            Checks the in-memory registry first, then falls back to the shared
            DB. The in-memory registry is only populated when this process also
            runs the task server (co-located deployment); in a split setup, or
            after a gateway restart that wiped the in-memory registry, the node
            may only exist in the DB — without the fallback a live node reads as
            offline and needlessly kills the session. Used both for the initial
            check and for each poll during the A18 offline grace hold."""
            _node = registry.get(session.machine_id)
            if _node is not None and _node.status == "online":
                return _node, True
            try:
                from src.control.db import get_db as _get_db
                _db = _get_db()
                if _db is not None:
                    _row = _db.get_node(session.machine_id)
                    if _row and _row.get("status") == "online":
                        return _node, True
            except Exception:
                logger.warning(
                    "event=affinity_liveness_db_check_failed task_id=%s machine_id=%s",
                    task.id, session.machine_id, exc_info=True,
                )
            return _node, False

        node, node_online = _check_pinned_liveness()

        if not node_online:
            # A18 — pinned-worker offline fallback. `grace=0` ⇒ disabled: reproduce
            # the pre-A18 (A11) behavior byte-for-byte (immediate honest fail,
            # terminal ERROR, no hold). `grace>0` ⇒ bounded hold-and-requeue: a
            # transient worker blip no longer permanently kills a healthy session.
            grace_sec = max(0, int(getattr(config.mesh, "affinity_offline_grace_sec", 0) or 0))
            if grace_sec <= 0:
                result = _routing_failure(f"Node {session.machine_id!r} is offline; cannot continue session (no local fallback — affinity is required)")
                session.status = SessionStatus.ERROR
                self.session_store.save(session)
                result.error_class = self._classify_error(result)
                result.retries = 0
                return result

            # Option A — bounded hold-and-requeue. The pinned node is offline right
            # now, but the outage may clear within the grace window. Hold the
            # session in a distinct, honest PAUSED state and poll liveness. The
            # turn is NEVER relocated: it is only dispatched (below) once the node
            # is confirmed online again, and the mesh claim filter (db.py) still
            # guarantees only the pinned node can ever claim it. A11 invariant is
            # preserved exactly — no off-host execution, ever.
            poll_interval = max(0.5, float(getattr(config.mesh, "affinity_offline_poll_interval_sec", 5.0) or 5.0))
            poll_interval = min(poll_interval, float(grace_sec))
            deadline = time.time() + grace_sec

            logger.warning(
                "event=affinity_hold_started task_id=%s session_id=%s machine_id=%s grace_sec=%s poll_sec=%.1f",
                task.id, session.session_id, session.machine_id, grace_sec, poll_interval,
            )
            self._emit_event("affinity_hold_started", task, {
                "session_id": session.session_id,
                "machine_id": session.machine_id,
                "grace_sec": grace_sec,
            })
            session.status = SessionStatus.PAUSED_PINNED_NODE_OFFLINE
            self.session_store.save(session)

            hold_cancel_ev = self._task_cancel_events.get(task.id)
            polls = 0
            while time.time() < deadline:
                # Honor an operator cancel during the hold rather than pinning the
                # session to a node that may never return.
                if hold_cancel_ev is not None and hold_cancel_ev.is_set():
                    break
                await asyncio.sleep(min(poll_interval, max(0.0, deadline - time.time())))
                polls += 1
                node, node_online = _check_pinned_liveness()
                if node_online:
                    break

            if not node_online:
                # Grace expired (or cancelled) with the node still down. Honest,
                # resumable terminal state + a distinct event — the operator can
                # retry once the node returns, or re-pin the session to another
                # node. Not a bare ERROR; not an off-host fallback.
                cancelled = hold_cancel_ev is not None and hold_cancel_ev.is_set()
                reason = (
                    f"Node {session.machine_id!r} still offline after {grace_sec}s affinity "
                    f"grace window; session paused (retry when the node returns, or re-pin to "
                    f"another node). No off-host fallback — affinity is required."
                )
                logger.error(
                    "event=affinity_offline_timeout task_id=%s session_id=%s machine_id=%s "
                    "grace_sec=%s polls=%s cancelled=%s",
                    task.id, session.session_id, session.machine_id, grace_sec, polls, cancelled,
                )
                self._emit_event("affinity_offline_timeout", task, {
                    "session_id": session.session_id,
                    "machine_id": session.machine_id,
                    "grace_sec": grace_sec,
                    "polls": polls,
                    "cancelled": cancelled,
                })
                result = TaskResult(
                    task_id=task.id,
                    success=False,
                    output="",
                    errors=[reason],
                    files_modified=[],
                    execution_time=time.time() - start_time,
                    timestamp=datetime.now().isoformat(),
                )
                setattr(result, "backend_name", backend_name)
                session.status = (
                    SessionStatus.CANCELLED if cancelled else SessionStatus.PINNED_NODE_OFFLINE
                )
                self.session_store.save(session)
                result.error_class = self._classify_error(result)
                result.retries = polls
                return result

            # Node re-registered within the grace window — the blip was invisible
            # to the operator. Resume normal dispatch below (emits mesh_dispatch).
            logger.info(
                "event=affinity_hold_resolved task_id=%s session_id=%s machine_id=%s polls=%s",
                task.id, session.session_id, session.machine_id, polls,
            )
            self._emit_event("affinity_hold_resolved", task, {
                "session_id": session.session_id,
                "machine_id": session.machine_id,
                "polls": polls,
            })
            session.status = SessionStatus.BUSY
            self.session_store.save(session)

        cancel_ev = self._task_cancel_events.get(task.id)
        heartbeat_task: Optional[asyncio.Task] = None
        heartbeat_interval = getattr(config.system, "task_heartbeat_interval_sec", 300)
        try:
            try:
                if self.telegram_interface and heartbeat_interval > 0:
                    heartbeat_task = asyncio.create_task(
                        self._send_task_heartbeats(task, session, start_time, heartbeat_interval, timeout_s)
                    )
                # node may be None here when liveness was confirmed via the DB
                # fallback (the in-memory registry didn't have it). machine_id is
                # the reliable identifier in every case.
                target_node = node.node_id if node is not None else session.machine_id
                # A18/A11 defense-in-depth: a pinned turn dispatches to its own
                # node or not at all. The mesh claim filter (db.py) already keeps
                # the local worker pool from ever claiming a pinned task; enforce
                # the invariant here too so a routing regression fails CLOSED
                # rather than silently forking the conversation on a substitute
                # host. Deliberately NOT an `assert` — this is a hard correctness
                # invariant that must survive `python -O` / PYTHONOPTIMIZE, which
                # strips asserts. Never reached for unpinned/mesh-disabled work.
                if target_node != session.machine_id:
                    result = _routing_failure(
                        f"affinity violation: session pinned to {session.machine_id!r} "
                        f"but dispatch target resolved to {target_node!r}; refusing off-host dispatch"
                    )
                    session.status = SessionStatus.ERROR
                    self.session_store.save(session)
                    result.error_class = self._classify_error(result)
                    result.retries = 0
                    return result
                logger.info("mesh_dispatch backend=%s -> %s", backend_name, target_node)
                self._emit_event("mesh_dispatch", task, {
                    "backend": backend_name,
                    "target_node": target_node,
                })
                # A11 follow-up: attribute this turn's execution_node_id to the
                # remote node instead of leaving it null. The worker's own rich
                # per-invocation telemetry (tool calls, model usage) ships
                # separately via the worker's own sink and may or may not arrive
                # depending on its connectivity to this gateway (see the A10 §T1
                # revalidation: it didn't, for the mesh path exercised there).
                # This pair of events is the one fact the gateway can assert
                # unconditionally around the dispatch call — that execution
                # happened on `target_node`, not here. Without it,
                # telemetry_projection.project_turn() has no event carrying
                # event_name in ("invocation.started", "process.spawned") for
                # mesh-dispatched turns, so execution_node_id (and
                # llm_invocations.node_id, derived from it) silently default to
                # null. If the worker's own telemetry later starts arriving too,
                # this will show as a second invocation row for the same
                # turn_id — acceptable overlap, not a correctness bug, since
                # each invocation_id is independently minted.
                _mesh_invocation_id = None
                try:
                    from src.core.telemetry import (
                        EMITTER_PROCESS_INSTANCE_ID,
                        build_event,
                        new_telemetry_id,
                    )
                    _mesh_invocation_id = new_telemetry_id("inv")
                    self._telemetry_sink.emit(
                        build_event(
                            "invocation.started",
                            turn_id=task.id,
                            session_id=session.session_id,
                            node_id=target_node,
                            emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                            source="worker",
                            invocation_id=_mesh_invocation_id,
                            backend=backend_name,
                            model=getattr(session, "model", None),
                            attributes={"action": "mesh_dispatch"},
                        )
                    )
                except Exception:
                    logger.warning("event=mesh_dispatch_telemetry_emit_failed task_id=%s", task.id, exc_info=True)
                result = await self._dispatch_to_node(task, session, node)
                if _mesh_invocation_id is not None:
                    try:
                        self._telemetry_sink.emit(
                            build_event(
                                "invocation.completed",
                                turn_id=task.id,
                                session_id=session.session_id,
                                node_id=target_node,
                                emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                                source="worker",
                                invocation_id=_mesh_invocation_id,
                                backend=backend_name,
                                model=getattr(session, "model", None),
                                attributes={
                                    "status": "success" if getattr(result, "success", False) else "failed",
                                    "duration_ms": int(getattr(result, "execution_time", 0.0) * 1000),
                                    "exit_code": getattr(result, "return_code", None),
                                },
                            )
                        )
                    except Exception:
                        logger.warning("event=mesh_dispatch_telemetry_emit_failed task_id=%s", task.id, exc_info=True)
            finally:
                if heartbeat_task and not heartbeat_task.done():
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task
        except Exception as exc:
            # Unexpected dispatch failure — return a clean error and unblock
            # the session rather than leaving it stuck as BUSY.
            result = _routing_failure(f"Unexpected dispatch error: {exc}")

        result.error_class = self._classify_error(result)
        result.retries = 0

        # Detached = gateway is shutting down while the remote worker keeps
        # running. Leave the session BUSY (do not touch its status) so startup
        # recovery reattaches and reports the worker's real result. Marking it
        # CANCELLED/ERROR here would be the fabricated state we're fixing.
        if getattr(result, "detached", False):
            logger.info(
                "event=mesh_dispatch_detached task_id=%s session_id=%s reason=gateway_shutdown",
                task.id, session.session_id,
            )
            return result

        if result.success:
            session.status = SessionStatus.AWAITING_INPUT
        elif cancel_ev is not None and cancel_ev.is_set():
            session.status = SessionStatus.CANCELLED
        else:
            session.status = SessionStatus.ERROR
        self.session_store.save(session)

        # Annotate failure errors with the node name so users see *which* machine failed.
        node_label = node.node_id if node is not None else session.machine_id
        if not result.success and node_label:
            if not result.errors:
                result.errors = [f"[{node_label}] Task failed with no error details"]
            else:
                result.errors = [
                    (f"[{node_label}] {e}" if e.strip() else f"[{node_label}] Task failed (no error message)")
                    if not str(e).startswith(f"[{node_label}]") else e
                    for e in result.errors
                ]

        first_error = result.errors[0] if result.errors else ""
        logger.info(
            "mesh_result success=%s elapsed=%.1fs%s",
            result.success, result.execution_time,
            "" if result.success else f" error={first_error}",
        )
        error_detail = getattr(result, "error_detail", "") or getattr(result, "raw_stderr", "") or ""
        if not result.success and error_detail:
            logger.info(
                "mesh_result_detail task_id=%s node=%s detail=%s",
                task.id,
                node_label,
                error_detail[:4000],
            )
        self._emit_event("mesh_result", task, {
            "success": result.success,
            "target_node": node.node_id if node is not None else session.machine_id,
            "duration_s": round(result.execution_time, 3),
            "error_class": result.error_class,
            "error": first_error,
            "error_detail": error_detail[:4000],
        })

        return result

    def _reconstruct_task_content(self, task: Task) -> str:
        """Reconstruct a `.task.md` representation for LLAMA processing.

        Used to provide consistent context to LLAMA summarization/optimizations.
        """
        content = f"""---
id: {task.id}
type: {task.type.value}
priority: {task.priority.value}
created: {task.created}
---

# {task.title}

**Target Files:**
{chr(10).join('- ' + f for f in task.target_files)}

**Prompt:**
{task.prompt}

**Success Criteria:**
{chr(10).join('- [ ] ' + c for c in task.success_criteria)}

**Context:**
{task.context}
"""
        return content

    def _write_artifacts(self, task_id: str, result: TaskResult, task: Optional[Task] = None):
        """Persist results and summaries to disk"""
        results_dir = Path(config.system.results_dir)
        summaries_dir = Path(config.system.summaries_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        summaries_dir.mkdir(parents=True, exist_ok=True)

        # Write raw JSON artifact with structured fields
        artifact = {
            "schema_version": "1.0",
            "task_id": task_id,
            "success": result.success,
            "return_code": result.return_code,
            "timestamp": result.timestamp,
            "execution_time": result.execution_time,
            "errors": result.errors,
            "files_modified": result.files_modified,
            "file_changes": getattr(result, "file_changes", []),
            # Linkage for multi-turn/threaded contexts (optional)
            "parent_task_id": getattr(result, "parent_task_id", None),
            "turn_of": getattr(result, "turn_of", None),
            # Keep full stdout/stderr for now, but add triage previews
            "raw_stdout": result.raw_stdout,
            "raw_stderr": result.raw_stderr,
            "triage": {
                "stdout_head": (result.raw_stdout or "")[:2048],
                "stdout_tail": (result.raw_stdout or "")[-2048:] if result.raw_stdout else "",
                "stderr_head": (result.raw_stderr or "")[:2048],
                "stderr_tail": (result.raw_stderr or "")[-2048:] if result.raw_stderr else "",
            },
            "parsed_output": result.parsed_output,
            "validation": getattr(result, "validation", None),
            "retry": {
                "retries": getattr(result, "retries", 0),
                "error_class": getattr(result, "error_class", ""),
            },
            "security": {
                "guarded_write": bool(getattr(config.system, "guarded_write", False)),
                "allowlist_root": getattr(config.claude, "allowed_root", None),
                "violations": [],
            },
            "suggested_actions": self._suggest_actions(getattr(result, "error_class", ""), result) if not result.success else [],
            # Minimal status blocks for operability/triage
            "orchestrator": {
                "components": self.component_status,
                "workers": len(self.worker_tasks),
            },
            "runtime": {
                "backend": getattr(result, "backend_name", "claude"),
                "claude_executable": shutil.which("claude") or "claude",
                "codex_executable": shutil.which("codex") or "codex",
                "max_turns": getattr(config.claude, "max_turns", 3),
                "timeout": getattr(config.claude, "timeout", 600),
                "skip_permissions": bool(getattr(config.claude, "skip_permissions", True)),
            },
            "bridge": {
                "available": bool(shutil.which("claude")),
                "claude_executable": shutil.which("claude") or "claude",
                "max_turns": getattr(config.claude, "max_turns", 3),
                "timeout": getattr(config.claude, "timeout", 600),
                "skip_permissions": bool(getattr(config.claude, "skip_permissions", True)),
            },
            "llama": self.llama_mediator.get_status(probe=False),
            "tool_summary": self._extract_tool_summary(result.raw_stdout or ""),
        }
        if task is not None:
            artifact["task"] = {
                "type": getattr(task.type, "value", str(task.type)),
                "priority": getattr(task.priority, "value", str(task.priority)),
                "title": task.title,
                # FULL user instruction — `title` is only a truncated display label
                # (`Task: {description[:50]}...`); persisting `prompt` is what lets
                # the transcript show the complete message instead of a 50-char clip.
                "prompt": task.prompt or "",
                "target_files": list(task.target_files or []),
                "source": str((task.metadata or {}).get("source") or "runtime"),
                "cwd": str((task.metadata or {}).get("cwd") or ""),
            }
            session_id = str((task.metadata or {}).get("session_id") or "").strip()
            if session_id:
                session = self.session_store.get(session_id)
                artifact["session"] = {
                    "session_id": session_id,
                    "backend": session.backend if session else getattr(result, "backend_name", "claude"),
                    "backend_session_id": session.backend_session_id if session else "",
                    "repo_path": session.repo_path if session else str((task.metadata or {}).get("cwd") or ""),
                    "owner_user_id": session.owner_user_id if session else None,
                    "telegram_chat_id": session.telegram_chat_id if session else None,
                }

        import json
        # Allowlist enforcement on files_modified (telemetry + artifact note)
        try:
            allow_root = getattr(config.claude, "allowed_root", None)
            if allow_root and artifact.get("files_modified"):
                from pathlib import Path as _P
                root = _P(allow_root).resolve()
                bad = []
                for f in list(artifact.get("files_modified") or []):
                    try:
                        p = _P(f).resolve()
                        if not (root in p.parents or p == root):
                            bad.append(f)
                    except Exception:
                        bad.append(f)
                if bad:
                    artifact["security"]["violations"] = [
                        {"type": "out_of_root", "path": b} for b in bad
                    ]
                    # Emit security event
                    try:
                        self._emit_event("security_violation", None, {"paths": bad})
                    except Exception:
                        pass
        except Exception:
            pass

        # raw_stdout is 87% of artifact bytes (264 MB across the corpus) and pure
        # debug NDJSON — nothing product-facing reads it back once the reply +
        # usage are extracted into mesh_tasks. When `slim_artifacts` is on, move it
        # to a gzipped sidecar (~10x smaller) and drop it from the JSON, so the DB
        # is the self-sufficient source and the on-disk files shrink to metadata.
        slim = bool(getattr(config.system, "slim_artifacts", False))
        if slim and artifact.get("raw_stdout"):
            try:
                import gzip
                raw_dir = results_dir / "raw"
                raw_dir.mkdir(parents=True, exist_ok=True)
                with gzip.open(raw_dir / f"{task_id}.ndjson.gz", "wt", encoding="utf-8") as gz:
                    gz.write(artifact.get("raw_stdout") or "")
            except Exception as e:
                logger.warning(f"event=raw_archive_failed task_id={task_id} error={e}")
            else:
                artifact["raw_stdout"] = ""
                artifact["raw_stdout_archived"] = f"raw/{task_id}.ndjson.gz"

        flat_artifact_path = results_dir / f"{task_id}.json"
        flat_artifact_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        # Update artifact index (best-effort)
        try:
            self._update_artifact_index(task_id, flat_artifact_path)
        except Exception:
            pass


        # Write human readable summary (extract the LLAMA-generated summary)
        # LLAMA generates a summary and prepends it to result.output
        if result.output:
            # The LLAMA summary is prepended to the output, separated by double newlines
            # So we take everything before the first double newline as the summary
            summary_text = result.output.split("\n\n", 1)[0]
            
            # If the summary is too short (just a title), try to get more content
            if len(summary_text.strip()) < 50:
                # Look for the actual summary content after the title
                paragraphs = result.output.split("\n\n")
                if len(paragraphs) > 1:
                    # Take first 2-3 paragraphs that look like actual content
                    meaningful_paras = []
                    for para in paragraphs[1:4]:  # Skip first (title), take next 3
                        para = para.strip()
                        if para and len(para) > 30 and not para.startswith("#"):
                            meaningful_paras.append(para)
                    if meaningful_paras:
                        summary_text = "\n\n".join(meaningful_paras)
        else:
            summary_text = ""
            
        (summaries_dir / f"{task_id}_summary.txt").write_text(
            summary_text,
            encoding="utf-8"
        )

    def _get_retry_strategy(self, error_class: str) -> Dict[str, Any]:
        """Return retry strategy for an error class.

        Fields: max_retries, initial_delay, backoff_multiplier
        """
        default_max = max(0, getattr(config.validation, "max_retries", 2))
        default_mult = max(1, getattr(config.validation, "backoff_multiplier", 2))
        if error_class in ("none", "interactive", "auth", "fatal", "context_overflow"):
            return {"max_retries": 0, "initial_delay": 0.0, "backoff_multiplier": 1}
        if error_class == "timeout":
            return {"max_retries": min(1, default_max), "initial_delay": 1.0, "backoff_multiplier": 1}
        if error_class == "network":
            return {"max_retries": max(1, default_max), "initial_delay": 1.5, "backoff_multiplier": default_mult}
        if error_class == "rate_limit":
            return {"max_retries": max(2, default_max), "initial_delay": 2.0, "backoff_multiplier": max(2, default_mult)}
        return {"max_retries": default_max, "initial_delay": 1.0, "backoff_multiplier": default_mult}

    def _suggest_actions(self, error_class: str, result: TaskResult) -> List[str]:
        """Return actionable hints for common failure classes."""
        actions: List[str] = []
        ec = (error_class or "").lower()
        if ec == "interactive":
            actions.append("Enable skip-permissions or trust the folder; ensure non-interactive flags.")
        elif ec == "rate_limit":
            info = self._extract_rate_limit_info(result)
            if info and info.get("resetsAt"):
                try:
                    reset_dt = datetime.fromtimestamp(int(info["resetsAt"]))
                    actions.append(f"Usage limit active. Tasks will resume automatically after {reset_dt.strftime('%H:%M')}.")
                except Exception:
                    actions.append("Usage limit active. Tasks will resume when the limit resets.")
            else:
                actions.append("Usage limit active. Tasks will resume when the limit resets.")
        elif ec == "timeout":
            backend_name = str(getattr(result, "backend_name", "") or "").lower()
            if backend_name.startswith("opencode"):
                actions.append("Increase OPENCODE_TIMEOUT_SEC or reduce task scope.")
            else:
                actions.append("Increase GATEWAY_TASK_TIMEOUT_SEC or reduce task scope.")
        elif ec == "network":
            actions.append("Check connectivity/VPN; retry with backoff.")
        elif ec == "context_overflow":
            actions.append("Session context is full. Run /compact on the session or start a new session.")
        elif ec == "auth":
            actions.append("Run 'claude auth status' and re-authenticate if needed.")
        elif ec == "fatal":
            actions.append("Inspect stderr for root cause; adjust prompt/targets.")
        return actions

    def cancel_task(self, task_id: str) -> bool:
        """Request cooperative cancellation for a task.

        Returns True if a cancel signal was set for a queued or running task.
        """
        ev = self._task_cancel_events.get(task_id)
        if ev is None:
            # If task exists but no event yet (e.g., still queued elsewhere), create and set
            if task_id in self.active_tasks:
                ev = asyncio.Event()
                self._task_cancel_events[task_id] = ev
            else:
                return False
        if not ev.is_set():
            ev.set()
            t = self.active_tasks.get(task_id)
            # Interrupt the live backend turn directly, right now. The execution
            # loop's own graceful-cancel branch (which calls backend.cancel(session)
            # before tearing down exec_task) only fires if it's watching a
            # cancel_waiter built from THIS event — but that loop reads
            # `self._task_cancel_events.get(task.id)` once, before the task starts,
            # so for any task that was already running when cancel is requested
            # (the normal case) the event created just above is invisible to it.
            # Without this direct call, only the asyncio wrapper around
            # `asyncio.to_thread(...)` gets cancelled: the backend thread and its
            # live subprocess/session keep running unattended, never removed from
            # the driver's session pool, so the next turn on this session queues
            # up behind the abandoned one and appends the same prompt into the
            # same still-live conversation (the ever-growing-session bug).
            if t is not None:
                try:
                    session_id = (t.metadata or {}).get("session_id", "").strip()
                    session = self.session_store.get(session_id) if session_id else None
                    if session is not None:
                        backend_name = str(
                            (t.metadata or {}).get("backend") or session.backend or "claude"
                        ).strip().lower()
                        backend = self._backends.get(backend_name)
                        if backend is not None:
                            backend.cancel(session)
                except Exception:
                    logger.warning(
                        "event=cancel_backend_interrupt_failed task_id=%s", task_id, exc_info=True
                    )
            # Best-effort cancel running exec task
            task = self._running_exec_tasks.get(task_id)
            if task is not None and not task.done():
                task.cancel()
            # Emit cancel_requested event
            self._emit_event("cancel_requested", t if t else None, None)
            return True
        return False

    def _classify_error(self, result: TaskResult) -> str:
        """Classify error type for retry policy.

        Returns one of: none|interactive|rate_limit|timeout|network|auth|fatal
        """
        if result.success:
            return "none"
        # Prefer explicit interactive marker from bridge
        try:
            if any(isinstance(e, str) and "interactive_prompt_detected" in e for e in (result.errors or [])):
                return "interactive"
        except Exception:
            pass
        if self._extract_rate_limit_info(result) is not None:
            return "rate_limit"
        text = self._failure_text(result)
        text_lower = text.lower()
        if any(s in text_lower for s in ("rate limit", "rate-limit", "too many requests", "hit your limit", "you've hit your limit", "\"error\":\"rate_limit\"", "overagestatus")):
            return "rate_limit"
        if any(s in text_lower for s in ("timeout", "timed out", "inactivity")):
            return "timeout"
        if any(s in text_lower for s in ("connection reset", "connection aborted", "network error", "503", "504", "temporarily unavailable")):
            return "network"
        if any(s in text_lower for s in ("prompt is too long", "blocking_limit", "context_window", "context window")):
            return "context_overflow"
        if any(s in text_lower for s in ("unauthorized", "forbidden", "permission denied", "not logged in", "authentication")):
            return "auth"
        return "fatal"

    def _emit_event(self, name: str, task: Optional[Task] = None, extra: Optional[Dict[str, Any]] = None) -> None:
        """Append a single NDJSON event line to logs/events.ndjson.

        Thin wrapper over the shared observability spine. Preserves the legacy
        envelope keys (task_id, task_type, priority, status) so the existing
        `main.py stats` / `tail-events` readers keep parsing, while letting the
        spine fill node_id and any task_id/session_id from the correlation
        context automatically.
        """
        from src.core.observability import emit_event as _emit
        fields: Dict[str, Any] = {}
        task_id = None
        if task is not None:
            task_id = task.id
            fields.update({
                "task_type": getattr(task.type, "value", str(task.type)),
                "priority": getattr(task.priority, "value", str(task.priority)),
                "status": getattr(task.status, "value", str(task.status)),
            })
        if extra:
            fields.update(extra)
        task_id = fields.pop("task_id", task_id)
        _emit(name, task_id=task_id, **fields)

    def _emit_turn_telemetry(
        self,
        name: str,
        task: Task,
        attributes: Optional[Dict[str, Any]] = None,
        *,
        invocation_id: Optional[str] = None,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        flush: bool = False,
    ) -> None:
        """Best-effort normalized telemetry through the controller sink."""
        try:
            from src.core.telemetry import EMITTER_PROCESS_INSTANCE_ID, build_event
            session_id = str((task.metadata or {}).get("session_id") or "") or None
            session = self.session_store.get(session_id) if session_id else None
            event_attributes = dict(attributes or {})
            if (
                name == "turn.started"
                and session is not None
                and session.backend_session_id
            ):
                event_attributes.setdefault(
                    "backend_session_id_start", session.backend_session_id
                )
            elif (
                name == "turn.completed"
                and session is not None
                and session.backend_session_id
            ):
                event_attributes.setdefault(
                    "backend_session_id_end", session.backend_session_id
                )
            self._telemetry_sink.emit(
                build_event(
                    name,
                    turn_id=task.id,
                    session_id=session_id,
                    node_id=socket.gethostname(),
                    emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                    source="gateway",
                    invocation_id=invocation_id,
                    backend=backend or (session.backend if session else self._resolve_task_backend(task)),
                    model=model or (session.model if session else None),
                    attributes=event_attributes,
                )
            )
            if name == "turn.started" and session is None:
                self._telemetry_sink.emit(
                    build_event(
                        "telemetry.coverage",
                        turn_id=task.id,
                        session_id=None,
                        node_id=socket.gethostname(),
                        emitter_process_instance_id=EMITTER_PROCESS_INSTANCE_ID,
                        source="gateway",
                        invocation_id=invocation_id,
                        backend=backend or self._resolve_task_backend(task),
                        model=model,
                        attributes={
                            "area": "postprocess",
                            "coverage": "unsupported",
                            "reason_code": "llama_postprocess_uninstrumented",
                            "adapter_version": "gateway-v1",
                        },
                    )
                )
            if flush:
                self._telemetry_sink.flush()
        except Exception:
            logger.warning("event=gateway_telemetry_emit_failed", exc_info=True)
    
    # Simulation execution removed: system now always runs real Claude Code CLI
    
    def create_task_from_description(
        self,
        description: str,
        task_type: Optional[str] = None,
        target_files: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> str:
        """Create and persist a `.task.md` task from a natural language description.

        May use LLAMA to expand metadata; heuristically extracts `cwd` hints.
        Returns the created file path.
        """
        
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        
        # Use simple template for now - can be enhanced with LLAMA later
        parsed = self._parse_description_simple(description)

        # Override task type if provided
        if task_type:
            parsed["type"] = task_type
        
        # Override target files if provided
        if target_files:
            parsed["target_files"] = target_files
        
        # Heuristic: detect inline path hints like "in C:\\Users\\..." or "in /path/..."
        # and inject into frontmatter as `cwd` if allowed by config.
        try:
            import re
            path_hint = None
            # Windows-style absolute path after 'in '
            m = re.search(r"\bin\s+([A-Za-z]:\\[^\n\r]+)", description)
            if m:
                path_hint = m.group(1).strip()
            else:
                # POSIX-like
                m2 = re.search(r"\bin\s+(/[^\n\r]+)", description)
                if m2:
                    path_hint = m2.group(1).strip()
            if path_hint:
                parsed.setdefault("metadata", {})["cwd"] = path_hint
        except Exception:
            pass

        explicit_cwd = (cwd or parsed.get("metadata", {}).get("cwd") or "").strip()
        if explicit_cwd:
            resolved_cwd = PathResolver.from_config().resolve_execution_path(explicit_cwd)
            if resolved_cwd:
                parsed.setdefault("metadata", {})["cwd"] = resolved_cwd
            else:
                parsed.setdefault("metadata", {})["cwd"] = ""

        # Create task file
        task_content = f"""---
id: {task_id}
type: {parsed.get('type', 'analyze')}
priority: {parsed.get('priority', 'medium')}
created: {datetime.now().isoformat()}
cwd: {parsed.get('metadata', {}).get('cwd', '')}
session_id: {session_id or ""}
---

# {parsed.get('title', 'Auto-generated Task')}

**Target Files:**
{chr(10).join('- ' + f for f in parsed.get('target_files', []))}

**Prompt:**
{parsed.get('prompt', description)}

**Success Criteria:**
- [ ] Task completed successfully
- [ ] Results validated
- [ ] Documentation updated if needed

**Context:**
Generated from user description: {description}
"""
        
        # Write task file atomically: write to tmp then rename to final
        tasks_dir = Path(config.system.tasks_dir)
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_file = tasks_dir / f"{task_id}.task.md"
        tmp_file = tasks_dir / f".{task_id}.task.tmp"
        tmp_file.write_text(task_content, encoding='utf-8')
        try:
            tmp_file.replace(task_file)
        except Exception:
            # Fallback to writing directly if replace fails
            task_file.write_text(task_content, encoding='utf-8')
        
        logger.info(f"Created task file: {task_file}")

        # Directly trigger processing so we don't rely solely on the file watcher.
        # On Windows, watchdog can miss the atomic rename event. Calling
        # _handle_new_task_file here ensures the task is always picked up even
        # if the watcher fires late or not at all.
        if self.running:
            asyncio.ensure_future(self._handle_new_task_file(str(task_file)))

        return task_id

    def _parse_description_simple(self, description: str) -> Dict[str, Any]:
        """Minimal task wrapper around a raw user instruction."""
        return {
            "type": "analyze",
            "title": f"Task: {description[:50]}...",
            "prompt": description,
            "priority": "medium",
            "target_files": []
        }
    
    async def _send_task_heartbeats(
        self,
        task: Task,
        session: Optional[Any],
        start_time: float,
        interval_sec: int,
        timeout_s: int,
    ) -> None:
        """Send periodic "still working" messages via the notifier."""
        chat_id = session.telegram_chat_id if session else None
        if not chat_id:
            return
        try:
            await asyncio.sleep(interval_sec)
            while True:
                elapsed = time.time() - start_time
                elapsed_min = int(elapsed // 60)
                remaining = timeout_s - elapsed if timeout_s else 0
                remaining_min = max(0, int(remaining // 60))

                await self.notifier.notify_heartbeat(
                    task.id,
                    session=session,
                    chat_id=chat_id,
                    elapsed_min=elapsed_min,
                    remaining_min=remaining_min,
                )
                await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            pass

    def _mesh_status(self) -> Dict[str, Any]:
        """Return operator-facing mesh mode without probing live services."""
        online_nodes: Optional[int] = None
        total_nodes: Optional[int] = None
        db_available: bool = False
        if config.mesh.enabled:
            try:
                from src.control.db import get_db
                db = get_db()
                db_available = db is not None
                if db is not None:
                    online_nodes = len(db.list_nodes(status="online"))
                    total_nodes = len(db.list_nodes())
            except Exception:
                db_available = False

        if not config.mesh.enabled:
            task_server_mode = "off"
        elif config.mesh.embedded_server:
            task_server_mode = "embedded-running" if self._embedded_task_server is not None else "embedded-configured"
        else:
            task_server_mode = "standalone"

        return {
            "enabled": bool(config.mesh.enabled),
            "task_server_mode": task_server_mode,
            "embedded_server": bool(config.mesh.embedded_server),
            "local_worker_capacity": len(self.worker_tasks),
            "configured_worker_capacity": int(config.system.max_concurrent_tasks),
            "fallback_capacity": len(self.worker_tasks) > 0,
            "db_available": db_available,
            "online_nodes": online_nodes,
            "total_nodes": total_nodes,
            "session_affinity_required": True,
        }

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive orchestrator status"""
        resolver = PathResolver.from_config()
        return {
            "running": self.running,
            "components": self.component_status,
            "tasks": {
                "active": len(self.active_tasks),
                "queued": self.task_queue.qsize(),
                "completed": len(self.task_results),
                "workers": len(self.worker_tasks)
            },
            "llama_status": self.llama_mediator.get_status(probe=False),
            "telegram": {
                "configured": bool(self.telegram_interface),
                "running": bool(self.telegram_interface and self.telegram_interface.is_running),
            },
            "mesh": self._mesh_status(),
            "scope": {
                "base_cwd": getattr(config.claude, "base_cwd", None),
                "allowed_root": getattr(config.claude, "allowed_root", None),
                "root_dirs": resolver.list_root_directories(limit=10),
            },
        }


    def _write_session_summary(self, session, result: TaskResult) -> None:
        """Write/overwrite a compact human-readable summary for a session."""
        try:
            # Same project-root anchor as SessionStore
            project_root = Path(__file__).resolve().parent.parent
            summaries_dir = project_root / "state" / "summaries"
            summaries_dir.mkdir(parents=True, exist_ok=True)
            files = result.files_modified or []
            files_section = "\n".join(f"- {f}" for f in files[:30]) if files else "(none)"
            lines = [
                f"# Session {session.session_id}",
                f"Backend: {session.backend}  |  Status: {session.status.value}",
                f"Path: {session.repo_path}",
                f"Updated: {session.updated_at}",
                f"Backend session: {session.backend_session_id or '(not yet captured)'}",
                "",
                f"## Last instruction",
                session.last_user_message or "(none)",
                "",
                f"## Last result (tail)",
                session.last_result_summary or "(none)",
                "",
                f"## Changed files",
                files_section,
                "",
                f"## Last artifact",
                session.last_artifact_path or "(none)",
            ]
            (summaries_dir / f"{session.session_id}.md").write_text(
                "\n".join(lines), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"session_summary_write_failed id={session.session_id} error={e}")

    def _append_session_event(self, session_id: str, task_id: str, result: TaskResult) -> None:
        """Append one line to the per-session event log."""
        try:
            import json as _json
            log_dir = Path(config.system.logs_dir) / "session_events"
            log_dir.mkdir(parents=True, exist_ok=True)
            entry = _json.dumps({
                "timestamp": datetime.now().isoformat(),
                "task_id": task_id,
                "success": result.success,
                "execution_time": result.execution_time,
                "error": result.errors[0] if result.errors else "",
            }, ensure_ascii=False)
            with (log_dir / f"{session_id}.log").open("a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception as e:
            logger.warning(f"session_event_log_failed id={session_id} error={e}")

    @staticmethod
    def _extract_tool_summary(raw_stdout: str) -> dict:
        """Count tool calls by name and collect Bash commands from Claude Code's JSONL stdout."""
        counts: dict = {}
        bash_commands: list = []
        for line in raw_stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
                blocks = []
                if ev.get("type") == "assistant":
                    blocks = ev.get("message", {}).get("content") or []
                elif ev.get("type") == "tool_use":
                    blocks = [ev]
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "unknown")
                    counts[name] = counts.get(name, 0) + 1
                    if name == "Bash":
                        cmd = (block.get("input") or {}).get("command", "")
                        if cmd:
                            bash_commands.append(cmd)
            except Exception:
                pass
        return {"calls": counts, "total": sum(counts.values()), "bash_commands": bash_commands}


    # ------------------------------------------------------------------
    # Mesh routing
    # ------------------------------------------------------------------

    async def _run_backend_local(
        self,
        task: "Task",
        session: Optional[Any],
        backend_name: str,
    ) -> "TaskResult":
        """Execute the task on this machine using the local backend pool.

        This is the existing execution path extracted so that
        `_dispatch_or_run_local` can call it when mesh routing is off or no
        capable remote node is available.
        """
        from src.core.interfaces import ExecutionResult as _ER
        backend = self._backends.get(backend_name, self._backends["claude"])
        start = time.time()
        cancel_ev = self._task_cancel_events.get(task.id)

        if session:
            session.last_user_message = task.prompt
            if session.backend_session_id:
                exec_task = asyncio.create_task(
                    asyncio.to_thread(backend.resume_session, session, task.prompt)
                )
            else:
                exec_task = asyncio.create_task(
                    asyncio.to_thread(backend.create_session, session)
                )
        else:
            cwd_override = str((task.metadata or {}).get("cwd") or "").strip()
            if not cwd_override:
                cwd_override = str(getattr(config.claude, "base_cwd", "") or "").strip()
            exec_task = asyncio.create_task(
                asyncio.to_thread(backend.run_oneoff, cwd_override, task.prompt)
            )

        self._running_exec_tasks[task.id] = exec_task

        wait_set = {exec_task}
        cancel_waiter: Optional[asyncio.Task] = None
        if cancel_ev is not None:
            cancel_waiter = asyncio.create_task(cancel_ev.wait())
            wait_set.add(cancel_waiter)

        try:
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if cancel_waiter and not cancel_waiter.done():
                cancel_waiter.cancel()

        if exec_task in done:
            raw = exec_task.result()
            if isinstance(raw, _ER):
                if session and (
                    raw.backend_session_id
                    or session.cache_health != "unknown"
                    or session.driver_status
                ):
                    if raw.backend_session_id:
                        session.backend_session_id = raw.backend_session_id
                    self.session_store.save(session)
                result = TaskResult(
                    task_id=task.id,
                    success=raw.success,
                    output=raw.output,
                    errors=raw.errors,
                    files_modified=raw.files_modified,
                    execution_time=raw.execution_time,
                    timestamp=datetime.now().isoformat(),
                    file_changes=getattr(raw, "file_changes", []),
                    raw_stdout=getattr(raw, "raw_stdout", ""),
                    raw_stderr=getattr(raw, "raw_stderr", ""),
                    parsed_output=getattr(raw, "parsed_output", None),
                    return_code=getattr(raw, "return_code", 0),
                )
                setattr(result, "backend_name", backend_name)
                return result
            setattr(raw, "backend_name", backend_name)
            return raw

        # Cancel signal
        if session:
            import contextlib
            with contextlib.suppress(Exception):
                backend.cancel(session)
        exec_task.cancel()
        import contextlib
        with contextlib.suppress(asyncio.CancelledError):
            await exec_task
        result = TaskResult(
            task_id=task.id,
            success=False,
            output="",
            errors=["cancelled"],
            files_modified=[],
            execution_time=time.time() - start,
            timestamp=datetime.now().isoformat(),
        )
        setattr(result, "backend_name", backend_name)
        return result

    def _dispatch_remote_close(self, session: Any) -> None:
        """Enqueue a fire-and-forget close_session task pinned to the session's
        owning node so the remote worker tears down its live backend session and
        frees the claude process.

        Injected into SessionService: a mesh /close used to be a no-op on the
        worker (event=session_backend_close_remote_skipped), leaking the process.
        This does NOT block the caller — the worker claims the pending task on its
        next poll. If the node is offline the task simply waits; the worker's boot
        reaper reclaims the process on restart regardless, so no leak survives.
        """
        machine_id = getattr(session, "machine_id", "") or ""
        if not machine_id:
            return
        from src.control.db import get_db
        db = get_db()
        if db is None:
            logger.warning(
                "event=remote_close_no_db session_id=%s node=%s",
                getattr(session, "session_id", ""), machine_id,
            )
            return
        session_id = getattr(session, "session_id", "") or ""
        backend = getattr(session, "backend", "") or "claude"
        payload = {
            "session": {
                "session_id": session_id,
                "backend": backend,
                "backend_session_id": getattr(session, "backend_session_id", "") or "",
                "machine_id": machine_id,
            }
        }
        task_id = f"close-{session_id}-{uuid.uuid4().hex[:8]}"
        try:
            db.enqueue_task(
                task_id=task_id,
                session_id=session_id,
                machine_id=machine_id,
                backend=backend,
                action="close_session",
                payload=payload,
            )
        except Exception as e:
            logger.warning(
                "event=remote_close_enqueue_failed session_id=%s node=%s err=%s",
                session_id, machine_id, e,
            )
            return
        logger.info(
            "event=remote_close_enqueued session_id=%s node=%s task_id=%s",
            session_id, machine_id, task_id,
        )

    async def _dispatch_to_node(
        self,
        task: "Task",
        session: Optional[Any],
        node: Any,
    ) -> "TaskResult":
        """Enqueue task on the DB as pending (it is already enqueued by _mesh_enqueue_task).

        Then poll the DB until it reaches completed/failed status, up to
        `mesh.oneoff_queue_timeout_sec` seconds. Once a worker claims it, the
        queue timeout no longer applies; the worker owns execution and we wait
        for the terminal DB state.

        The session dict is embedded in the payload by _mesh_enqueue_task so
        the worker can reconstruct the Session object.
        """
        import asyncio as _aio
        from src.control.db import get_db

        db = get_db()
        if db is None:
            # DB unavailable: cannot dispatch to worker. Fail loudly rather
            # than silently falling back to local execution, which would break
            # backend_session_id continuity for machine-pinned sessions.
            result = TaskResult(
                task_id=task.id,
                success=False,
                output="",
                errors=["Mesh DB unavailable; cannot dispatch to remote worker"],
                files_modified=[],
                execution_time=0.0,
                timestamp=datetime.now().isoformat(),
            )
            setattr(result, "backend_name", self._resolve_task_backend(task))
            return result

        pickup_timeout_sec = getattr(config.mesh, "oneoff_queue_timeout_sec", 600)
        pickup_deadline = time.time() + pickup_timeout_sec
        poll_interval = 3.0
        first_poll = True
        target_node_id = getattr(node, "node_id", None) or (session.machine_id if session else "")
        await _aio.to_thread(self._nudge_worker_for_dispatch, node, target_node_id, db)

        while True:
            row = db.get_task(task.id)
            if row is None and first_poll:
                # Row not found on the very first poll — _mesh_enqueue_task
                # must have failed silently. Fail fast instead of burning the
                # full timeout (up to 600s) before the user sees an error.
                result = TaskResult(
                    task_id=task.id,
                    success=False,
                    output="",
                    errors=["Task row missing from DB — enqueue failed before dispatch; check logs for mesh_enqueue_failed"],
                    files_modified=[],
                    execution_time=0.0,
                    timestamp=datetime.now().isoformat(),
                )
                setattr(result, "backend_name", self._resolve_task_backend(task))
                return result
            first_poll = False
            if row:
                status = row.get("status", "pending")
                if status == "completed":
                    result_raw = row.get("result")
                    try:
                        r = json.loads(result_raw) if isinstance(result_raw, str) else (result_raw or {})
                    except Exception:
                        r = {}
                    # Propagate the worker's backend_session_id so the next
                    # turn can resume the remote-side backend session.
                    new_bsid = r.get("backend_session_id", "")
                    if session:
                        changed = False
                        if new_bsid:
                            session.backend_session_id = new_bsid
                            changed = True
                        for attr in ("driver_type", "driver_status", "cache_health"):
                            value = r.get(attr)
                            if value is not None:
                                setattr(session, attr, value)
                                changed = True
                        if "cache_unhealthy_count" in r:
                            session.cache_unhealthy_count = int(r.get("cache_unhealthy_count") or 0)
                            changed = True
                        if "previous_backend_session_ids" in r:
                            session.previous_backend_session_ids = r.get("previous_backend_session_ids") or []
                            changed = True
                        if changed:
                            self.session_store.save(session)
                    worker_output = r.get("output", "")
                    result = TaskResult(
                        task_id=task.id,
                        success=r.get("success", True),
                        output=worker_output,
                        errors=r.get("errors") or [],
                        files_modified=r.get("files_modified") or [],
                        execution_time=r.get("execution_time", 0.0),
                        timestamp=r.get("timestamp", datetime.now().isoformat()),
                        return_code=r.get("return_code", 0),
                        # The worker only ships `output` over the wire; mirror it
                        # into raw_stdout so the artifact JSON (which persists
                        # raw_stdout, not output) captures the full remote result
                        # rather than an empty field (T2).
                        raw_stdout=worker_output,
                    )
                    setattr(result, "usage", r.get("usage"))
                    setattr(result, "backend_name", row.get("backend", "claude"))
                    setattr(
                        result,
                        "telemetry_invocation_id",
                        r.get("telemetry_invocation_id") or None,
                    )
                    return result

                if status in ("failed", "failed_node_offline"):
                    result_raw = row.get("result")
                    try:
                        r = json.loads(result_raw) if isinstance(result_raw, str) else (result_raw or {})
                    except Exception:
                        r = {}
                    if session and r:
                        changed = False
                        for attr in ("driver_type", "driver_status", "cache_health"):
                            value = r.get(attr)
                            if value is not None:
                                setattr(session, attr, value)
                                changed = True
                        if "cache_unhealthy_count" in r:
                            session.cache_unhealthy_count = int(r.get("cache_unhealthy_count") or 0)
                            changed = True
                        if "previous_backend_session_ids" in r:
                            session.previous_backend_session_ids = r.get("previous_backend_session_ids") or []
                            changed = True
                        if changed:
                            self.session_store.save(session)
                    error_msg = row.get("error") or f"Task {status}"
                    error_detail = (r.get("error_detail") if r else "") or ""
                    result = TaskResult(
                        task_id=task.id,
                        success=False,
                        output=r.get("output", "") if r else "",
                        errors=r.get("errors") or [error_msg],
                        files_modified=r.get("files_modified") or [],
                        execution_time=r.get("execution_time", 0.0),
                        timestamp=r.get("timestamp", datetime.now().isoformat()) if r else datetime.now().isoformat(),
                        return_code=r.get("return_code", 1) if r else 1,
                        raw_stdout=r.get("output", "") if r else "",
                        raw_stderr=error_detail,
                    )
                    setattr(result, "error_detail", error_detail)
                    setattr(result, "usage", r.get("usage") if r else None)
                    setattr(result, "backend_name", row.get("backend", "claude"))
                    setattr(
                        result,
                        "telemetry_invocation_id",
                        r.get("telemetry_invocation_id") if r else None,
                    )
                    return result

                if status != "claimed" and time.time() >= pickup_deadline:
                    db.fail_task(task.id, f"dispatch timeout after {pickup_timeout_sec}s waiting for worker")
                    result = TaskResult(
                        task_id=task.id,
                        success=False,
                        output="",
                        errors=[f"Dispatch timeout: no worker picked up the task within {pickup_timeout_sec}s"],
                        files_modified=[],
                        execution_time=pickup_timeout_sec,
                        timestamp=datetime.now().isoformat(),
                    )
                    setattr(result, "backend_name", self._resolve_task_backend(task))
                    return result

                # status == claimed: a worker has picked up the task. Do not
                # apply the pickup timeout to execution time; wait for the
                # worker's real completed/failed state or an offline update.
            elif time.time() >= pickup_deadline:
                result = TaskResult(
                    task_id=task.id,
                    success=False,
                    output="",
                    errors=["Task row disappeared from DB while waiting for worker pickup"],
                    files_modified=[],
                    execution_time=pickup_timeout_sec,
                    timestamp=datetime.now().isoformat(),
                )
                setattr(result, "backend_name", self._resolve_task_backend(task))
                return result

            # Check for cancellation
            cancel_ev = self._task_cancel_events.get(task.id)
            if cancel_ev and cancel_ev.is_set():
                # Distinguish a genuine user cancel from a gateway shutdown.
                # On shutdown we are only *detaching* our poll loop — the remote
                # worker keeps running and owns the task's real terminal state in
                # the DB. Writing fail_task here would fabricate a 'failed' row
                # that overwrites the worker's truth, which is exactly the
                # restart-cancel bug. So on shutdown we leave the DB row as-is
                # (still 'claimed') and return a non-terminal detached result;
                # startup recovery (_recover_stale_busy_sessions) reattaches and
                # reports whatever the worker actually wrote.
                interrupted = task.id in self._shutdown_interrupted_tasks
                if not interrupted:
                    db.fail_task(task.id, "cancelled by gateway")
                result = TaskResult(
                    task_id=task.id,
                    success=False,
                    output="",
                    errors=["interrupted by gateway restart" if interrupted else "cancelled"],
                    files_modified=[],
                    execution_time=0.0,
                    timestamp=datetime.now().isoformat(),
                )
                setattr(result, "backend_name", self._resolve_task_backend(task))
                setattr(result, "detached", interrupted)
                return result

            await _aio.sleep(poll_interval)

    def _nudge_worker_for_dispatch(self, node: Any, node_id: str, db: Any) -> bool:
        """Best-effort wake-up for a worker after enqueuing remote work.

        Prefers the in-memory node object (avoids a DB round-trip when we
        already have fresh registration data). Falls back to a DB lookup when
        the node object is absent or lacks address fields.
        """
        import urllib.request

        # Fast path: use the in-memory node's address when available.
        tailscale_ip = getattr(node, "tailscale_ip", None) or ""
        api_port = getattr(node, "api_port", None) or 0
        if tailscale_ip and api_port:
            try:
                url = f"http://{tailscale_ip}:{api_port}/nudge"
                req = urllib.request.Request(url, method="POST", data=b"")
                with urllib.request.urlopen(req, timeout=2):
                    pass
                return True
            except Exception as e:
                import logging as _logging
                _logging.getLogger(__name__).debug(
                    "event=nudge_failed node_id=%s err=%s", node_id, e
                )
                return False

        # Slow path: look up from DB (covers cases where node=None or has no address).
        from src.control.node_inspector import nudge_node_direct
        return nudge_node_direct(node_id, db)

    async def _refresh_capable_nodes_before_routing(self, registry: Any, backend_name: str) -> None:
        """Nudge capable workers and briefly wait for fresher live_state."""
        import asyncio as _aio
        from src.control.db import get_db

        wait_sec = float(getattr(config.mesh, "routing_freshness_wait_sec", 2.0) or 0.0)
        if wait_sec <= 0:
            return

        try:
            candidates = registry.list_capable(backend_name)
        except Exception:
            candidates = []
        if not candidates:
            return

        before = {
            node.node_id: node.live_state_updated_at
            for node in candidates
        }
        db = get_db()
        await _aio.gather(
            *[
                _aio.to_thread(self._nudge_worker_for_dispatch, node, node.node_id, db)
                for node in candidates
            ],
            return_exceptions=True,
        )

        deadline = time.time() + wait_sec
        while time.time() < deadline:
            for node in candidates:
                if node.live_state_updated_at and node.live_state_updated_at != before.get(node.node_id):
                    logger.debug(
                        "event=mesh_preroute_fresh_state node_id=%s backend=%s",
                        node.node_id,
                        backend_name,
                    )
                    return
            await _aio.sleep(0.1)

    async def _dispatch_or_run_local(
        self,
        task: "Task",
        session: Optional[Any],
        backend_name: str,
    ) -> "TaskResult":
        """Route task to a worker node or fall back to local execution.

        `MESH_ENABLED=false` (default) → always runs locally, zero regression.
        `MESH_ENABLED=true`            → routes through node registry when nodes
                                         are available; falls back to local if not.
        """
        from src.control.node_registry import get_registry

        if not config.mesh.enabled:
            return await self._run_backend_local(task, session, backend_name)

        registry = get_registry()
        if registry.is_empty():
            return await self._run_backend_local(task, session, backend_name)

        def _routing_failure(msg: str) -> "TaskResult":
            result = TaskResult(
                task_id=task.id,
                success=False,
                output="",
                errors=[msg],
                files_modified=[],
                execution_time=0.0,
                timestamp=datetime.now().isoformat(),
            )
            setattr(result, "backend_name", backend_name)
            return result

        if session and session.machine_id:
            node = registry.get(session.machine_id)
            if not node or node.status != "online":
                return _routing_failure(f"Node {session.machine_id!r} is offline; cannot continue session")
        else:
            await self._refresh_capable_nodes_before_routing(registry, backend_name)
            node = registry.pick_capable(
                backend=backend_name,
                max_live_state_age_sec=getattr(config.mesh, "routing_live_state_max_age_sec", 90),
            )
            if not node:
                return _routing_failure(
                    f"No online node supports backend {backend_name!r} with available capacity"
                )

        return await self._dispatch_to_node(task, session, node)

    # ------------------------------------------------------------------
    # Mesh DB shadow-write helpers
    # ------------------------------------------------------------------

    def _mesh_enqueue_task(self, task: Task, backend_name: str) -> None:
        """Shadow-write a dispatched task into mesh_tasks.

        Two cases (the split MUST match `process_task`'s local/remote decision,
        which routes remote ⟺ machine_id is set AND machine_id != this host):

        Local execution (no machine_id, OR machine_id names THIS host, OR
        MESH_ENABLED=false):
          Insert + immediately self-claim under this host's identity so no
          worker daemon can pick up the row as claimable work. The row is a
          faithful historical mirror; `_mesh_complete_task` finalises it.

        Remote dispatch (machine_id names a DIFFERENT host and MESH_ENABLED=true):
          Insert as 'pending' WITHOUT self-claiming. The row is the actual
          dispatch signal — the pinned worker polls `get_pending_tasks`, sees
          it (machine_id filter matches), claims it, executes, and posts the
          result. `process_task` (via `_dispatch_to_node`) polls the DB for
          completion. `_mesh_complete_task` later enriches the row with the
          local artifact_path.

        BUGFIX: the self-claim used to fire only when machine_id was UNSET, so a
        session pinned to THIS host (e.g. a standalone worker daemon sharing the
        gateway's hostname as its node_id) left the row 'pending' AND was run
        locally by `process_task` — the daemon then claimed the same row and ran
        it a SECOND time (two agents for one task). Treating machine_id == host as
        local closes that: the gateway owns host-local execution, single-writer.
        """
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return
            session_id = (task.metadata or {}).get("session_id", "").strip() or None
            session = self.session_store.get(session_id) if session_id else None
            machine_id = (session.machine_id or None) if session else None
            host = socket.gethostname()
            action_override = (task.metadata or {}).get("task_type", "")
            if action_override == "fetch_staged_file":
                action = "fetch_staged_file"
            elif session_id and session and not session.backend_session_id:
                action = "create_session"
            elif session_id:
                action = "resume_session"
            else:
                action = "run_oneoff"
            payload = {
                "prompt": task.prompt,
                "task_id": task.id,
                "action": action,
                "metadata": task.metadata or {},
                "telemetry": {
                    "schema_version": 1,
                    "turn_id": task.id,
                    "session_id": session_id,
                    "gateway_node_id": host,
                    "attempt": 1,
                    "spawn_reason": "initial",
                },
            }
            if session:
                payload["session"] = {
                    "session_id": session.session_id,
                    "backend": session.backend,
                    "repo_path": session.repo_path,
                    "backend_session_id": session.backend_session_id,
                    "model": session.model,
                    "machine_id": session.machine_id,
                    "telegram_chat_id": session.telegram_chat_id,
                    "telegram_thread_id": session.telegram_thread_id,
                    "owner_user_id": session.owner_user_id,
                    "last_user_message": session.last_user_message,
                    "driver_type": session.driver_type,
                    "driver_status": session.driver_status,
                    "cache_health": session.cache_health,
                    "cache_unhealthy_count": session.cache_unhealthy_count,
                    "previous_backend_session_ids": session.previous_backend_session_ids or [],
                }
            # Runs on THIS host ⟺ no pin, or the pin names this host. Only a pin
            # to a DIFFERENT host is a true remote dispatch. This MUST mirror
            # process_task's `_pinned_elsewhere` test, or a host-pinned task both
            # runs locally AND stays claimable by a daemon → double execution.
            runs_locally = (not machine_id) or (machine_id == host)
            db.enqueue_task(
                task_id=task.id,
                session_id=session_id,
                machine_id=machine_id,
                backend=backend_name,
                action=action,
                payload=payload,
            )
            # Self-claim when this task runs on THIS host so no worker daemon can
            # pick up the row. A row pinned to a DIFFERENT host stays 'pending' so
            # that remote worker can claim it via get_pending_tasks.
            if runs_locally:
                if not db.claim_task(task.id, host):
                    logger.warning(
                        "event=mesh_self_claim_failed task_id=%s host=%s — "
                        "row may be claimable by a remote worker",
                        task.id, host,
                    )
        except Exception as e:
            if machine_id and machine_id != host:
                # Remote dispatch depends on this row existing — log loudly so
                # the operator sees it immediately rather than after a 600s poll timeout.
                logger.error(
                    "event=mesh_enqueue_failed task_id=%s machine_id=%s err=%s — "
                    "worker will never see this task; dispatch will timeout",
                    task.id, machine_id, e,
                )
            else:
                logger.debug("event=mesh_enqueue_failed task_id=%s err=%s", task.id, e)

    def _mesh_reconcile_dir(self) -> Path:
        return Path(config.system.results_dir) / "reconcile"

    def _spool_mesh_completion_reconcile(
        self,
        task: Task,
        result: "TaskResult",
        artifact_path: Optional[str],
        reason: str,
    ) -> None:
        """Persist a completed task for later DB reconciliation."""
        try:
            spool_dir = self._mesh_reconcile_dir()
            spool_dir.mkdir(parents=True, exist_ok=True)
            payload: Dict[str, Any] = {
                "schema_version": 1,
                "task": {
                    "id": task.id,
                    "type": getattr(task.type, "value", str(task.type)),
                    "priority": getattr(task.priority, "value", str(task.priority)),
                    "status": getattr(task.status, "value", str(task.status)),
                    "created": task.created,
                    "title": task.title,
                    "target_files": list(task.target_files or []),
                    "prompt": task.prompt or "",
                    "success_criteria": list(task.success_criteria or []),
                    "context": task.context or "",
                    "metadata": task.metadata or {},
                },
                "result": {
                    "task_id": result.task_id,
                    "success": result.success,
                    "output": result.output or "",
                    "errors": list(result.errors or []),
                    "files_modified": list(result.files_modified or []),
                    "execution_time": result.execution_time,
                    "timestamp": result.timestamp,
                    "file_changes": list(getattr(result, "file_changes", None) or []),
                    "raw_stdout": getattr(result, "raw_stdout", "") or "",
                    "raw_stderr": getattr(result, "raw_stderr", "") or "",
                    "parsed_output": getattr(result, "parsed_output", None),
                    "return_code": getattr(result, "return_code", 0),
                    "usage": getattr(result, "usage", None),
                    "retries": getattr(result, "retries", 0),
                    "error_class": getattr(result, "error_class", "") or "",
                    "backend_name": getattr(result, "backend_name", ""),
                },
                "artifact_path": artifact_path or "",
                "reason": reason,
                "created_at": datetime.now().isoformat(),
                "reconciled": False,
            }
            (spool_dir / f"{task.id}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.warning(
                "event=mesh_completion_reconcile_spooled task_id=%s reason=%s",
                task.id,
                reason,
            )
        except Exception as e:
            logger.error(
                "event=mesh_completion_reconcile_spool_failed task_id=%s err=%s",
                task.id,
                e,
            )

    def _task_from_reconcile_payload(self, payload: Dict[str, Any]) -> Task:
        data = payload.get("task") or {}
        try:
            task_type = TaskType(data.get("type") or TaskType.FIX.value)
        except Exception:
            task_type = TaskType.FIX
        try:
            priority = TaskPriority(data.get("priority") or TaskPriority.MEDIUM.value)
        except Exception:
            priority = TaskPriority.MEDIUM
        try:
            status = TaskStatus(data.get("status") or TaskStatus.COMPLETED.value)
        except Exception:
            status = TaskStatus.COMPLETED
        return Task(
            id=str(data.get("id") or ""),
            type=task_type,
            priority=priority,
            status=status,
            created=str(data.get("created") or datetime.now().isoformat()),
            title=str(data.get("title") or data.get("id") or "reconciled task"),
            target_files=list(data.get("target_files") or []),
            prompt=str(data.get("prompt") or ""),
            success_criteria=list(data.get("success_criteria") or []),
            context=str(data.get("context") or ""),
            metadata=dict(data.get("metadata") or {}),
        )

    def _result_from_reconcile_payload(self, payload: Dict[str, Any]) -> TaskResult:
        data = payload.get("result") or {}
        result = TaskResult(
            task_id=str(data.get("task_id") or (payload.get("task") or {}).get("id") or ""),
            success=bool(data.get("success")),
            output=str(data.get("output") or ""),
            errors=list(data.get("errors") or []),
            files_modified=list(data.get("files_modified") or []),
            execution_time=float(data.get("execution_time") or 0.0),
            timestamp=str(data.get("timestamp") or datetime.now().isoformat()),
            file_changes=list(data.get("file_changes") or []),
            raw_stdout=str(data.get("raw_stdout") or ""),
            raw_stderr=str(data.get("raw_stderr") or ""),
            parsed_output=data.get("parsed_output"),
            return_code=int(data.get("return_code") or 0),
            usage=data.get("usage"),
            retries=int(data.get("retries") or 0),
            error_class=str(data.get("error_class") or ""),
        )
        backend_name = str(data.get("backend_name") or "")
        if backend_name:
            setattr(result, "backend_name", backend_name)
        return result

    def _ensure_reconcile_task_row(self, db: Any, task: Task, result: TaskResult) -> None:
        if db.get_task(task.id):
            return
        metadata: Dict[str, Any] = task.metadata or {}
        session_id = str(metadata.get("session_id") or "").strip() or None
        backend_name = str(getattr(result, "backend_name", "") or metadata.get("backend") or "claude")
        action = "resume_session" if session_id else "run_oneoff"
        machine_id: Optional[str] = None
        if session_id:
            try:
                session = self.session_store.get(session_id)
                if session:
                    machine_id = session.machine_id or None
            except Exception:
                machine_id = None
        db.enqueue_task(
            task_id=task.id,
            session_id=session_id,
            machine_id=machine_id,
            backend=backend_name,
            action=action,
            payload={
                "prompt": task.prompt,
                "task_id": task.id,
                "action": action,
                "metadata": metadata,
            },
        )

    def reconcile_spooled_mesh_completions(self, limit: int = 100) -> Dict[str, int]:
        """Replay completed task DB mirrors that were spooled during DB outage."""
        if self._mesh_reconcile_in_progress:
            return {"checked": 0, "reconciled": 0, "failed": 0}
        try:
            from src.control.db import get_db
            db = get_db()
        except Exception:
            return {"checked": 0, "reconciled": 0, "failed": 0}
        if db is None:
            return {"checked": 0, "reconciled": 0, "failed": 0}

        spool_dir = self._mesh_reconcile_dir()
        if not spool_dir.exists():
            return {"checked": 0, "reconciled": 0, "failed": 0}

        checked: int = 0
        reconciled: int = 0
        failed: int = 0
        self._mesh_reconcile_in_progress = True
        try:
            for path in sorted(spool_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
                if checked >= limit:
                    break
                checked += 1
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if payload.get("reconciled"):
                        continue
                    task = self._task_from_reconcile_payload(payload)
                    result = self._result_from_reconcile_payload(payload)
                    if not task.id or not result.task_id:
                        raise ValueError("missing task_id")
                    self._ensure_reconcile_task_row(db, task, result)
                    self._mesh_complete_task(task, result, payload.get("artifact_path") or None)
                    row = db.get_task(task.id)
                    if row and row.get("status") in {"completed", "failed", "failed_node_offline"}:
                        payload["reconciled"] = True
                        payload["reconciled_at"] = datetime.now().isoformat()
                        path.write_text(
                            json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        reconciled += 1
                    else:
                        failed += 1
                except Exception as e:
                    failed += 1
                    logger.warning(
                        "event=mesh_completion_reconcile_failed path=%s err=%s",
                        path,
                        e,
                    )
        finally:
            self._mesh_reconcile_in_progress = False

        if checked:
            logger.info(
                "event=mesh_completion_reconcile checked=%s reconciled=%s failed=%s",
                checked,
                reconciled,
                failed,
            )
        return {"checked": checked, "reconciled": reconciled, "failed": failed}

    def mesh_reconcile_status(self, limit: int = 1000) -> Dict[str, Any]:
        """Summarize pending DB-completion reconcile spool files for operators."""
        spool_dir = self._mesh_reconcile_dir()
        if not spool_dir.exists():
            return {
                "total": 0,
                "pending": 0,
                "reconciled": 0,
                "invalid": 0,
                "oldest_pending_at": None,
                "latest_reconciled_at": None,
            }

        total: int = 0
        pending: int = 0
        reconciled: int = 0
        invalid: int = 0
        oldest_pending_at: Optional[str] = None
        latest_reconciled_at: Optional[str] = None
        for path in sorted(spool_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            if total >= max(1, limit):
                break
            total += 1
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                invalid += 1
                continue
            if payload.get("reconciled"):
                reconciled += 1
                reconciled_at = str(payload.get("reconciled_at") or "")
                if reconciled_at and (latest_reconciled_at is None or reconciled_at > latest_reconciled_at):
                    latest_reconciled_at = reconciled_at
            else:
                pending += 1
                created_at = str(payload.get("created_at") or "")
                if created_at and (oldest_pending_at is None or created_at < oldest_pending_at):
                    oldest_pending_at = created_at

        return {
            "total": total,
            "pending": pending,
            "reconciled": reconciled,
            "invalid": invalid,
            "oldest_pending_at": oldest_pending_at,
            "latest_reconciled_at": latest_reconciled_at,
        }

    def _handle_proactive_turn(
        self,
        session_id: str,
        task_id: str,
        text: str,
        backend_session_id: str = "",
    ) -> None:
        """Hook invoked by the mesh task server (on its threadpool) when a worker
        reports an autonomous turn. The turn is already persisted to the DB; here
        we marshal the live notification onto the orchestrator loop so the user
        gets actively reached (web push / Telegram) — the "reach back"."""
        loop = getattr(self, "_loop", None)
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._notify_proactive_turn(session_id, task_id, text, backend_session_id),
                loop,
            )
        except Exception as e:
            logger.warning("event=proactive_notify_schedule_failed session_id=%s err=%s", session_id, e)

    async def _notify_proactive_turn(
        self,
        session_id: str,
        task_id: str,
        text: str,
        backend_session_id: str = "",
    ) -> None:
        """Deliver a proactive turn through the same UI-agnostic notifier every
        normal turn uses, so WebUI (SSE), web push and Telegram all inherit it."""
        from types import SimpleNamespace
        try:
            session = self.session_store.get(session_id)
            if session is None:
                return
            # The autonomous turn advanced the live backend session — keep the
            # gateway's record in step so the next resume continues cleanly.
            if backend_session_id and backend_session_id != getattr(session, "backend_session_id", ""):
                session.backend_session_id = backend_session_id
            # Mirror it into the session's file-side history too (the file
            # transcript fallback), flagged so it's never shown with a fake user
            # message.
            try:
                session.task_history.append({
                    "task_id": task_id,
                    "timestamp": datetime.now().isoformat(),
                    "success": True,
                    "execution_time": 0.0,
                    "user_message": "",
                    "result_summary": text,
                    "files_modified": [],
                    "proactive": True,
                })
                session.task_history = session.task_history[-20:]
                session.last_result_summary = text[-400:] if len(text) > 400 else text
                session.last_summary = session.last_result_summary
                self.session_store.save(session)
            except Exception:
                pass
            self._emit_event("proactive_turn", None, {"session_id": session_id, "task_id": task_id})
            result_like = SimpleNamespace(
                success=True,
                output=text,
                files_modified=[],
                parsed_output=None,
                raw_stdout="",
                errors=[],
                usage=None,
                execution_time=0.0,
                error_class="",
                return_code=0,
                timestamp=datetime.now().isoformat(),
            )
            chat_id = getattr(session, "telegram_chat_id", None)
            await self.notifier.notify_task_outcome(
                task_id, result_like, session=session, chat_id=chat_id,
            )
        except Exception as e:
            logger.warning("event=proactive_notify_failed session_id=%s err=%s", session_id, e)

    def _mesh_complete_task(self, task: Task, result: "TaskResult", artifact_path: Optional[str]) -> None:
        """Shadow-write the task result into mesh_tasks — the canonical, file-free store.

        Two writes:
          1. The legacy ``result`` JSON (``output`` still capped at 2000 for the
             small-payload list/preview consumers and back-compat).
          2. ``enrich_task`` with the FULL untruncated reply + structured fields
             (parsed_output, file_changes, usage, prompt) so the DB holds
             everything ``results/task_*.json`` did. This is what lets the chat
             transcript and Files/Info tabs read from the DB and lets the fat
             artifact files be dropped. Only ``raw_stdout`` (debug NDJSON) stays
             on disk, gzipped.
        """
        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                self._spool_mesh_completion_reconcile(task, result, artifact_path, "db unavailable")
                return
            replay = getattr(self, "reconcile_spooled_mesh_completions", None)
            if callable(replay) and not getattr(self, "_mesh_reconcile_in_progress", False):
                replay(limit=25)
            result_dict = {
                "success": result.success,
                "output": result.output[:2000] if result.output else "",  # small preview only
                "errors": result.errors or [],
                "files_modified": result.files_modified or [],
                "execution_time": result.execution_time,
                "timestamp": result.timestamp,
                "return_code": getattr(result, "return_code", 0),
            }
            if result.success:
                db.complete_task(task.id, result_dict, artifact_path)
            else:
                error_str = "; ".join(result.errors) if result.errors else "unknown error"
                db.fail_task(task.id, error_str, result=result_dict, artifact_path=artifact_path)

            # Full artifact-complete enrichment — the file-free conversation store.
            if result.success:
                reply_text = self._session_reply_text(result).strip()
            else:
                # A failed turn may still carry a deliverable reply — e.g. a
                # context-overflow turn that salvaged the agent's real progress
                # (driver builds banner + bounded work into result.output). Prefer
                # that so the user gets the work, not just a terse reason. Fall
                # back to the short failure reason when output is empty.
                salvaged = (getattr(result, "output", "") or "").strip()
                if salvaged:
                    reply_text = salvaged
                else:
                    reply_text = (self._short_failure_reason(result) or "(failed)").strip()
            usage = getattr(result, "usage", None)
            try:
                if usage is None:
                    from src.services.result_text import extract_usage_from_ndjson
                    usage = extract_usage_from_ndjson(getattr(result, "raw_stdout", "") or "")
            except Exception:
                usage = None
            # Prompt: task.prompt is the source for runtime tasks, but for some
            # dispatch paths it's empty while session.last_user_message holds the
            # full instruction (same precedence the file transcript used). Fall
            # back so the user turn is never blank.
            prompt_text = (task.prompt or "").strip()
            if not prompt_text:
                try:
                    sid = (task.metadata or {}).get("session_id", "").strip()
                    if sid:
                        _s = self.session_store.get(sid)
                        if _s:
                            prompt_text = (_s.last_user_message or "").strip()
                except Exception:
                    pass
            db.enrich_task(
                task.id,
                prompt=prompt_text or None,
                reply_text=reply_text,
                parsed_output=getattr(result, "parsed_output", None),
                file_changes=getattr(result, "file_changes", None) or None,
                files_modified=result.files_modified or [],
                usage=usage,
                error_class=getattr(result, "error_class", "") or None,
                return_code=getattr(result, "return_code", None),
            )
        except Exception as e:
            logger.debug("event=mesh_complete_failed task_id=%s err=%s", task.id, e)
            self._spool_mesh_completion_reconcile(task, result, artifact_path, str(e))


class _ContextLoader:
    """Lightweight loader that produces compact, prompt-ready context.

    Reads the canonical `mesh_tasks` row first, then falls back to
    `results/index.json` / `results/{task_id}.json` for old artifacts. Returns a
    small dictionary containing bounded prompt context, summary, constraints,
    usage, and files.
    """

    SUMMARY_LIMIT = 2000
    PROMPT_LIMIT = 1000
    ERROR_LIMIT = 5

    def __init__(
        self,
        index_path: Path,
        results_dir: Path,
        db_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._index_path = index_path
        self._results_dir = results_dir
        self._db_factory = db_factory

    def load(self, task_id: str) -> Dict[str, Any]:
        default: Dict[str, Any] = {
            "task_id": task_id,
            "source": "none",
            "prompt": "",
            "summary": "",
            "constraints": {},
            "files_modified": [],
            "usage": {},
            "errors": [],
        }
        try:
            row = self._load_db_row(task_id)
            if row is not None:
                return self._from_db_row(task_id, row)
            data = self._load_artifact(task_id)
            if data is not None:
                return self._from_artifact(task_id, data)
        except Exception:
            pass
        return default

    def _load_db_row(self, task_id: str) -> Optional[Dict[str, Any]]:
        if self._db_factory is None:
            return None
        try:
            db = self._db_factory()
            if db is None:
                return None
            row = db.get_task(task_id)
            return row if isinstance(row, dict) else None
        except Exception:
            return None

    def _load_artifact(self, task_id: str) -> Optional[Dict[str, Any]]:
        artifact_path: Optional[Path] = None
        if self._index_path.exists():
            try:
                idx = json.loads(self._index_path.read_text(encoding="utf-8"))
                p = idx.get(str(task_id))
                if p:
                    ap = Path(p)
                    if ap.exists():
                        artifact_path = ap
            except Exception:
                artifact_path = None
        if artifact_path is None:
            cand = self._results_dir / f"{task_id}.json"
            if cand.exists():
                artifact_path = cand
        if artifact_path is None or not artifact_path.exists():
            return None
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None

    def _from_db_row(self, task_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
        result = self._json_obj(row.get("result"))
        parsed_output = self._json_obj(row.get("parsed_output_json"))
        files_modified = self._json_list(row.get("files_modified_json")) or self._json_list(
            result.get("files_modified")
        )
        usage = self._json_obj(row.get("usage_json"))
        prompt = self._text(row.get("prompt")) or self._text(self._json_obj(row.get("payload")).get("prompt"))
        summary = (
            self._text(row.get("reply_text"))
            or self._summary_from_parsed(parsed_output)
            or self._text(result.get("output"))
        )
        errors = self._json_list(result.get("errors"))
        status = self._text(row.get("status"))
        prior_success = result.get("success")
        if prior_success is None:
            prior_success = status == "completed"
        return {
            "task_id": task_id,
            "source": "db",
            "prompt": prompt[:self.PROMPT_LIMIT],
            "summary": summary[:self.SUMMARY_LIMIT],
            "constraints": {
                "prior_success": bool(prior_success),
                "status": status,
                "error_class": self._text(row.get("error_class")),
                "return_code": row.get("return_code"),
            },
            "files_modified": files_modified,
            "usage": usage,
            "errors": [self._text(e)[:300] for e in errors[:self.ERROR_LIMIT]],
        }

    def _from_artifact(self, task_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        parsed_output = data.get("parsed_output") if isinstance(data.get("parsed_output"), dict) else {}
        summary = self._summary_from_parsed(parsed_output) or self._text(data.get("output"))
        errors = self._json_list(data.get("errors"))
        return {
            "task_id": task_id,
            "source": "artifact",
            "prompt": self._text(data.get("prompt"))[:self.PROMPT_LIMIT],
            "summary": summary[:self.SUMMARY_LIMIT],
            "constraints": {
                "prior_success": bool(data.get("success")),
                "status": self._text(data.get("status")),
                "error_class": self._text(data.get("error_class")),
                "return_code": data.get("return_code"),
            },
            "files_modified": self._json_list(data.get("files_modified")),
            "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {},
            "errors": [self._text(e)[:300] for e in errors[:self.ERROR_LIMIT]],
        }

    def _summary_from_parsed(self, parsed_output: Dict[str, Any]) -> str:
        content = parsed_output.get("content")
        return content if isinstance(content, str) else ""

    def _json_obj(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                loaded = json.loads(value)
                return loaded if isinstance(loaded, dict) else {}
            except Exception:
                return {}
        return {}

    def _json_list(self, value: Any) -> List[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.strip():
            try:
                loaded = json.loads(value)
                return loaded if isinstance(loaded, list) else []
            except Exception:
                return []
        return []

    def _text(self, value: Any) -> str:
        return value if isinstance(value, str) else ""
