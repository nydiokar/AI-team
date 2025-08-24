"""
Main AI Task Orchestrator - Coordinates all components
"""
import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime
import uuid
import random
import contextlib

import sys
import os
sys.path.append(os.path.dirname(__file__))

from core import (
    ITaskOrchestrator, Task, TaskResult, TaskStatus, TaskParser, 
    AsyncFileWatcher
)
from bridges import ClaudeBridge, LlamaMediator
from config import config
from validation.engine import ValidationEngine

logger = logging.getLogger(__name__)

class TaskOrchestrator(ITaskOrchestrator):
    """Main orchestrator that coordinates all AI task processing"""
    
    def __init__(self):
        # Initialize core components
        self.task_parser = TaskParser()
        self.file_watcher = AsyncFileWatcher(config.system.tasks_dir)
        self.claude_bridge = ClaudeBridge()
        self.llama_mediator = LlamaMediator()
        
        # Task management
        self.task_queue = asyncio.Queue()
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
        # Cancellation and runtime tracking
        self._task_cancel_events: Dict[str, asyncio.Event] = {}
        self._running_exec_tasks: Dict[str, asyncio.Task] = {}
        
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
    
    async def start(self):
        """Start all system components"""
        if self.running:
            logger.warning("Orchestrator is already running")
            return
        
        logger.info("Starting AI Task Orchestrator...")
        
        # Check component availability
        await self._check_component_status()
        
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
        
        # Start Telegram interface if available
        if self.telegram_interface:
            try:
                await self.telegram_interface.start()
                logger.info("Telegram interface started")
            except Exception as e:
                logger.error(f"Failed to start Telegram interface: {e}")
        
        # Log startup status
        self._log_startup_status()
        
        logger.info("AI Task Orchestrator started successfully!")
    
    async def stop(self):
        """Stop all system components"""
        if not self.running:
            return
        
        logger.info("Stopping AI Task Orchestrator...")
        
        self.running = False
        
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
        
        logger.info("AI Task Orchestrator stopped")
    
    async def _check_component_status(self):
        """Check availability of all components"""
        
        # Check Claude Code CLI
        self.component_status["claude_available"] = self.claude_bridge.test_connection()
        
        # Check LLAMA availability
        llama_status = self.llama_mediator.get_status()
        self.component_status["llama_available"] = llama_status["ollama_available"]
        
        logger.info(f"Component status: {self.component_status}")
    
    def _log_startup_status(self):
        """Log detailed startup status"""
        status_lines = [
            "=== AI Task Orchestrator Status ===",
            f"Claude Code CLI: {'[OK] Available' if self.component_status['claude_available'] else '[--] Not found'}",
            f"LLAMA/Ollama: {'[OK] Available' if self.component_status['llama_available'] else '[--] Using fallback'}",
            f"File Watcher: {'[OK] Running' if self.component_status['file_watcher_running'] else '[--] Stopped'}",
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

    def load_compact_context(self, task_id: str) -> Dict[str, Any]:
        """Load the latest artifact for a task and produce a compact context summary."""
        import json
        try:
            # Resolve artifact path from index; fallback to scan
            artifact_path: Path | None = None
            if self._artifact_index_path.exists():
                try:
                    idx = json.loads(self._artifact_index_path.read_text(encoding="utf-8"))
                    p = idx.get(str(task_id))
                    if p:
                        ap = Path(p)
                        if ap.exists():
                            artifact_path = ap
                except Exception:
                    pass
            if artifact_path is None:
                # Fallback: scan for results/{task_id}.json
                cand = Path(config.system.results_dir) / f"{task_id}.json"
                if cand.exists():
                    artifact_path = cand

            if not artifact_path or not artifact_path.exists():
                return {"summary": "", "constraints": {}, "files_modified": []}

            data = json.loads(artifact_path.read_text(encoding="utf-8"))
            summary_text = ""
            try:
                # Prefer summary in parsed_output or top-level validation hints
                po = data.get("parsed_output") or {}
                content = po.get("content") if isinstance(po, dict) else None
                if isinstance(content, str):
                    summary_text = content[:2000]
            except Exception:
                pass
            files_modified = data.get("files_modified") or []
            constraints = {"prior_success": bool(data.get("success"))}
            return {
                "summary": summary_text,
                "constraints": constraints,
                "files_modified": files_modified,
            }
        except Exception:
            return {"summary": "", "constraints": {}, "files_modified": []}
    
    async def _handle_new_task_file(self, file_path: str):
        """Handle detection of new task file"""
        try:
            # Acquire per-file lock; skip if already processing
            path_key = str(file_path)
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
            except Exception:
                pass
            logger.info(f"event=parsed task_id={task.id} type={task.type.value} priority={task.priority.value}")
            self._emit_event("parsed", task)
            
            # Add to queue
            await self.task_queue.put(task)
            self.active_tasks[task.id] = task
            
            logger.info(f"Queued task: {task.id} ({task.type.value}, {task.priority.value})")
            
        except Exception as e:
            logger.error(f"Error processing task file {file_path}: {e}")
            # Best-effort release of lock on exception
            try:
                self._inflight_paths.discard(str(file_path))
            except Exception:
                pass
    
    async def _task_worker(self, worker_name: str):
        """Worker coroutine that processes tasks from the queue"""
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

                logger.info(f"event=claude_started worker={worker_name} task_id={task.id}")
                self._emit_event("claude_started", task, {"worker": worker_name})
                
                # Process the task
                result = await self.process_task(task)
                
                # Store result
                self.task_results[task.id] = result
                
                # Update task status
                task.status = TaskStatus.COMPLETED if result.success else TaskStatus.FAILED
                
                # Log completion
                status = "SUCCESS" if result.success else "FAILED"
                logger.info(f"event=claude_finished task_id={task.id} status={status} duration_s={result.execution_time:.2f}")
                self._emit_event("claude_finished", task, {"status": status, "duration_s": result.execution_time})
                
                # Send Telegram notification if available
                if self.telegram_interface and result.success:
                    try:
                        # Extract summary from result output
                        summary = result.output.split('\n\n', 1)[0] if result.output else "Task completed"
                        await self.telegram_interface.notify_completion(task.id, summary, success=True)
                    except Exception as e:
                        logger.warning(f"Failed to send Telegram completion notification: {e}")
                elif self.telegram_interface and not result.success:
                    try:
                        error_summary = f"Task failed with errors: {'; '.join(result.errors[:2])}" if result.errors else "Task failed"
                        await self.telegram_interface.notify_completion(task.id, error_summary, success=False)
                    except Exception as e:
                        logger.warning(f"Failed to send Telegram failure notification: {e}")
                
                # Write artifacts
                try:
                    self._write_artifacts(task.id, result)
                    logger.info(f"event=artifacts_written task_id={task.id}")
                    self._emit_event("artifacts_written", task)
                except Exception as e:
                    logger.error(f"event=artifacts_error task_id={task.id} error={e}")
                    self._emit_event("artifacts_error", task, {"error": str(e)})

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
        """Process a single task through the complete pipeline"""
        start_time = time.time()
        
        try:
            task.status = TaskStatus.PROCESSING
            
            # Step 1: Use LLAMA to parse and optimize the task (or fallback)
            logger.debug(f"Step 1: Parsing task {task.id} with LLAMA mediator")
            task_content = self._reconstruct_task_content(task)
            parsed_task = self.llama_mediator.parse_task(task_content)
            
            # Step 2: Create optimized Claude prompt
            logger.debug(f"Step 2: Creating Claude prompt for task {task.id}")
            claude_prompt = self.llama_mediator.create_claude_prompt(parsed_task)
            task.prompt = claude_prompt
            
            # Step 3: Execute with Claude Code with timeout and cooperative cancellation
            logger.debug(f"Step 3: Executing task {task.id} with Claude Code")
            max_retries = getattr(config.validation, "max_retries", 2)
            retry_delay = 1.0
            backoff_mult = max(1, getattr(config.validation, "backoff_multiplier", 2))
            attempt = 0
            last_result: Optional[TaskResult] = None
            # Per-task timeout override via frontmatter metadata `timeout_sec`, else system default
            try:
                timeout_s = int(task.metadata.get("timeout_sec", config.system.task_timeout)) if getattr(task, "metadata", None) else config.system.task_timeout
            except Exception:
                timeout_s = config.system.task_timeout
            cancel_ev = self._task_cancel_events.get(task.id)
            while True:
                attempt += 1
                # Run execution as a task to allow timeout/cancel
                exec_task = asyncio.create_task(self.claude_bridge.execute_task(task))
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
                        result = exec_task.result()
                    elif cancel_waiter and cancel_waiter in done:
                        # Cooperative cancellation
                        exec_task.cancel()
                        with contextlib.suppress(Exception):
                            await exec_task
                        execution_time = time.time() - start_time
                        self._emit_event("cancelled", task, {"when": "during_execution"})
                        return TaskResult(
                            task_id=task.id,
                            success=False,
                            output="",
                            errors=["cancelled"],
                            files_modified=[],
                            execution_time=execution_time,
                            timestamp=datetime.now().isoformat(),
                        )
                    else:
                        # Timeout
                        exec_task.cancel()
                        with contextlib.suppress(Exception):
                            await exec_task
                        execution_time = time.time() - start_time
                        self._emit_event("timeout", task, {"timeout_s": timeout_s})
                        return TaskResult(
                            task_id=task.id,
                            success=False,
                            output="",
                            errors=[f"timeout after {timeout_s}s"],
                            files_modified=[],
                            execution_time=execution_time,
                            timestamp=datetime.now().isoformat(),
                        )
                finally:
                    # Cancel any pending helper waiters
                    for w in (cancel_waiter, timeout_waiter):
                        if w and not w.done():
                            w.cancel()
                error_class = self._classify_error(result)
                result.error_class = error_class
                result.retries = attempt - 1
                
                if error_class == "transient" and attempt <= max_retries and not result.success:
                    jitter = random.uniform(0.75, 1.5)
                    delay = retry_delay * jitter
                    logger.warning(f"event=retry task_id={task.id} attempt={attempt} class=transient delay_s={delay:.2f}")
                    self._emit_event("retry", task, {"attempt": attempt, "class": "transient", "delay_s": delay})
                    await asyncio.sleep(delay)
                    retry_delay *= backoff_mult
                    last_result = result
                    continue
                else:
                    last_result = result
                    break
            
            # Step 4: Summarize results with LLAMA
            logger.debug(f"Step 4: Summarizing results for task {task.id}")
            summary = self.llama_mediator.summarize_result(last_result, task)
            last_result.output = summary + "\n\n" + last_result.output
            logger.info(f"event=summarized task_id={task.id}")
            self._emit_event("summarized", task)
            
            # Step 5: Validation pass (MVP)
            try:
                validation_summary = {
                    "llama": self.validation_engine.validate_llama_output(
                        input_text=task.prompt or "",
                        output=last_result.output or "",
                        task_type=task.type,
                    ).__dict__,
                    "result": self.validation_engine.validate_task_result(
                        result=last_result,
                        expected_files=task.target_files or [],
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
        """Reconstruct task content for LLAMA processing"""
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

    def _write_artifacts(self, task_id: str, result: TaskResult):
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
            # Minimal status blocks for operability/triage
            "orchestrator": {
                "components": self.component_status,
                "workers": len(self.worker_tasks),
            },
            "bridge": {
                "available": bool(self.component_status.get("claude_available")),
                "claude_executable": getattr(self.claude_bridge, "claude_executable", "claude"),
                "max_turns": getattr(config.claude, "max_turns", 3),
                "timeout": getattr(config.claude, "timeout", 600),
                "skip_permissions": bool(getattr(config.claude, "skip_permissions", True)),
            },
            "llama": self.llama_mediator.get_status(),
        }

        import json
        (results_dir / f"{task_id}.json").write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        # Update artifact index (best-effort)
        try:
            self._update_artifact_index(task_id, results_dir / f"{task_id}.json")
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
        """Classify error type for retry policy: transient|fatal|none"""
        if result.success:
            return "none"
        text = (result.raw_stderr or "") + "\n" + (result.raw_stdout or "")
        text_lower = text.lower()
        transient_markers = [
            "rate limit", "rate-limit", "too many requests", "temporarily unavailable",
            "timeout", "timed out", "connection reset", "connection aborted",
            "503", "504", "network error", "retry later"
        ]
        for marker in transient_markers:
            if marker in text_lower:
                return "transient"
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
    
    def create_task_from_description(self, description: str) -> str:
        """Create a task file from natural language description"""
        
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        
        # Use LLAMA to parse description or simple template
        if self.component_status["llama_available"]:
            parsed = self._parse_description_with_llama(description)
        else:
            parsed = self._parse_description_simple(description)
        
        # Heuristic: detect inline path hints like "in C:\\Users\\..." or "in /path/..."
        # and inject into frontmatter as `cwd` if allowed by config
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
        
        # Create task file
        task_content = f"""---
id: {task_id}
type: {parsed.get('type', 'analyze')}
priority: {parsed.get('priority', 'medium')}
created: {datetime.now().isoformat()}
cwd: {parsed.get('metadata', {}).get('cwd', '')}
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
        return task_id
    
    def _parse_description_simple(self, description: str) -> Dict[str, Any]:
        """Simple parsing of task description"""
        
        # Basic keyword detection
        task_type = "analyze"
        if any(word in description.lower() for word in ["fix", "bug", "error"]):
            task_type = "fix"
        elif any(word in description.lower() for word in ["review", "check"]):
            task_type = "code_review"
        elif any(word in description.lower() for word in ["summary", "summarize"]):
            task_type = "summarize"
        
        return {
            "type": task_type,
            "title": f"Task: {description[:50]}...",
            "prompt": description,
            "priority": "medium",
            "target_files": []
        }
    
    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive orchestrator status"""
        return {
            "running": self.running,
            "components": self.component_status,
            "tasks": {
                "active": len(self.active_tasks),
                "queued": self.task_queue.qsize(),
                "completed": len(self.task_results),
                "workers": len(self.worker_tasks)
            },
            "llama_status": self.llama_mediator.get_status()
        }