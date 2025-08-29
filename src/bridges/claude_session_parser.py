"""
Simplified Claude Session Parser - Focuses only on essential artifacts
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime
import re

logger = logging.getLogger(__name__)

class ClaudeSessionParser:
    """Parses Claude's session files to detect tool usage and file changes"""
    
    def __init__(self, claude_projects_dir: Optional[str] = None):
        """Initialize with Claude projects directory path"""
        if claude_projects_dir:
            self.claude_projects_dir = Path(claude_projects_dir)
        else:
            self.claude_projects_dir = self._find_claude_projects_dir()
        
        if not self.claude_projects_dir or not self.claude_projects_dir.exists():
            logger.warning(f"Claude projects directory not found: {claude_projects_dir}")
            self.claude_projects_dir = None
    
    def _find_claude_projects_dir(self) -> Optional[Path]:
        """Try to find Claude projects directory automatically"""
        possible_paths = [
            Path.home() / ".claude" / "projects",
            Path.home() / "AppData" / "Roaming" / "Claude" / "projects",
            Path.home() / "Library" / "Application Support" / "Claude" / "projects",
        ]
        
        for path in possible_paths:
            if path.exists():
                logger.info(f"Found Claude projects directory: {path}")
                return path
        
        return None
    
    def find_session_files(self, working_dir: str, since_timestamp: Optional[datetime] = None) -> List[Path]:
        """Find Claude session files for a specific working directory"""
        if not self.claude_projects_dir:
            return []
        
        session_files = []
        
        try:
            # Look for session files in the projects directory
            for project_dir in self.claude_projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                
                # Look for .jsonl files in project directories
                for jsonl_file in project_dir.glob("*.jsonl"):
                    try:
                        # Check if this session file is relevant to our working directory
                        if self._is_session_relevant(jsonl_file, working_dir, since_timestamp):
                            session_files.append(jsonl_file)
                    except Exception as e:
                        logger.debug(f"Error checking session file {jsonl_file}: {e}")
                        continue
            
            # Sort by modification time (newest first)
            session_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            
        except Exception as e:
            logger.error(f"Error searching for session files: {e}")
        
        return session_files
    
    def _is_session_relevant(self, session_file: Path, working_dir: str, since_timestamp: Optional[datetime] = None) -> bool:
        """Check if a session file is relevant to our working directory and timeframe"""
        try:
            # Check file modification time if timestamp filter is applied
            if since_timestamp:
                file_mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
                if file_mtime < since_timestamp:
                    return False
            
            # Read the session file to check if it's relevant
            with open(session_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f):
                    try:
                        if line.strip():
                            data = json.loads(line)
                            
                            # Check if this line contains relevant information
                            if self._is_line_relevant(data, working_dir):
                                return True
                                
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        logger.debug(f"Error parsing line {line_num} in {session_file}: {e}")
                        continue
            
            return False
            
        except Exception as e:
            logger.debug(f"Error checking session file relevance: {e}")
            return False
    
    def _is_line_relevant(self, data: Dict[str, Any], working_dir: str) -> bool:
        """Check if a JSON line from the session file is relevant"""
        try:
            # Check if the working directory matches
            if 'cwd' in data:
                session_cwd = str(data['cwd']).lower()
                target_cwd = str(working_dir).lower()
                
                # Check if the working directories match or are related
                if session_cwd == target_cwd:
                    return True
                if target_cwd in session_cwd or session_cwd in target_cwd:
                    return True
            
            # Check if there are tool uses that indicate file operations
            if 'message' in data and 'content' in data['message']:
                content = data['message']['content']
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and 'tool_use_id' in item:
                            # This indicates Claude used a tool (possibly file operations)
                            return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Error checking line relevance: {e}")
            return False
    
    def parse_file_changes(self, session_files: List[Path]) -> Dict[str, List[str]]:
        """Parse session files to extract tool usage and file changes"""
        changes = {
            "created": [],
            "modified": [],
            "deleted": [],
            "tool_uses": []
        }
        
        for session_file in session_files:
            try:
                file_changes = self._parse_session_file(session_file)
                
                # Merge changes from this file
                for change_type in changes:
                    if change_type in file_changes:
                        changes[change_type].extend(file_changes[change_type])
                
            except Exception as e:
                logger.warning(f"Error parsing session file {session_file}: {e}")
                continue
        
        # Remove duplicates while preserving order
        for change_type in changes:
            seen = set()
            unique_changes = []
            for change in changes[change_type]:
                if change not in seen:
                    seen.add(change)
                    unique_changes.append(change)
            changes[change_type] = unique_changes
        
        return changes
    
    def _parse_session_file(self, session_file: Path) -> Dict[str, List[str]]:
        """Parse a single session file to extract tool usage and file changes"""
        changes = {
            "created": [],
            "modified": [],
            "deleted": [],
            "tool_uses": []
        }
        
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f):
                    try:
                        if line.strip():
                            data = json.loads(line)
                            line_changes = self._parse_session_line(data)
                            
                            # Merge changes from this line
                            for change_type in changes:
                                if change_type in line_changes:
                                    changes[change_type].extend(line_changes[change_type])
                                    
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        logger.debug(f"Error parsing line {line_num} in {session_file}: {e}")
                        continue
                        
        except Exception as e:
            logger.error(f"Error reading session file {session_file}: {e}")
        
        return changes
    
    def _parse_session_line(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Parse a single JSON line from a session file - FOCUS ON ESSENTIALS ONLY"""
        changes = {
            "created": [],
            "modified": [],
            "deleted": [],
            "tool_uses": []
        }
        
        try:
            # Look for tool use information - this is the most reliable source
            if 'message' in data and 'content' in data['message']:
                content = data['message']['content']
                
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            # Check for tool use
                            if 'tool_use_id' in item:
                                tool_type = item.get('type', 'unknown')
                                changes["tool_uses"].append(f"Tool used: {tool_type}")
                                
                                # Check for edit_file tool specifically - this is direct evidence of file modification
                                if tool_type == 'edit_file':
                                    target_file = item.get('target_file', '')
                                    if target_file:
                                        # If this is a new file path, it was created
                                        # If it's an existing path, it was modified
                                        # For now, assume modified (most common case)
                                        changes["modified"].append(target_file)
                            
                            # Check for tool results that might indicate file operations
                            if 'type' in item and item['type'] == 'tool_result':
                                tool_result = item.get('content', '')
                                if isinstance(tool_result, str):
                                    # Look for file paths in tool results
                                    file_paths = self._extract_file_paths_from_text(tool_result)
                                    for file_path in file_paths:
                                        if file_path not in changes["modified"]:
                                            changes["modified"].append(file_path)
                
                elif isinstance(content, str):
                    # Look for file paths in text content
                    file_paths = self._extract_file_paths_from_text(content)
                    for file_path in file_paths:
                        if file_path not in changes["modified"]:
                            changes["modified"].append(file_path)
            
            # Look for output that might contain file paths
            if 'output' in data:
                output = data['output']
                if isinstance(output, str):
                    file_paths = self._extract_file_paths_from_text(output)
                    for file_path in file_paths:
                        if file_path not in changes["modified"]:
                            changes["modified"].append(file_path)
                        
        except Exception as e:
            logger.debug(f"Error parsing session line: {e}")
        
        return changes
    
    def _extract_file_paths_from_text(self, text: str) -> List[str]:
        """Extract file paths from text content using simple pattern matching"""
        file_paths = []
        
        try:
            # Look for Windows and Unix file paths
            file_path_pattern = r'([a-zA-Z]:\\[^\s\n\r]+|/[^\s\n\r]+|\./[^\s\n\r]+)'
            matches = re.findall(file_path_pattern, text)
            
            for file_path in matches:
                file_path = file_path.strip()
                if file_path and file_path not in file_paths:
                    file_paths.append(file_path)
                    
        except Exception as e:
            logger.debug(f"Error extracting file paths from text: {e}")
        
        return file_paths
    
    def get_changes_summary(self, changes: Dict[str, List[str]]) -> str:
        """Generate a human-readable summary focusing on essentials"""
        summary_parts = []
        
        # Count tool uses - this is what you asked for
        if changes["tool_uses"]:
            tool_counts = {}
            for tool_use in changes["tool_uses"]:
                tool_name = tool_use.split(": ")[1] if ": " in tool_use else "unknown"
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            
            summary_parts.append(f"Tools used: {len(changes['tool_uses'])} total operations")
            for tool, count in tool_counts.items():
                summary_parts.append(f"  {tool}: {count} times")
        
        # Count file changes - this is what you asked for
        total_files = len(changes["modified"]) + len(changes["created"]) + len(changes["deleted"])
        if total_files > 0:
            summary_parts.append(f"\nFiles affected: {total_files} total")
            
            if changes["modified"]:
                summary_parts.append(f"  Modified: {len(changes['modified'])} files")
                for file_path in changes["modified"][:3]:  # Show first 3
                    summary_parts.append(f"    * {file_path}")
                if len(changes["modified"]) > 3:
                    summary_parts.append(f"    ... and {len(changes['modified']) - 3} more")
            
            if changes["created"]:
                summary_parts.append(f"  Created: {len(changes['created'])} files")
                for file_path in changes["created"][:3]:  # Show first 3
                    summary_parts.append(f"    + {file_path}")
                if len(changes["created"]) > 3:
                    summary_parts.append(f"    ... and {len(changes['created']) - 3} more")
            
            if changes["deleted"]:
                summary_parts.append(f"  Deleted: {len(changes['deleted'])} files")
                for file_path in changes["deleted"][:3]:  # Show first 3
                    summary_parts.append(f"    - {file_path}")
                if len(changes["deleted"]) > 3:
                    summary_parts.append(f"    ... and {len(changes['deleted']) - 3} more")
        else:
            summary_parts.append("No file changes detected")
        
        return "\n".join(summary_parts)
