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
from pathlib import Path
from typing import Dict, List, Any, Optional
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
        self.session_service = SessionService(self.session_store)
        self.workflow_service = WorkflowService()
        self._backends = build_backends()
        from src.control.telemetry_sink import build_runtime_telemetry_sink
        self._telemetry_sink = build_runtime_telemetry_sink(
            node_id=socket.gethostname(),
            logs_dir=config.system.logs_dir,
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
        # Job completion polling (T3)
        self._last_job_poll = datetime.now().isoformat()
        # Cancellation and runtime tracking
        self._task_cancel_events: Dict[str, asyncio.Event] = {}
        self._running_exec_tasks: Dict[str, asyncio.Task] = {}
        self._shutdown_interrupted_tasks: set[str] = set()
        self._stale_busy_reconcile_task: Optional[asyncio.Task] = None
        
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

        db = None
        try:
            from src.control.db import get_db
            db = get_db()
        except Exception:
            pass

        for session in self.session_store.list_all():
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
                error_msg = (row.get("error") if row else "") or f"Task {status}"
                session.status = SessionStatus.ERROR
                session.last_result_summary = error_msg[-400:]
                self.session_store.save(session)
                result = TaskResult(
                    task_id=task_id,
                    success=False,
                    output="",
                    errors=[error_msg],
                    files_modified=[],
                    execution_time=0.0,
                    timestamp=datetime.now().isoformat(),
                )
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
            except Exception as e:
                logger.debug("event=job_poller_error err=%s", e)

            try:
                await asyncio.wait_for(asyncio.sleep(30), timeout=30)
            except asyncio.TimeoutError:
                pass

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
        """Queue a task object directly without writing a task file."""
        logger.info(f"event=task_created task_id={task.id} source={(task.metadata or {}).get('source', 'runtime')}")
        self._emit_event("task_created", task, {"source": (task.metadata or {}).get("source", "runtime")})
        self._emit_event("parsed", task)
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

    async def submit_instruction(
        self,
        description: str,
        task_type: Optional[str] = None,
        target_files: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        source: str = "telegram",
        extra_metadata: Optional[Dict] = None,
    ) -> str:
        """Direct runtime entrypoint for Telegram/CLI instructions."""
        task = self._make_task(
            description=description,
            task_type=task_type,
            target_files=target_files,
            session_id=session_id,
            cwd=cwd,
            source=source,
            extra_metadata=extra_metadata,
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
            self._context_loader = _ContextLoader(self._artifact_index_path, Path(config.system.results_dir))
        return self._context_loader.load(task_id)
    
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
            await self._enqueue_task(task)
            
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
            route_remote = bool(
                config.mesh.enabled
                and session
                and session.machine_id
                and session.machine_id != socket.gethostname()
            )
            if route_remote:
                last_result = await self._process_task_remote(task, session, start_time, timeout_s)

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
                            if session and raw.backend_session_id:
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
        node = registry.get(session.machine_id)
        node_online = node is not None and node.status == "online"

        # The gateway's in-memory registry is only populated if this process
        # is also running the task server (co-located deployment). In a split
        # setup (separate task server process, or after a gateway restart that
        # wiped the in-memory registry), the node may only exist in the DB.
        # Fall back to the DB so a gateway restart doesn't kill in-flight sessions.
        if not node_online:
            from src.control.db import get_db as _get_db
            _db = _get_db()
            if _db is not None:
                _row = _db.get_node(session.machine_id)
                node_online = bool(_row and _row.get("status") == "online")

        if not node_online:
            result = _routing_failure(f"Node {session.machine_id!r} is offline; cannot continue session (no local fallback — affinity is required)")
            session.status = SessionStatus.ERROR
            self.session_store.save(session)
            result.error_class = self._classify_error(result)
            result.retries = 0
            return result

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
                logger.info("mesh_dispatch backend=%s -> %s", backend_name, target_node)
                self._emit_event("mesh_dispatch", task, {
                    "backend": backend_name,
                    "target_node": target_node,
                })
                result = await self._dispatch_to_node(task, session, node)
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
        if not result.success and result.errors and node_label:
            result.errors = [
                f"[{node_label}] {e}" if not str(e).startswith(f"[{node_label}]") else e
                for e in result.errors
            ]

        first_error = result.errors[0] if result.errors else ""
        logger.info(
            "mesh_result success=%s elapsed=%.1fs%s",
            result.success, result.execution_time,
            "" if result.success else f" error={first_error}",
        )
        self._emit_event("mesh_result", task, {
            "success": result.success,
            "target_node": node.node_id if node is not None else session.machine_id,
            "duration_s": round(result.execution_time, 3),
            "error_class": result.error_class,
            "error": first_error,
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
            # Best-effort cancel running exec task
            task = self._running_exec_tasks.get(task_id)
            if task is not None and not task.done():
                task.cancel()
            # Emit cancel_requested event
            t = self.active_tasks.get(task_id)
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
                if session and raw.backend_session_id:
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
                    if session and new_bsid:
                        session.backend_session_id = new_bsid
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
                    setattr(result, "backend_name", row.get("backend", "claude"))
                    setattr(
                        result,
                        "telemetry_invocation_id",
                        r.get("telemetry_invocation_id") or None,
                    )
                    return result

                if status in ("failed", "failed_node_offline"):
                    error_msg = row.get("error") or f"Task {status}"
                    result = TaskResult(
                        task_id=task.id,
                        success=False,
                        output="",
                        errors=[error_msg],
                        files_modified=[],
                        execution_time=0.0,
                        timestamp=datetime.now().isoformat(),
                    )
                    setattr(result, "backend_name", row.get("backend", "claude"))
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
        """Best-effort wake-up for a worker after enqueuing remote work."""
        import urllib.request

        tailscale_ip = getattr(node, "tailscale_ip", "") if node is not None else ""
        api_port = getattr(node, "api_port", 0) if node is not None else 0
        if (not tailscale_ip or not api_port) and node_id and db is not None:
            try:
                row = db.get_node(node_id)
            except Exception:
                row = None
            if row:
                tailscale_ip = row.get("tailscale_ip") or tailscale_ip
                api_port = row.get("api_port") or api_port

        if not tailscale_ip or not api_port:
            logger.debug("event=mesh_nudge_skipped node_id=%s reason=no_address", node_id)
            return False

        url = f"http://{tailscale_ip}:{api_port}/nudge"
        try:
            req = urllib.request.Request(url, method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=2):
                pass
            logger.debug("event=mesh_nudge_sent node_id=%s url=%s", node_id, url)
            return True
        except Exception as e:
            logger.debug("event=mesh_nudge_failed node_id=%s url=%s err=%s", node_id, url, e)
            return False

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

        Two cases:

        Local execution (session.machine_id not set, or MESH_ENABLED=false):
          Insert + immediately self-claim under this host's identity so no
          worker daemon can pick up the row as claimable work. The row is a
          faithful historical mirror; `_mesh_complete_task` finalises it.

        Remote dispatch (session.machine_id set and MESH_ENABLED=true):
          Insert as 'pending' WITHOUT self-claiming. The row is the actual
          dispatch signal — the pinned worker polls `get_pending_tasks`, sees
          it (machine_id filter matches), claims it, executes, and posts the
          result. `process_task` (via `_dispatch_to_node`) polls the DB for
          completion. `_mesh_complete_task` later enriches the row with the
          local artifact_path.
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
                }
            db.enqueue_task(
                task_id=task.id,
                session_id=session_id,
                machine_id=machine_id,
                backend=backend_name,
                action=action,
                payload=payload,
            )
            # Self-claim only when this task runs locally (no remote machine_id).
            # When machine_id is set, the row must stay 'pending' so the
            # pinned remote worker can claim it via get_pending_tasks.
            if not machine_id:
                if not db.claim_task(task.id, host):
                    logger.warning(
                        "event=mesh_self_claim_failed task_id=%s host=%s — "
                        "row may be claimable by a remote worker",
                        task.id, host,
                    )
        except Exception as e:
            if machine_id:
                # Remote dispatch depends on this row existing — log loudly so
                # the operator sees it immediately rather than after a 600s poll timeout.
                logger.error(
                    "event=mesh_enqueue_failed task_id=%s machine_id=%s err=%s — "
                    "worker will never see this task; dispatch will timeout",
                    task.id, machine_id, e,
                )
            else:
                logger.debug("event=mesh_enqueue_failed task_id=%s err=%s", task.id, e)

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
                return
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
                reply_text = (self._short_failure_reason(result) or "(failed)").strip()
            usage = None
            try:
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


class _ContextLoader:
    """Lightweight loader that produces compact, prompt-ready context.

    Reads `results/index.json` to resolve the latest artifact path for a task
    id, with a fallback to `results/{task_id}.json` when missing. Returns a
    small dictionary containing a short summary, constraints, and files list.
    """

    def __init__(self, index_path: Path, results_dir: Path) -> None:
        self._index_path = index_path
        self._results_dir = results_dir

    def load(self, task_id: str) -> Dict[str, Any]:
        import json
        default: Dict[str, Any] = {"summary": "", "constraints": {}, "files_modified": []}
        try:
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
                return default

            data = json.loads(artifact_path.read_text(encoding="utf-8"))
            # Extract up to ~2000 chars for prompt friendliness
            summary_text: str = ""
            po = data.get("parsed_output")
            if isinstance(po, dict):
                content = po.get("content")
                if isinstance(content, str):
                    summary_text = content[:2000]
            files_modified: List[str] = list(data.get("files_modified") or [])
            constraints: Dict[str, Any] = {"prior_success": bool(data.get("success"))}
            return {
                "summary": summary_text,
                "constraints": constraints,
                "files_modified": files_modified,
            }
        except Exception:
            return default
