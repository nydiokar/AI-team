"""
LLAMA mediator with intelligent fallback modes
"""
import json
import re
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from pathlib import Path

import sys
import os

from src.core import ILlamaMediator, Task, TaskResult, TaskType
from config import config

logger = logging.getLogger(__name__)

class LlamaMediator(ILlamaMediator):
    """LLAMA mediator with automatic fallback to simple parsing.

    Provides three roles when available:
    - Parse task files into normalized fields (type, targets, main request)
    - Create optimized Claude prompts (task-type aware)
    - Summarize results into concise human-readable summaries
    """
    
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
        """Check if Ollama is running and accessible."""
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
        """Check if specified model is installed locally to avoid long pulls."""
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
        """Parse task content using LLAMA or fallback to simple parsing."""
        # Enforce content size cap to avoid timeouts/memory pressure
        max_chars = getattr(config.llama, "max_parse_chars", 200_000)
        if len(task_content) > max_chars:
            logger.info(
                f"event=truncate_parse before_chars={len(task_content)} after_chars={max_chars}"
            )
            task_content = task_content[:max_chars]

        if self.ollama_available and self.client and self.model_installed:
            return self._parse_with_llama(task_content)
        else:
            if self.ollama_available and not self.model_installed:
                logger.info("LLAMA model not installed; using fallback parser to avoid long downloads")
            return self._parse_with_fallback(task_content)
    
    def _parse_with_llama(self, task_content: str) -> Dict[str, Any]:
        """Parse using LLAMA/Ollama."""
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
        """Simple rule-based parsing fallback."""
        
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
                try:
                    frontmatter = yaml.safe_load(parts[1])
                    if frontmatter:
                        result["type"] = frontmatter.get("type", "analyze")
                        result["priority"] = frontmatter.get("priority", "medium")
                except yaml.YAMLError:
                    logger.warning("Invalid YAML frontmatter, using defaults")
                    frontmatter = {}
                
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
            prompt_match = re.search(r'\*\*Prompt:\*\*\s*\n(.+?)(?=\n\*\*[A-Za-z]|\n##|\Z)', body, re.DOTALL)
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
        """Create Claude-optimized prompt."""
        
        if self.ollama_available and self.client and self.model_installed:
            return self._create_prompt_with_llama(parsed_task)
        else:
            return self._create_prompt_with_template(parsed_task)
    
    def _create_prompt_with_llama(self, parsed_task: Dict[str, Any]) -> str:
        """Use LLAMA to create an optimized Claude prompt."""
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
        """Create prompt using the modular agent system."""
        
        task_type = (parsed_task.get('type') or 'analyze').lower()
        
        # Get the appropriate agent for this task type
        from src.core.agent_manager import AgentManager
        agent_manager = AgentManager()
        
        # Map task type to agent name
        type_to_agent = {
            'analyze': 'analyze',
            'fix': 'bug_fix',
            'bug_fix': 'bug_fix',
            'code_review': 'code_review',
            'documentation': 'documentation',
            'summarize': 'analyze'
        }
        
        agent_name = type_to_agent.get(task_type, 'analyze')
        agent = agent_manager.get_agent(agent_name)
        
        if agent:
            # Use agent-specific instructions
            agent_instructions = agent.get_agent_instructions()
        else:
            # Fallback to generic instructions when agents are disabled or unavailable
            if task_type == 'code_review':
                agent_instructions = "Please review the code for quality, security, and best practices. Focus on identifying potential issues, code smells, and areas for improvement."
            elif task_type in ('fix', 'bug_fix'):
                agent_instructions = "Please identify and fix the reported issue. Ensure the solution is robust, well-tested, and follows established coding patterns."
            elif task_type == 'documentation':
                agent_instructions = "Please create or update documentation as requested. Ensure clarity, completeness, and consistency with existing documentation."
            elif task_type == 'summarize':
                agent_instructions = "Please provide a clear, concise summary of the requested information or analysis."
            else:
                agent_instructions = "Please analyze and provide insights on the requested task. Focus on understanding the current state and providing actionable recommendations."
        
        # Build the prompt with general system instructions + agent-specific instructions
        header = f"""Task: {task_type.upper()} - {parsed_task['title']}

Priority: {parsed_task['priority'].upper()}

Description:
{parsed_task['main_request']}"""
        
        files_block = ""
        if parsed_task['target_files']:
            files_block = f"""

Target Files:
{chr(10).join('- ' + f for f in parsed_task['target_files'])}"""
        else:
            files_block = """

Target Files: To be discovered (search and identify relevant files first)"""
        
        # Agent-specific instructions
        agent_block = f"""

Agent Instructions:
{agent_instructions}"""
        
        # General system instructions
        system_block = """

General Instructions:
1. Follow the agent's specific instructions above
2. If no target files are specified, first identify relevant files by searching the repository
3. Provide clear, actionable output
4. Include a changelog section at the end if files are modified:
   Created: path/to/file
   Modified: path/to/file
5. Note any limitations or issues encountered

Focus on quality, maintainability, and following established code conventions."""
        
        prompt = header + files_block + agent_block + system_block
        
        # Cap prompt size per config to keep Claude requests reliable
        max_chars = getattr(config.llama, "max_prompt_chars", 32_000)
        if len(prompt) > max_chars:
            logger.info(
                f"event=truncate_prompt before_chars={len(prompt)} after_chars={max_chars}"
            )
            prompt = prompt[:max_chars]
        return prompt
    
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
            # Extract key information from output with size cap
            preview_source = result.output
            max_input = getattr(config.llama, "max_summary_input_chars", 40_000)
            if len(preview_source) > max_input:
                preview_source = preview_source[:max_input]
            output_preview = preview_source[:200].replace('\n', ' ')
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

    # --- Agent expansion (command-driven) ---
    def expand_agent_intent(self, agent: str, intent_text: str, files: Optional[List[str]] = None) -> Dict[str, Any]:
        """Expand an agent command + brief intent into a structured enriched task using few-shot templates.

        Returns a dict with keys: type, title, prompt, target_files, metadata.cwd (optional).
        """
        agent_norm = (agent or "").strip().lower().replace("-", "_")
        files = files or []

        # Resolve safe working directory hint from phrases like: new directory called "name"
        cwd_hint: Optional[str] = None
        try:
            m = re.search(r"new\s+directory\s+called\s+\"?([^\"\n]+)\"?", intent_text, re.IGNORECASE)
            if m:
                safe_name = re.sub(r"[^A-Za-z0-9 _.-]", "", m.group(1)).strip().strip(". ")
                base = config.claude.base_cwd
                if base:
                    sep = "\\" if "\\" in base else "/"
                    cwd_hint = f"{base.rstrip(sep)}{sep}{safe_name}"
        except Exception:
            cwd_hint = None

        # Build target files list from provided paths (sanitized, capped)
        target_files: List[str] = []
        for p in files[:20]:
            try:
                target_files.append(str(p))
            except Exception:
                continue

        # Enforce template-driven expansion via LLAMA
        if not self._load_agent_template(agent_norm):
            available = ", ".join(self.list_available_agents())
            raise ValueError(f"Unknown agent '{agent_norm}'. Available: {available}")
        if not (self.ollama_available and self.client and self.model_installed):
            raise RuntimeError("LLAMA (Ollama) is not available; cannot expand agent template")
        return self._expand_with_llama_template(agent_norm, intent_text, target_files, cwd_hint)

    def _load_agent_template(self, agent: str) -> Optional[str]:
        try:
            # Project root = .../AI-team; this file lives under src/bridges/
            root = Path(__file__).resolve().parents[2]
            path = root / "prompts" / "agents" / f"{agent}.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        except Exception:
            pass
        return None

    def list_available_agents(self) -> List[str]:
        try:
            root = Path(__file__).resolve().parents[2]
            glob = (root / "prompts" / "agents").glob("*.md")
            return sorted(p.stem for p in glob)
        except Exception:
            return []

    def _expand_with_llama_template(self, agent: str, intent_text: str, target_files: List[str], cwd_hint: Optional[str]) -> Dict[str, Any]:
        """Use few-shot prompt template to expand into a structured enriched task (JSON)."""
        template = self._load_agent_template(agent)
        if not template:
            raise RuntimeError(f"Template not found for agent: {agent}")
        # Build context payload
        payload = {
            "agent": agent,
            "intent": intent_text,
            "target_files": target_files,
            "cwd_hint": cwd_hint or "",
            "template_id": f"{agent}-v1"
        }
        prompt = (
            template.strip()
            + "\n\n" 
            + "Context:" 
            + "\n" 
            + json.dumps(payload, ensure_ascii=False)
            + "\n\n"
            + "Respond with only a single JSON object with keys: type, title, prompt, target_files, metadata."
        )
        response = self.client.generate(
            model=config.llama.model,
            prompt=prompt,
            format='json',
            options={'temperature': 0.2}
        )
        enriched = json.loads(response['response'])
        # Basic normalization
        enriched.setdefault("type", "analyze")
        enriched.setdefault("title", "Enriched Task")
        enriched.setdefault("target_files", target_files)
        meta = enriched.setdefault("metadata", {})
        if cwd_hint and not meta.get("cwd"):
            meta["cwd"] = cwd_hint
        return enriched