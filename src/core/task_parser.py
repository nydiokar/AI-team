"""
Task file parser implementation
"""
import re
import yaml
from datetime import datetime
from typing import List, Dict, Any
from pathlib import Path

from .interfaces import ITaskParser, Task, TaskType, TaskPriority, TaskStatus

class TaskParser(ITaskParser):
    """Parse `.task.md` files into `Task` objects.

    Expects YAML frontmatter followed by Markdown sections. Extracts:
    - Title (`# Heading`)
    - Target files (`**Target Files:**` list)
    - Prompt (`**Prompt:**` block)
    - Success criteria (checkbox list)
    - Context (`**Context:**` block)
    """
    
    def parse_task_file(self, file_path: str) -> Task:
        """Parse a `.task.md` file into a `Task` instance."""
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Split content into YAML frontmatter and markdown body
        parts = content.split('---', 2)
        if len(parts) < 3:
            raise ValueError("Task file must have YAML frontmatter")
        
        # Parse YAML frontmatter
        try:
            frontmatter = yaml.safe_load(parts[1])
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML frontmatter: {e}")
        
        # Parse markdown body
        body = parts[2].strip()
        
        # Extract required fields from frontmatter
        task_id = frontmatter.get('id')
        if not task_id:
            # Generate ID from filename if not provided
            task_id = Path(file_path).stem
        
        task_type = self._parse_task_type(frontmatter.get('type'))
        priority = self._parse_priority(frontmatter.get('priority', 'medium'))
        created = frontmatter.get('created', datetime.now().isoformat())
        
        # Extract sections from markdown body
        sections = self._parse_markdown_sections(body)
        
        return Task(
            id=task_id,
            type=task_type,
            priority=priority,
            status=TaskStatus.PENDING,
            created=created,
            title=sections.get('title', f'Task {task_id}'),
            target_files=sections.get('target_files', []),
            prompt=sections.get('prompt', ''),
            success_criteria=sections.get('success_criteria', []),
            context=sections.get('context', ''),
            metadata=frontmatter
        )
    
    def validate_task_format(self, file_path: str) -> List[str]:
        """Validate task file format and return errors."""
        errors = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            return [f"Cannot read file: {e}"]
        
        # Check for YAML frontmatter
        if not content.startswith('---'):
            errors.append("File must start with YAML frontmatter (---)")
        
        parts = content.split('---', 2)
        if len(parts) < 3:
            errors.append("File must have complete YAML frontmatter")
            return errors
        
        # Validate YAML
        try:
            frontmatter = yaml.safe_load(parts[1])
        except yaml.YAMLError as e:
            errors.append(f"Invalid YAML frontmatter: {e}")
            return errors
        
        # Check required fields
        if not frontmatter.get('type'):
            errors.append("Missing required field: type")
        
        # Validate task type
        try:
            self._parse_task_type(frontmatter.get('type'))
        except ValueError as e:
            errors.append(str(e))
        
        # Validate priority
        try:
            self._parse_priority(frontmatter.get('priority', 'medium'))
        except ValueError as e:
            errors.append(str(e))
        
        return errors
    
    def _parse_task_type(self, type_str: str) -> TaskType:
        """Parse task type string into `TaskType` enum."""
        if not type_str:
            raise ValueError("Task type is required")
        
        type_map = {
            'code_review': TaskType.CODE_REVIEW,
            'summarize': TaskType.SUMMARIZE,
            'fix': TaskType.FIX,
            'analyze': TaskType.ANALYZE
        }
        
        if type_str not in type_map:
            raise ValueError(f"Invalid task type: {type_str}. Must be one of: {list(type_map.keys())}")
        
        return type_map[type_str]
    
    def _parse_priority(self, priority_str: str) -> TaskPriority:
        """Parse priority string into `TaskPriority` enum."""
        priority_map = {
            'high': TaskPriority.HIGH,
            'medium': TaskPriority.MEDIUM,
            'low': TaskPriority.LOW
        }
        
        if priority_str not in priority_map:
            raise ValueError(f"Invalid priority: {priority_str}. Must be one of: {list(priority_map.keys())}")
        
        return priority_map[priority_str]
    
    def _parse_markdown_sections(self, body: str) -> Dict[str, Any]:
        """Parse markdown body into sections.

        Uses conservative regexes that look ahead for the next section header to
        avoid truncating multi-line blocks.
        """
        sections = {}
        
        # Extract title (first # heading)
        title_match = re.search(r'^# (.+)$', body, re.MULTILINE)
        if title_match:
            sections['title'] = title_match.group(1).strip()
        
        # Extract target files
        target_files_match = re.search(r'\*\*Target Files:\*\*\s*\n((?:- .+\n?)+)', body, re.MULTILINE)
        if target_files_match:
            files_text = target_files_match.group(1)
            sections['target_files'] = [
                line.strip('- ').strip() 
                for line in files_text.split('\n') 
                if line.strip().startswith('-')
            ]
        
        # Extract prompt
        prompt_match = re.search(r'\*\*Prompt:\*\*\s*\n(.+?)(?=\n\*\*[A-Za-z]|\n##|\Z)', body, re.DOTALL)
        if prompt_match:
            sections['prompt'] = prompt_match.group(1).strip()
        
        # Extract success criteria
        criteria_match = re.search(r'\*\*Success Criteria:\*\*\s*\n((?:- \[.\] .+\n?)+)', body, re.MULTILINE)
        if criteria_match:
            criteria_text = criteria_match.group(1)
            sections['success_criteria'] = [
                re.sub(r'^- \[.\] ', '', line.strip())
                for line in criteria_text.split('\n')
                if line.strip().startswith('- [')
            ]
        
        # Extract context
        context_match = re.search(r'\*\*Context:\*\*\s*\n(.+?)(?=\n\*\*[A-Za-z]|\n##|\Z)', body, re.DOTALL)
        if context_match:
            sections['context'] = context_match.group(1).strip()
        
        return sections