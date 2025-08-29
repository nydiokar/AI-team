"""
Git-based file change detector - Simple and reliable way to detect file changes
"""
import subprocess
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

class GitFileDetector:
    """Detects file changes using git commands and provides git automation"""
    
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
    
    def get_current_branch(self) -> Optional[str]:
        """Get the current branch name"""
        if not self.repo_path:
            return None
        
        try:
            result = subprocess.run(
                ['git', 'branch', '--show-current'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get current branch: {e}")
            return None
        except Exception as e:
            logger.error(f"Error getting current branch: {e}")
            return None
    
    def get_git_diff(self, staged_only: bool = False) -> Optional[str]:
        """Get git diff for current changes"""
        if not self.repo_path:
            return None
        
        try:
            if staged_only:
                result = subprocess.run(
                    ['git', 'diff', '--cached'],
                    cwd=self.repo_path,
                    capture_output=True,
                    text=True,
                    check=True
                )
            else:
                result = subprocess.run(
                    ['git', 'diff'],
                    cwd=self.repo_path,
                    capture_output=True,
                    text=True,
                    check=True
                )
            return result.stdout
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get git diff: {e}")
            return None
        except Exception as e:
            logger.error(f"Error getting git diff: {e}")
            return None
    
    def stage_files(self, file_patterns: Optional[List[str]] = None) -> bool:
        """Stage files for commit"""
        if not self.repo_path:
            return False
        
        try:
            if file_patterns:
                # Stage specific files
                for pattern in file_patterns:
                    subprocess.run(
                        ['git', 'add', pattern],
                        cwd=self.repo_path,
                        check=True
                    )
            else:
                # Stage all changes
                subprocess.run(
                    ['git', 'add', '.'],
                    cwd=self.repo_path,
                    check=True
                )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to stage files: {e}")
            return False
        except Exception as e:
            logger.error(f"Error staging files: {e}")
            return False
    
    def create_feature_branch(self, task_id: str, description: str) -> Optional[str]:
        """Create a feature branch for a task"""
        if not self.repo_path:
            return None
        
        try:
            # Clean description for branch name
            clean_desc = re.sub(r'[^a-zA-Z0-9\s-]', '', description)
            clean_desc = re.sub(r'\s+', '-', clean_desc).strip('-')
            clean_desc = clean_desc[:30]  # Limit length
            
            branch_name = f"feature/task-{task_id}-{clean_desc}"
            
            # Check if branch already exists
            result = subprocess.run(
                ['git', 'branch', '--list', branch_name],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            
            if result.stdout.strip():
                logger.warning(f"Branch {branch_name} already exists")
                return branch_name
            
            # Create and checkout new branch
            subprocess.run(
                ['git', 'checkout', '-b', branch_name],
                cwd=self.repo_path,
                check=True
            )
            
            logger.info(f"Created and switched to branch: {branch_name}")
            return branch_name
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create feature branch: {e}")
            return None
        except Exception as e:
            logger.error(f"Error creating feature branch: {e}")
            return None
    
    def commit_changes(self, commit_message: str) -> bool:
        """Commit staged changes with the given message"""
        if not self.repo_path:
            return False
        
        try:
            result = subprocess.run(
                ['git', 'commit', '-m', commit_message],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            logger.info(f"Committed changes: {commit_message}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to commit changes: {e}")
            return False
        except Exception as e:
            logger.error(f"Error committing changes: {e}")
            return False
    
    def push_branch(self, branch_name: Optional[str] = None) -> bool:
        """Push the current branch to remote"""
        if not self.repo_path:
            return False
        
        try:
            if not branch_name:
                branch_name = self.get_current_branch()
            
            if not branch_name:
                logger.error("No branch name specified and could not determine current branch")
                return False
            
            result = subprocess.run(
                ['git', 'push', 'origin', branch_name],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            logger.info(f"Pushed branch {branch_name} to remote")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to push branch: {e}")
            return False
        except Exception as e:
            logger.error(f"Error pushing branch: {e}")
            return False
    
    def get_commit_history(self, limit: int = 10) -> List[Dict[str, str]]:
        """Get recent commit history"""
        if not self.repo_path:
            return []
        
        try:
            result = subprocess.run(
                ['git', 'log', f'--max-count={limit}', '--pretty=format:%H|%an|%ad|%s', '--date=short'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            
            commits = []
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                
                parts = line.split('|')
                if len(parts) == 4:
                    commits.append({
                        'hash': parts[0],
                        'author': parts[1],
                        'date': parts[2],
                        'message': parts[3]
                    })
            
            return commits
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get commit history: {e}")
            return []
        except Exception as e:
            logger.error(f"Error getting commit history: {e}")
            return []
    
    def is_working_directory_clean(self) -> bool:
        """Check if working directory is clean (no uncommitted changes)"""
        if not self.repo_path:
            return True
        
        try:
            result = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            return not result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to check working directory status: {e}")
            return False
        except Exception as e:
            logger.error(f"Error checking working directory status: {e}")
            return False
    
    def get_staged_files(self) -> List[str]:
        """Get list of staged files"""
        if not self.repo_path:
            return []
        
        try:
            result = subprocess.run(
                ['git', 'diff', '--cached', '--name-only'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            
            files = []
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    files.append(line.strip())
            
            return files
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get staged files: {e}")
            return []
        except Exception as e:
            logger.error(f"Error getting staged files: {e}")
            return []
    
    def detect_file_changes(self) -> Dict[str, List[str]]:
        """Detect all file changes in the repository"""
        if not self.repo_path:
            return {"modified": [], "created": [], "deleted": [], "total": 0}
        
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
            
            # Add total count
            changes["total"] = len(changes["modified"]) + len(changes["created"]) + len(changes["deleted"])
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
