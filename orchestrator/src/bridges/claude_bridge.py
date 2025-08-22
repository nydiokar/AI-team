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

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from core import IClaudeBridge, Task, TaskResult, TaskStatus, TaskType
from config import config

class ClaudeBridge(IClaudeBridge):
    """Bridge to interact with Claude Code CLI"""
    
    def __init__(self):
        self.claude_executable = self._find_claude_executable()
        self.base_command = self._build_base_command()
        
    def _find_claude_executable(self) -> str:
        """Find the Claude executable path"""
        # Prefer the PATH-resolved executable
        claude_path = shutil.which("claude")
        return claude_path or "claude"
        
    def _build_base_command(self) -> List[str]:
        """Build the base Claude command with optimal settings for automation"""
        cmd = [self.claude_executable]
        
        # Use headless mode for automation
        cmd.append("-p")  # Non-interactive
        
        # Structured output for parsing
        cmd.extend(["--output-format", "json"])
        
        # Headless permissions behavior (default to bypassPermissions unless explicitly skipped)
        if config.claude.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd.extend(["--permission-mode", "bypassPermissions"])  # non-interactive
        
        return cmd
    
    async def execute_task(self, task: Task) -> TaskResult:
        """Execute a task using Claude Code CLI"""
        start_time = time.time()
        
        try:
            # Build the complete prompt
            prompt = self._build_prompt(task)
            
            # Build command with allowed tools
            command = self.base_command.copy()
            
            # Add specific tool permissions for safety
            allowed_tools = self._get_allowed_tools_for_task(task.type)
            if allowed_tools:
                command.extend(["--allowedTools", ",".join(allowed_tools)])

            # Use global max-turns from config without task-specific overrides
            command.extend(["--max-turns", str(config.claude.max_turns)])
            
            # Add the prompt
            command.append(prompt)
            
            # Execute the command
            result = await self._execute_command(command, task.target_files)
            
            execution_time = time.time() - start_time
            
            # Parse the result
            return self._parse_result(task.id, result, execution_time)
            
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
    
    def test_connection(self) -> bool:
        """Test if Claude Code is available and working"""
        try:
            # Prefer a non-interactive check first
            version = subprocess.run(
                [self.claude_executable, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if version.returncode == 0 and version.stdout.strip():
                return True

            # Fallback: auth status may show an interactive trust prompt; treat that as presence
            status = subprocess.run(
                [self.claude_executable, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if status.returncode == 0:
                return True
            text_out = (status.stdout or "") + (status.stderr or "")
            if "Do you trust the files in this folder" in text_out or "trust the files" in text_out:
                # CLI is present; interactive prompt is expected in some environments
                return True
            return False
        except Exception:
            return False
    
    def _build_prompt(self, task: Task) -> str:
        """Pass through the task prompt with minimal framing."""
        # Keep only the raw task prompt and optional target files/context
        parts = [task.prompt]
        if task.target_files:
            parts.append("\nTarget Files:")
            parts.extend([f"- {file}" for file in task.target_files])
        if task.context:
            parts.append("\nContext:")
            parts.append(task.context)
        assembled = "\n".join(parts)
        # Safety cap on prompt size to avoid CLI issues
        try:
            from config import config as _cfg
            max_chars = getattr(_cfg.llama, "max_prompt_chars", 32_000)
        except Exception:
            max_chars = 32_000
        if len(assembled) > max_chars:
            assembled = assembled[:max_chars]
        return assembled
    
    def _get_allowed_tools_for_task(self, task_type) -> List[str]:
        """Get allowed tools based on task type (least-privilege).

        Accepts either a TaskType enum (from any import path) or a string.
        We normalize to a lowercase string to avoid enum identity issues across modules.
        """
        type_value = getattr(task_type, "value", str(task_type)).lower()
        if type_value in ("fix", "analyze"):
            return ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob", "Bash"]
        if type_value in ("code_review", "summarize"):
            return ["Read", "LS", "Grep", "Glob"]
        # Default to safe read-only if unknown
        return ["Read", "LS", "Grep", "Glob"]
    
    async def _execute_command(self, command: List[str], target_files: List[str]) -> Dict[str, Any]:
        """Execute Claude command asynchronously"""
        
        # Set working directory to project root for best results
        cwd = None
        try:
            # Use the project root as the working directory
            project_root = Path(__file__).resolve().parents[3]  # bridges -> src -> orchestrator -> project_root
            cwd = str(project_root)
            print(f"Setting Claude working directory to project root: {cwd}")
        except Exception:
            cwd = None
        
        # Execute the command
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )
        
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=config.claude.timeout
        )
        
        return {
            'returncode': process.returncode,
            'stdout': stdout.decode('utf-8', errors='replace') if stdout else '',
            'stderr': stderr.decode('utf-8', errors='replace') if stderr else ''
        }
    
    def _parse_result(self, task_id: str, result: Dict[str, Any], execution_time: float) -> TaskResult:
        """Parse command result into TaskResult"""
        
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
        
        # Try to detect modified files from output
        # This is a simple heuristic - in practice, you might want more sophisticated detection
        files_modified = []
        if stdout:
            # Look for file modification patterns in the output
            import re
            file_patterns = [
                r'Modified:\s+(.+)',
                r'Edited:\s+(.+)',
                r'Updated:\s+(.+)',
                r'Created:\s+(.+)'
            ]
            for pattern in file_patterns:
                matches = re.findall(pattern, stdout, re.IGNORECASE)
                files_modified.extend(matches)
        
        return TaskResult(
            task_id=task_id,
            success=success,
            output=stdout,
            errors=errors,
            files_modified=files_modified,
            execution_time=execution_time,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            raw_stdout=result.get('stdout', ''),
            raw_stderr=result.get('stderr', ''),
            parsed_output=parsed_output,
            return_code=result.get('returncode', -1)
        )