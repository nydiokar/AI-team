"""
LLAMA mediator with intelligent fallback modes
"""
import json
import re
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from core import ILlamaMediator, Task, TaskResult, TaskType
from config import config

logger = logging.getLogger(__name__)

class LlamaMediator(ILlamaMediator):
    """LLAMA mediator with automatic fallback to simple parsing"""
    
    def __init__(self):
        self.ollama_available = self._check_ollama_availability()
        self.client = None
        self.model_installed = False
        
        if self.ollama_available:
            try:
                import ollama
                self.client = ollama.Client(host=f"http://{config.llama.host}:{config.llama.port}")
                self.model_installed = self._is_model_installed(config.llama.model)
                logger.info("LLAMA/Ollama client initialized successfully")
            except ImportError:
                logger.warning("Ollama package not available, using fallback mode")
                self.ollama_available = False
            except Exception as e:
                logger.warning(f"Failed to initialize Ollama client: {e}")
                self.ollama_available = False
        else:
            logger.info("LLAMA not available, using built-in parsing fallback")
    
    def _check_ollama_availability(self) -> bool:
        """Check if Ollama is running and accessible"""
        try:
            import subprocess
            result = subprocess.run(
                ["ollama", "list"], 
                capture_output=True, 
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _is_model_installed(self, model_name: str) -> bool:
        """Check if specified model is installed locally to avoid long pulls"""
        if not self.ollama_available:
            return False
        try:
            import subprocess
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=5
            )
            output = (result.stdout or "") + (result.stderr or "")
            return model_name.split(":")[0] in output
        except Exception:
            return False
    
    def parse_task(self, task_content: str) -> Dict[str, Any]:
        """Parse task content using LLAMA or fallback to simple parsing"""
        
        if self.ollama_available and self.client and self.model_installed:
            return self._parse_with_llama(task_content)
        else:
            if self.ollama_available and not self.model_installed:
                logger.info("LLAMA model not installed; using fallback parser to avoid long downloads")
            return self._parse_with_fallback(task_content)
    
    def _parse_with_llama(self, task_content: str) -> Dict[str, Any]:
        """Parse using LLAMA/Ollama"""
        try:
            prompt = f"""
            Parse this task file and extract the following information in JSON format:
            1. Task type (code_review, summarize, fix, analyze)
            2. Target files (list of file paths)
            3. Main prompt/request (the core task description)
            4. Priority level (high, medium, low)
            5. Task title
            
            Task file content:
            {task_content}
            
            Respond with valid JSON only, no additional text:
            {{
                "type": "task_type_here",
                "target_files": ["file1", "file2"],
                "main_request": "description here",
                "priority": "priority_here",
                "title": "title here"
            }}
            """
            
            response = self.client.generate(
                model=config.llama.model,
                prompt=prompt,
                format='json',
                options={'temperature': 0.1}  # Low temperature for consistent parsing
            )
            
            result = json.loads(response['response'])
            logger.info("Successfully parsed task with LLAMA")
            return result
            
        except Exception as e:
            logger.warning(f"LLAMA parsing failed, falling back to simple parser: {e}")
            return self._parse_with_fallback(task_content)
    
    def _parse_with_fallback(self, task_content: str) -> Dict[str, Any]:
        """Simple rule-based parsing fallback"""
        
        # Split into frontmatter and body
        parts = task_content.split('---', 2)
        
        result = {
            "type": "analyze",  # Default type
            "target_files": [],
            "main_request": "",
            "priority": "medium",
            "title": "Task"
        }
        
        try:
            # Parse YAML frontmatter if available
            if len(parts) >= 3:
                import yaml
                frontmatter = yaml.safe_load(parts[1])
                if frontmatter:
                    result["type"] = frontmatter.get("type", "analyze")
                    result["priority"] = frontmatter.get("priority", "medium")
                
                body = parts[2].strip()
            else:
                body = task_content
            
            # Extract title (first # heading)
            title_match = re.search(r'^# (.+)$', body, re.MULTILINE)
            if title_match:
                result["title"] = title_match.group(1).strip()
            
            # Extract target files
            target_files_match = re.search(r'\*\*Target Files:\*\*\s*\n((?:- .+\n?)+)', body, re.MULTILINE)
            if target_files_match:
                files_text = target_files_match.group(1)
                result["target_files"] = [
                    line.strip('- ').strip() 
                    for line in files_text.split('\n') 
                    if line.strip().startswith('-')
                ]
            
            # Extract main prompt
            prompt_match = re.search(r'\*\*Prompt:\*\*\s*\n(.+?)(?=\n\*\*|\n##|\Z)', body, re.DOTALL)
            if prompt_match:
                result["main_request"] = prompt_match.group(1).strip()
            else:
                # Fallback: use the whole body as request
                result["main_request"] = body[:500] + "..." if len(body) > 500 else body
            
            logger.info("Successfully parsed task with fallback parser")
            return result
            
        except Exception as e:
            logger.error(f"Fallback parsing failed: {e}")
            # Return minimal valid structure
            return {
                "type": "analyze",
                "target_files": [],
                "main_request": "Parse task content and proceed with analysis",
                "priority": "medium",
                "title": "Auto-generated Task"
            }
    
    def create_claude_prompt(self, parsed_task: Dict[str, Any]) -> str:
        """Create Claude-optimized prompt"""
        
        if self.ollama_available and self.client and self.model_installed:
            return self._create_prompt_with_llama(parsed_task)
        else:
            return self._create_prompt_with_template(parsed_task)
    
    def _create_prompt_with_llama(self, parsed_task: Dict[str, Any]) -> str:
        """Use LLAMA to create an optimized Claude prompt"""
        try:
            prompt = f"""
            Create an optimized prompt for Claude Code to execute this task:
            
            Task details:
            - Type: {parsed_task['type']}
            - Title: {parsed_task['title']}
            - Files: {parsed_task['target_files']}
            - Request: {parsed_task['main_request']}
            - Priority: {parsed_task['priority']}
            
            Create a clear, actionable prompt that:
            1. Explains the task context
            2. Specifies exactly what to do
            3. Lists the target files
            4. Includes success criteria
            5. Asks for a summary of changes
            
            Generate only the prompt text, no additional formatting:
            """
            
            response = self.client.generate(
                model=config.llama.model,
                prompt=prompt,
                options={'temperature': 0.3}
            )
            
            return response['response'].strip()
            
        except Exception as e:
            logger.warning(f"LLAMA prompt creation failed, using template: {e}")
            return self._create_prompt_with_template(parsed_task)
    
    def _create_prompt_with_template(self, parsed_task: Dict[str, Any]) -> str:
        """Create prompt using a template (task-type aware)"""
        
        task_type = (parsed_task.get('type') or 'analyze').lower()
        
        header = f"""Task: {task_type.upper()} - {parsed_task['title']}

Priority: {parsed_task['priority'].upper()}

Description:
{parsed_task['main_request']}"""
        
        files_block = ""
        if parsed_task['target_files']:
            files_block = f"""

Target Files:
{chr(10).join('- ' + f for f in parsed_task['target_files'])}"""
        
        if task_type in ("summarize", "code_review"):
            body = """

Please:
1. Read the specified files
2. Provide a concise {} of the content with key insights
3. Do not modify any files or execute commands
4. Note any limitations encountered
""".format("summary" if task_type == "summarize" else "code review")
        else:
            body = """

Please:
1. Analyze the current state of the specified files
2. Implement the requested changes following best practices
3. Provide a clear summary of what was accomplished
4. Note any issues or limitations encountered
5. Ensure all changes are properly tested where applicable
"""
        
        footer = """
Focus on quality, maintainability, and following established code conventions."""
        
        return header + files_block + body + "\n" + footer
    
    def summarize_result(self, result: TaskResult, original_task: Task) -> str:
        """Create concise summary for user notification"""
        
        if self.ollama_available and self.client and self.model_installed:
            return self._summarize_with_llama(result, original_task)
        else:
            return self._summarize_with_template(result, original_task)
    
    def _summarize_with_llama(self, result: TaskResult, original_task: Task) -> str:
        """Use LLAMA to create a summary"""
        try:
            prompt = f"""
            Summarize this task execution for a busy developer:
            
            Original task: {original_task.title}
            Task type: {original_task.type.value}
            Success: {result.success}
            Execution time: {result.execution_time:.2f}s
            
            Result output:
            {result.output[:1000]}...
            
            Provide a concise summary (max 200 words) with:
            1. What was accomplished (1-2 sentences)
            2. Key findings or changes (max 3 bullet points)
            3. Status: SUCCESS/PARTIAL/FAILED
            4. Next steps if any
            
            Keep it actionable and focused:
            """
            
            response = self.client.generate(
                model=config.llama.model,
                prompt=prompt,
                options={'temperature': 0.2}
            )
            
            return response['response'].strip()
            
        except Exception as e:
            logger.warning(f"LLAMA summarization failed, using template: {e}")
            return self._summarize_with_template(result, original_task)
    
    def _summarize_with_template(self, result: TaskResult, original_task: Task) -> str:
        """Create summary using a template"""
        
        status = "SUCCESS" if result.success else "FAILED"
        
        summary = f"""Task: {original_task.title}
Status: {status}
Duration: {result.execution_time:.1f}s

"""
        
        if result.success:
            summary += "✓ Task completed successfully"
            if result.files_modified:
                summary += f"\n✓ Modified {len(result.files_modified)} files"
        else:
            summary += "✗ Task failed"
            if result.errors:
                summary += f"\n✗ Errors: {'; '.join(result.errors[:2])}"
        
        if result.output:
            # Extract key information from output
            output_preview = result.output[:200].replace('\n', ' ')
            summary += f"\n\nOutput: {output_preview}..."
        
        return summary
    
    def get_status(self) -> Dict[str, Any]:
        """Get mediator status for debugging"""
        return {
            "ollama_available": self.ollama_available,
            "client_initialized": self.client is not None,
            "model": config.llama.model if self.ollama_available else "fallback",
            "mode": "LLAMA" if self.ollama_available else "FALLBACK"
        }