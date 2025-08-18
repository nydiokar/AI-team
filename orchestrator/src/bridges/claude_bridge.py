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
        
        # Headless permissions behavior
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

            # Cap max conversation turns for safety
            command.extend(["--max-turns", "10"])  
            
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
        """Build a comprehensive prompt for Claude"""
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
        """Get allowed tools based on task type"""
        base_tools = ["Read", "Edit", "MultiEdit", "LS", "Grep", "Glob"]
        
        if task_type in (TaskType.FIX, TaskType.ANALYZE):
            # For fix and analyze tasks, allow bash execution
            base_tools.extend(["Bash"])
        
        return base_tools
    
    async def _execute_command(self, command: List[str], target_files: List[str]) -> Dict[str, Any]:
        """Execute Claude command asynchronously"""
        
        # Change to the appropriate working directory if target files are specified
        cwd = None
        if target_files:
            # Find common directory for target files
            paths = [Path(f).parent for f in target_files if Path(f).is_absolute()]
            if paths:
                # Use the most common parent directory
                cwd = str(paths[0])
        
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
            'stdout': stdout.decode('utf-8') if stdout else '',
            'stderr': stderr.decode('utf-8') if stderr else ''
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
            except json.JSONDecodeError:
                pass
            
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
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S")
        )