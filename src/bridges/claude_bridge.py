"""
Claude Code CLI bridge implementation
"""
import json
import asyncio
import subprocess
import time
import shutil
from typing import Dict, List, Any, Optional
from pathlib import Path
from datetime import datetime
import logging

from src.core import IClaudeBridge, Task, TaskResult, TaskStatus, TaskType
from config import config
from .claude_session_parser import ClaudeSessionParser

logger = logging.getLogger(__name__)

class ClaudeBridge(IClaudeBridge):
    """Bridge to interact with Claude Code CLI.
    
    Uses Claude session files to detect actual file changes instead of filesystem snapshots.
    """
    
    def __init__(self):
        self.claude_executable = self._find_claude_executable()
        self.base_command = self._build_base_command()
        self.session_parser = ClaudeSessionParser()
        
    def _find_claude_executable(self) -> str:
        """Find the Claude executable path."""
        claude_path = shutil.which("claude")
        return claude_path or "claude"
        
    def _build_base_command(self) -> List[str]:
        """Build the base Claude command."""
        try:
            cmd = list(config.claude.base_command)
            if cmd:
                cmd[0] = self.claude_executable
            return cmd
        except Exception:
            return [self.claude_executable, "--output-format", "json", "-p"]
    
    async def execute_task(self, task: Task) -> TaskResult:
        """Execute a task using Claude Code CLI."""
        start_time = time.time()
        
        try:
            # Resolve working directory
            cwd_override = self._resolve_cwd(task)
            
            # Record timestamp before execution for session file filtering
            execution_start_time = datetime.now()
            
            # Build the complete prompt
            prompt = self._build_prompt(task)
            
            # Build command with allowed tools
            command = self.base_command.copy()
            
            # Add specific tool permissions for safety
            if getattr(config.system, "guarded_write", False):
                allowed_tools = ["Read", "LS", "Grep", "Glob"]
            else:
                allowed_tools = self._get_allowed_tools_for_task(task.type)
            if allowed_tools:
                command.extend(["--allowedTools", ",".join(allowed_tools)])

            # Use global max-turns from config when > 0
            try:
                if int(getattr(config.claude, "max_turns", 0)) > 0:
                    command.extend(["--max-turns", str(config.claude.max_turns)])
            except Exception:
                pass
            
            # Execute the command
            result = await self._execute_command(command, task.target_files, cwd_override=cwd_override, stdin_input=prompt)
            
            execution_time = time.time() - start_time
            
            # Parse the result
            parsed = self._parse_result(task.id, result, execution_time)
            
            # Detect file changes using Claude session files (more accurate than filesystem snapshots)
            if cwd_override:
                files_modified = self._detect_file_changes_from_sessions(cwd_override, execution_start_time)
                parsed.files_modified = files_modified
            
            # Attach execution cwd for diagnostics
            try:
                po = parsed.parsed_output if isinstance(parsed.parsed_output, dict) else {}
                po.setdefault("meta", {})["execution_cwd"] = cwd_override
                parsed.parsed_output = po or {"meta": {"execution_cwd": cwd_override}}
            except Exception:
                pass
            
            return parsed
            
        except Exception as e:
            execution_time = time.time() - start_time
            return TaskResult(
                task_id=task.id,
                success=False,
                output="",
                errors=[str(e)],
                files_modified=[],
                execution_time=execution_time,
                timestamp=time.strftime("%Y-%m-%d %H:%M:%S")
            )
    
    def _detect_file_changes_from_sessions(self, cwd: str, execution_start_time: datetime) -> List[str]:
        """Detect file changes using Claude session files (more accurate than filesystem snapshots)."""
        try:
            # Find relevant session files for this working directory
            session_files = self.session_parser.find_session_files(cwd, execution_start_time)
            
            if not session_files:
                logger.debug(f"No Claude session files found for {cwd}")
                return []
            
            # Parse the session files to extract actual file changes
            changes = self.session_parser.parse_file_changes(session_files)
            
            # Combine all changes into a single list for files_modified
            all_changes = []
            
            # Add created files
            for file_path in changes["created"]:
                all_changes.append(f"Created: {file_path}")
            
            # Add modified files
            for file_path in changes["modified"]:
                all_changes.append(f"Modified: {file_path}")
            
            # Add deleted files
            for file_path in changes["deleted"]:
                all_changes.append(f"Deleted: {file_path}")
            
            if all_changes:
                logger.info(f"Detected file changes from Claude sessions: {len(all_changes)} changes")
                logger.debug(f"Changes: {all_changes}")
            else:
                logger.debug("No file changes detected in Claude sessions")
            
            return all_changes
            
        except Exception as e:
            logger.warning(f"Error detecting file changes from sessions: {e}")
            return []
    
    def _build_prompt(self, task: Task) -> str:
        """Build a comprehensive prompt for Claude."""
        prompt_parts = [
            f"Task Type: {task.type.value.upper()}",
            f"Task: {task.title}",
            "",
            "Description:",
            task.prompt,
            ""
        ]
        
        if task.target_files:
            prompt_parts.extend([
                "Target Files:",
                *[f"- {file}" for file in task.target_files],
                ""
            ])
        
        if task.success_criteria:
            prompt_parts.extend([
                "Success Criteria:",
                *[f"- {criteria}" for criteria in task.success_criteria],
                ""
            ])
        
        if task.context:
            prompt_parts.extend([
                "Context:",
                task.context,
                ""
            ])
        
        prompt_parts.extend([
            "Please:",
            "1. Analyze the current state of the specified files",
            "2. Implement the requested changes",
            "3. Provide a clear summary of what was accomplished",
            "4. Note any issues or limitations encountered"
        ])
        
        return "\n".join(prompt_parts)
    
    def _get_allowed_tools_for_task(self, task_type) -> List[str]:
        """Get allowed tools based on task type."""
        base_tools = ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob"]
        
        if task_type in (TaskType.FIX, TaskType.ANALYZE):
            base_tools.extend(["Bash"])
        
        return base_tools
    
    async def _execute_command(self, command: List[str], target_files: List[str], 
                              cwd_override: Optional[str] = None, stdin_input: Optional[str] = None) -> Dict[str, Any]:
        """Execute Claude command asynchronously."""
        
        # Change to the appropriate working directory if specified
        cwd = cwd_override if cwd_override else None
        
        # Execute the command
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if stdin_input else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )
        
        # Send input via stdin if provided
        if stdin_input:
            stdin_data = stdin_input.encode('utf-8')
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin_data),
                timeout=config.claude.timeout
            )
        else:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=config.claude.timeout
            )
        
        return {
            'returncode': process.returncode,
            'stdout': stdout.decode('utf-8') if stdout else '',
            'stderr': stderr.decode('utf-8') if stderr else ''
        }
    
    def _parse_result(self, task_id: str, result: Dict[str, Any], execution_time: float) -> TaskResult:
        """Parse command result into TaskResult."""
        
        success = result['returncode'] == 0
        stdout = result['stdout']
        stderr = result['stderr']
        
        # Try to parse JSON output if available
        parsed_output = None
        if stdout:
            try:
                parsed_output = json.loads(stdout)
            except json.JSONDecodeError:
                # Attempt to strip non-JSON preamble
                s = stdout.strip()
                start, end = s.find("{"), s.rfind("}")
                if start != -1 and end != -1 and end > start:
                    try:
                        parsed_output = json.loads(s[start:end+1])
                    except json.JSONDecodeError:
                        parsed_output = None
            
            # If we parsed structured output, prefer its content when available
            if isinstance(parsed_output, dict):
                stdout = parsed_output.get('content', stdout)
        
        # Extract error messages
        errors = []
        if stderr:
            errors.append(stderr)
        if not success and not errors:
            errors.append("Command execution failed")
        
        return TaskResult(
            task_id=task_id,
            success=success,
            output=stdout,
            errors=errors,
            files_modified=[],
            execution_time=execution_time,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            raw_stdout=result.get('stdout', ''),
            raw_stderr=result.get('stderr', ''),
            parsed_output=parsed_output,
            return_code=result.get('returncode', -1)
        )
    
    def _resolve_cwd(self, task: Task) -> Optional[str]:
        """Resolve working directory for task execution."""
        try:
            # Check for per-task cwd override in metadata
            if hasattr(task, 'metadata') and task.metadata:
                cwd_override = task.metadata.get('cwd')
                if cwd_override:
                    candidate = Path(cwd_override)
                    if candidate.exists() and candidate.is_dir():
                        return str(candidate)
            
            # Use base_cwd from config if set
            base_cwd = getattr(config.claude, 'base_cwd', None)
            if base_cwd:
                try:
                    base = Path(base_cwd).resolve()
                    return str(base)
                except Exception:
                    pass
            
            # No fallback - return None
            return None
            
        except Exception:
            return None
    
    def test_connection(self) -> bool:
        """Test if Claude Code is available and working."""
        try:
            result = subprocess.run(
                [self.claude_executable, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False
