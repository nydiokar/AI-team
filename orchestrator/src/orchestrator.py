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
        
        logger.info("TaskOrchestrator initialized")
        self.validation_engine = ValidationEngine()
        # Ensure logs directory exists for event emission
        try:
            Path(config.system.logs_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    
    async def start(self):
        """Start all system components"""
        if self.running:
            logger.warning("Orchestrator is already running")
            return
        
        logger.info("Starting AI Task Orchestrator...")
        
        # Check component availability
        await self._check_component_status()
        
        # Start file watcher
        await self.file_watcher.start_async(self._handle_new_task_file)
        self.component_status["file_watcher_running"] = True
        
        # Start task processing workers
        for i in range(config.system.max_concurrent_tasks):
            worker = asyncio.create_task(self._task_worker(f"worker-{i}"))
            self.worker_tasks.append(worker)
        
        self.running = True
        
        # Log startup status
        self._log_startup_status()
        
        logger.info("AI Task Orchestrator started successfully!")
    
    async def stop(self):
        """Stop all system components"""
        if not self.running:
            return
        
        logger.info("Stopping AI Task Orchestrator...")
        
        self.running = False
        
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
    
    async def _handle_new_task_file(self, file_path: str):
        """Handle detection of new task file"""
        try:
            logger.info(f"event=task_received file={file_path}")
            self._emit_event("task_received", None, {"file": file_path})
            
            # Validate task file format
            errors = self.task_parser.validate_task_format(file_path)
            if errors:
                logger.error(f"Invalid task file format: {errors}")
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
            
            # Step 3: Execute with Claude Code with limited retries on transient errors
            logger.debug(f"Step 3: Executing task {task.id} with Claude Code")
            max_retries = 2
            retry_delay = 2.0
            attempt = 0
            last_result: Optional[TaskResult] = None
            while True:
                attempt += 1
                result = await self.claude_bridge.execute_task(task)
                error_class = self._classify_error(result)
                result.error_class = error_class
                result.retries = attempt - 1
                
                if error_class == "transient" and attempt <= max_retries and not result.success:
                    logger.warning(f"event=retry task_id={task.id} attempt={attempt} class=transient delay_s={retry_delay}")
                    self._emit_event("retry", task, {"attempt": attempt, "class": "transient", "delay_s": retry_delay})
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
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
            "task_id": task_id,
            "success": result.success,
            "return_code": result.return_code,
            "timestamp": result.timestamp,
            "execution_time": result.execution_time,
            "errors": result.errors,
            "files_modified": result.files_modified,
            "raw_stdout": result.raw_stdout,
            "raw_stderr": result.raw_stderr,
            "parsed_output": result.parsed_output,
            "validation": getattr(result, "validation", None),
            "retry": {
                "retries": getattr(result, "retries", 0),
                "error_class": getattr(result, "error_class", ""),
            },
        }

        import json
        (results_dir / f"{task_id}.json").write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

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
            with event_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
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
        
        # Create task file
        task_content = f"""---
id: {task_id}
type: {parsed.get('type', 'analyze')}
priority: {parsed.get('priority', 'medium')}
created: {datetime.now().isoformat()}
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
        
        # Write task file
        task_file = Path(config.system.tasks_dir) / f"{task_id}.task.md"
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