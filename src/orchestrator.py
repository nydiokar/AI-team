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

from src.core import (
    ITaskOrchestrator, Task, TaskResult, TaskStatus, TaskType, TaskPriority, TaskParser,
    AsyncFileWatcher, SessionStore, SessionStatus, PathResolver
)
from src.bridges import LlamaMediator
from src.backends import ClaudeCodeBackend, CodexBackend
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
        self._backends = {
            "claude": ClaudeCodeBackend(),
            "codex": CodexBackend(),
        }
        
        # Task management
        self.task_queue = asyncio.Queue(maxsize=config.system.max_queue_size)
        self.active_tasks: Dict[str, Task] = {}
        self.task_results: Dict[str, TaskResult] = {}
        
        # System state
        self.running = False
        self.worker_tasks: List[asyncio.Task] = []
        
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
        # Cancellation and runtime tracking
        self._task_cancel_events: Dict[str, asyncio.Event] = {}
        self._running_exec_tasks: Dict[str, asyncio.Task] = {}
        self._shutdown_interrupted_tasks: set[str] = set()
        
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
        if any(s in haystack_lower for s in ("timeout", "timed out")):
            return "Claude timeout"
        if any(s in haystack_lower for s in ("connection reset", "connection aborted", "network error", "temporarily unavailable", "service unavailable")):
            return "Claude network error"
        if any(isinstance(e, str) and "interactive_prompt_detected" in e for e in (result.errors or [])):
            return "Claude needs interactive approval"

        for text in texts:
            low = text.lower()
            if low.startswith("claude exited with code "):
                continue
            compact = " ".join(text.split())
            if compact:
                return compact[:120]

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
        """Convert BUSY sessions left behind by a previous restart into a stable error state."""
        stale_sessions: List[Any] = []
        host = socket.gethostname()
        active_task_ids = set(self.active_tasks.keys())

        for session in self.session_store.list_all():
            if session.status != SessionStatus.BUSY:
                continue
            if session.machine_id and session.machine_id != host:
                continue
            if session.last_task_id and session.last_task_id in active_task_ids:
                continue
            stale_sessions.append(session)

        for session in stale_sessions:
            session.status = SessionStatus.ERROR
            session.last_result_summary = "Interrupted by gateway restart; partial changes may exist."
            self.session_store.save(session)
            result = TaskResult(
                task_id=session.last_task_id or f"session_{session.session_id}",
                success=False,
                output="",
                errors=["interrupted by gateway restart"],
                files_modified=[],
                execution_time=0.0,
                timestamp=datetime.now().isoformat(),
            )
            setattr(result, "backend_name", session.backend or "claude")
            self._write_session_summary(session, result)
            self._append_session_event(session.session_id, session.last_task_id or "", result)
            self._emit_event(
                "session_interrupted_recovered",
                None,
                {"session_id": session.session_id, "task_id": session.last_task_id, "backend": session.backend},
            )
            if self.telegram_interface and session.telegram_chat_id:
                try:
                    await self.telegram_interface.notify_completion(
                        session.last_task_id or session.session_id,
                        "Task interrupted by gateway restart",
                        success=False,
                        chat_id=session.telegram_chat_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to notify interrupted session recovery: {e}")

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
        
        # Cancel worker tasks
        for worker in self.worker_tasks:
            worker.cancel()
        
        # Wait for workers to finish
        await asyncio.gather(*self.worker_tasks, return_exceptions=True)
        self.worker_tasks.clear()
        
        logger.info("Telegram Coding Gateway stopped")
    
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
                text=True,
                timeout=10,
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
            metadata={
                "session_id": session_id or "",
                "cwd": resolved_cwd,
                "source": source,
                "task_origin": "runtime",
            },
        )
        return task

    async def _enqueue_task(self, task: Task) -> str:
        """Queue a task object directly without writing a task file."""
        logger.info(f"event=task_created task_id={task.id} source={(task.metadata or {}).get('source', 'runtime')}")
        self._emit_event("task_created", task, {"source": (task.metadata or {}).get("source", "runtime")})
        self._emit_event("parsed", task)

        try:
            self.task_queue.put_nowait(task)
            self.active_tasks[task.id] = task
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
    ) -> str:
        """Direct runtime entrypoint for Telegram/CLI instructions."""
        task = self._make_task(
            description=description,
            task_type=task_type,
            target_files=target_files,
            session_id=session_id,
            cwd=cwd,
            source=source,
        )
        return await self._enqueue_task(task)

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
                
                # Process the task
                result = await self.process_task(task)
                
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
                
                # Send Telegram notification if available
                if self.telegram_interface:
                    try:
                        session_id_for_notify = (task.metadata or {}).get("session_id", "").strip()
                        notify_chat_id: Optional[int] = None
                        if session_id_for_notify:
                            _s = self.session_store.get(session_id_for_notify)
                            if _s:
                                notify_chat_id = _s.telegram_chat_id

                        if result.success:
                            # For session tasks use Claude's raw output; for standalone use LLAMA summary
                            if session_id_for_notify:
                                content = self._session_reply_text(result)
                            else:
                                summary_file = Path(config.system.summaries_dir) / f"{task.id}_summary.txt"
                                if summary_file.exists():
                                    content = summary_file.read_text(encoding='utf-8').strip() or "Task completed successfully"
                                else:
                                    content = result.output.split('\n\n', 1)[0] if result.output else "Task completed successfully"
                            # Append changed-file list if any were detected
                            files = result.files_modified or []
                            if files:
                                lines = self._format_file_change_lines(result, limit=20)
                                content = content + "\n\n**Changed files:**\n" + "\n".join(lines)
                            await self.telegram_interface.notify_completion(task.id, content, success=True, chat_id=notify_chat_id)
                        else:
                            short_reason = self._short_failure_reason(result)
                            error_summary = f"Task failed: {short_reason}" if short_reason else "Task failed"
                            await self.telegram_interface.notify_completion(task.id, error_summary, success=False, chat_id=notify_chat_id)
                    except Exception as e:
                        logger.warning(f"Failed to send Telegram completion notification: {e}")
                
                # Write artifacts
                try:
                    self._write_artifacts(task.id, result, task=task)
                    logger.info(f"event=artifacts_written task_id={task.id}")
                    self._emit_event("artifacts_written", task)
                except Exception as e:
                    logger.error(f"event=artifacts_error task_id={task.id} error={e}")
                    self._emit_event("artifacts_error", task, {"error": str(e)})

                # Update session record + write compact summary + per-session event log
                try:
                    session_id = (task.metadata or {}).get("session_id", "").strip()
                    if session_id:
                        session = self.session_store.get(session_id)
                        if session:
                            session.last_task_id = task.id
                            if not result.success:
                                session.last_result_summary = self._short_failure_reason(result) or "(failed)"
                            else:
                                # Take the last ~400 chars (the conclusion) rather than
                                # the first 200 (always mid-explanation for session turns).
                                out = (result.output or "").strip()
                                session.last_result_summary = out[-400:] if len(out) > 400 else out
                            session.last_files_modified = result.files_modified or []
                            artifact_path = str(Path(config.system.results_dir) / f"{task.id}.json")
                            session.last_artifact_path = artifact_path
                            session.task_history.append({
                                "task_id": task.id,
                                "timestamp": result.timestamp,
                                "success": result.success,
                                "execution_time": round(result.execution_time or 0.0, 2),
                            })
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
            # Per-task timeout override via frontmatter metadata `timeout_sec`, else system default
            try:
                timeout_s = int(task.metadata.get("timeout_sec", config.system.task_timeout)) if getattr(task, "metadata", None) else config.system.task_timeout
            except Exception:
                timeout_s = config.system.task_timeout
            cancel_ev = self._task_cancel_events.get(task.id)
            while True:
                attempt += 1
                # Run execution as a task to allow timeout/cancel
                # Use session backend (with native resume) when task belongs to a session.
                # For non-session tasks, use the native backend directly instead of
                # the legacy Claude bridge/task-file execution path.
                session_id = (task.metadata or {}).get("session_id", "").strip()
                session = self.session_store.get(session_id) if session_id else None
                if session:
                    session.status = SessionStatus.BUSY
                    self.session_store.save(session)
                    backend_name = session.backend
                    backend = self._backends.get(backend_name, self._backends["claude"])
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
                    backend_name = str((task.metadata or {}).get("backend") or "claude").strip().lower()
                    backend = self._backends.get(backend_name, self._backends["claude"])
                    cwd_override = str((task.metadata or {}).get("cwd") or "").strip()
                    if not cwd_override:
                        cwd_override = str(getattr(config.claude, "base_cwd", "") or "").strip()
                    exec_task = asyncio.create_task(
                        asyncio.to_thread(backend.run_oneoff, cwd_override, task.prompt)
                    )
                self._running_exec_tasks[task.id] = exec_task
                # Wait for whichever happens first
                wait_set = {exec_task}
                cancel_waiter: Optional[asyncio.Task] = None
                timeout_waiter: Optional[asyncio.Task] = None
                try:
                    if cancel_ev is not None:
                        cancel_waiter = asyncio.create_task(cancel_ev.wait())
                        wait_set.add(cancel_waiter)
                    if timeout_s and timeout_s > 0:
                        timeout_waiter = asyncio.create_task(asyncio.sleep(timeout_s))
                        wait_set.add(timeout_waiter)
                    done, pending = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
                    if exec_task in done:
                        raw = exec_task.result()
                        # Normalize ExecutionResult (from backends) to TaskResult
                        from src.core.interfaces import ExecutionResult as _ER
                        if isinstance(raw, _ER):
                            # Persist backend session ID back onto the session record
                            if session and raw.success and raw.backend_session_id:
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
                        else:
                            result = raw
                    elif cancel_waiter and cancel_waiter in done:
                        # Cooperative cancellation
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
                        return result
                    else:
                        # Timeout
                        if session:
                            with contextlib.suppress(Exception):
                                backend.cancel(session)
                        exec_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await exec_task
                        execution_time = time.time() - start_time
                        self._emit_event("timeout", task, {"timeout_s": timeout_s})
                        if session:
                            session.status = SessionStatus.ERROR
                            self.session_store.save(session)
                        result = TaskResult(
                            task_id=task.id,
                            success=False,
                            output="",
                            errors=[f"timeout after {timeout_s}s"],
                            files_modified=[],
                            execution_time=execution_time,
                            timestamp=datetime.now().isoformat(),
                        )
                        setattr(result, "backend_name", backend_name)
                        return result
                finally:
                    # Cancel any pending helper waiters
                    for w in (cancel_waiter, timeout_waiter):
                        if w and not w.done():
                            w.cancel()
                error_class = self._classify_error(result)
                result.error_class = error_class
                result.retries = attempt - 1

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
                    await asyncio.sleep(delay)
                    retry_delay = retry_delay * backoff_mult if retry_delay > 0 else strategy.get("initial_delay", 1.0) * backoff_mult
                    last_result = result
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
            "llama": self.llama_mediator.get_status(probe=False),
        }
        if task is not None:
            artifact["task"] = {
                "type": getattr(task.type, "value", str(task.type)),
                "priority": getattr(task.priority, "value", str(task.priority)),
                "title": task.title,
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
            actions.append("Increase CLAUDE_TIMEOUT_SEC or reduce task scope.")
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
        if any(s in text_lower for s in ("timeout", "timed out")):
            return "timeout"
        if any(s in text_lower for s in ("connection reset", "connection aborted", "network error", "503", "504", "temporarily unavailable")):
            return "network"
        if any(s in text_lower for s in ("prompt is too long", "blocking_limit", "context_window", "context window")):
            return "context_overflow"
        if any(s in text_lower for s in ("unauthorized", "forbidden", "permission denied", "not logged in", "authentication")):
            return "auth"
        return "fatal"

    def _emit_event(self, name: str, task: Optional[Task] = None, extra: Optional[Dict[str, Any]] = None) -> None:
        """Append a single NDJSON event line to logs/events.ndjson"""
        try:
            event_path = Path(config.system.logs_dir) / "events.ndjson"
            payload: Dict[str, Any] = {
                "timestamp": datetime.now().isoformat(),
                "event": name,
            }
            if task is not None:
                payload.update({
                    "task_id": task.id,
                    "task_type": getattr(task.type, "value", str(task.type)),
                    "priority": getattr(task.priority, "value", str(task.priority)),
                    "status": getattr(task.status, "value", str(task.status)),
                })
            if extra:
                payload.update(extra)
            import json
            # Write event line
            with event_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            
            # Simple size-based rotation (~1MB, keep 3 backups)
            try:
                max_bytes = 1_000_000
                backup_count = 3
                if event_path.stat().st_size > max_bytes:
                    # Rotate: events.ndjson -> events.ndjson.1, .1 -> .2, etc.
                    for idx in range(backup_count - 1, 0, -1):
                        src = event_path.with_suffix(event_path.suffix + f".{idx}")
                        dst = event_path.with_suffix(event_path.suffix + f".{idx+1}")
                        if src.exists():
                            try:
                                src.replace(dst)
                            except Exception:
                                pass
                    # Move current to .1 and recreate empty base file
                    first_backup = event_path.with_suffix(event_path.suffix + ".1")
                    try:
                        event_path.replace(first_backup)
                        event_path.touch()
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            # Do not fail the pipeline on telemetry errors
            pass
    
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
