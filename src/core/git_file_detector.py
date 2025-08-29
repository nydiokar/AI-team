"""
Git-based file change detector - Simple and reliable way to detect file changes
"""
import subprocess
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class GitFileDetector:
    """Detects file changes using git commands"""
    
    def __init__(self, repo_path: Optional[str] = None):
        """Initialize with repository path"""
        self.repo_path = Path(repo_path) if repo_path else Path.cwd()
        
        # Verify this is a git repository
        if not self._is_git_repo():
            logger.warning(f"Not a git repository: {self.repo_path}")
            self.repo_path = None
    
    def _is_git_repo(self) -> bool:
        """Check if the path is a git repository"""
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--git-dir'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=False
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Error checking git repo: {e}")
            return False
    
    def detect_file_changes(self) -> Dict[str, List[str]]:
        """Detect all file changes in the repository"""
        if not self.repo_path:
            return {"modified": [], "created": [], "deleted": []}
        
        try:
            # Get git status in porcelain format (machine-readable)
            result = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            
            changes = {
                "modified": [],
                "created": [],
                "deleted": []
            }
            
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                
                # Parse git status line
                # Format: XY PATH
                # X = status of index, Y = status of working tree
                status = line[:2]
                file_path = line[3:].strip()
                
                if not file_path:
                    continue
                
                # Filter out build artifacts and dependencies
                if self._should_exclude_file(file_path):
                    continue
                
                # Categorize changes
                if status in ['M ', 'MM', 'A ']:  # Modified or Added
                    if status == 'A ':  # Added (new file)
                        changes["created"].append(file_path)
                    else:  # Modified
                        changes["modified"].append(file_path)
                elif status in ['D ', ' R']:  # Deleted or Renamed
                    changes["deleted"].append(file_path)
                elif status == '??':  # Untracked (new file)
                    changes["created"].append(file_path)
            
            return changes
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Git command failed: {e}")
            return {"modified": [], "created": [], "deleted": []}
        except Exception as e:
            logger.error(f"Error detecting file changes: {e}")
            return {"modified": [], "created": [], "deleted": []}
    
    def _should_exclude_file(self, file_path: str) -> bool:
        """Check if a file should be excluded from change detection"""
        file_path_lower = file_path.lower()
        
        # Exclude build artifacts and dependencies
        exclude_patterns = [
            'dist/', 'node_modules/', 'build/', 'target/',
            '.git/', '.vscode/', '.idea/', 'coverage/',
            'tmp/', 'temp/', 'logs/', 'cache/',
            '*.d.ts', '*.js.map', '*.css.map', '*.min.js', '*.min.css'
        ]
        
        for pattern in exclude_patterns:
            if pattern in file_path_lower:
                return True
        
        return False
    
    def get_changes_summary(self, changes: Dict[str, List[str]]) -> str:
        """Generate a human-readable summary of file changes"""
        summary_parts = []
        
        total_files = len(changes["modified"]) + len(changes["created"]) + len(changes["deleted"])
        if total_files > 0:
            summary_parts.append(f"Files changed: {total_files} total")
            
            if changes["modified"]:
                summary_parts.append(f"  Modified: {len(changes['modified'])} files")
                for file_path in changes["modified"][:5]:  # Show first 5
                    summary_parts.append(f"    * {file_path}")
                if len(changes["modified"]) > 5:
                    summary_parts.append(f"    ... and {len(changes['modified']) - 5} more")
            
            if changes["created"]:
                summary_parts.append(f"  Created: {len(changes['created'])} files")
                for file_path in changes["created"][:5]:  # Show first 5
                    summary_parts.append(f"    + {file_path}")
                if len(changes["created"]) > 5:
                    summary_parts.append(f"    ... and {len(changes['created']) - 5} more")
            
            if changes["deleted"]:
                summary_parts.append(f"  Deleted: {len(changes['deleted'])} files")
                for file_path in changes["deleted"][:5]:  # Show first 5
                    summary_parts.append(f"    - {file_path}")
                if len(changes["deleted"]) > 5:
                    summary_parts.append(f"    ... and {len(changes['deleted']) - 5} more")
        else:
            summary_parts.append("No file changes detected")
        
        return "\n".join(summary_parts)
