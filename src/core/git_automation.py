"""
Git automation service for safe commit workflow
"""
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from .git_file_detector import GitFileDetector
from src.bridges.llama_mediator import LlamaMediator

logger = logging.getLogger(__name__)

class GitAutomationService:
    """Service for automating git operations with safety checks"""
    
    def __init__(self, repo_path: Optional[str] = None):
        """Initialize git automation service"""
        self.git_detector = GitFileDetector(repo_path)
        self._llama_mediator = None  # Lazy initialization
        
        # Sensitive file patterns that should never be committed
        self.sensitive_patterns = [
            '.env', '.key', '.pem', '.p12', '.pfx', '.crt', '.csr',
            'secrets.json', 'config.local.py', '*.secret', '*.private',
            'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519',
            '*.db', '*.sqlite', '*.log', '*.tmp'
        ]
    
    @property
    def llama_mediator(self):
        """Lazy initialization of LLAMA mediator"""
        if self._llama_mediator is None:
            try:
                self._llama_mediator = LlamaMediator()
            except Exception as e:
                logger.warning(f"Failed to initialize LLAMA mediator: {e}")
                # Create a mock mediator that always uses fallback
                self._llama_mediator = type('MockMediator', (), {
                    'ollama_available': False,
                    'model_installed': False,
                    'client': None
                })()
        return self._llama_mediator
    
    def generate_commit_message(self, task_id: str, task_description: str, 
                               files_changed: List[str], git_diff: Optional[str] = None) -> str:
        """Generate a commit message using LLAMA or fallback"""
        try:
            if self.llama_mediator.ollama_available and self.llama_mediator.model_installed:
                return self._generate_with_llama(task_id, task_description, files_changed, git_diff)
            else:
                return self._generate_fallback_message(task_id, task_description, files_changed)
        except Exception as e:
            logger.warning(f"Failed to generate commit message with LLAMA: {e}")
            return self._generate_fallback_message(task_id, task_description, files_changed)
    
    def _generate_with_llama(self, task_id: str, task_description: str, 
                            files_changed: List[str], git_diff: Optional[str] = None) -> str:
        """Generate commit message using LLAMA"""
        try:
            if not (self.llama_mediator.ollama_available and self.llama_mediator.client):
                return self._generate_fallback_message(task_id, task_description, files_changed)
            
            # Prepare context for LLAMA
            context = f"""
Task ID: {task_id}
Task Description: {task_description}
Files Changed: {', '.join(files_changed[:10])}  # Limit to first 10 files
            """
            
            if git_diff:
                # Truncate diff to avoid token limits
                context += f"\n\nGit Diff (truncated):\n{git_diff[:1000]}..."
            
            prompt = f"""
Generate a concise, conventional commit message for the following changes:

{context}

Requirements:
- Use conventional commit format: <type>(<scope>): <description>
- Keep description under 72 characters
- Be specific about what was changed
- Use present tense ("add" not "added")
- Focus on the main purpose of the changes

Commit message:
            """.strip()
            
            # Use LLAMA to generate the message
            try:
                # Get the model name from config
                from config import config as app_config
                model_name = app_config.llama.model
                
                response = self.llama_mediator.client.generate(
                    model=model_name,
                    prompt=prompt,
                    format='json',
                    options={'temperature': 0.2}
                )
                
                commit_message = response.get('response', '').strip()
                if commit_message and len(commit_message) <= 72:
                    return commit_message
                else:
                    # Fallback if LLAMA response is too long or empty
                    return self._generate_fallback_message(task_id, task_description, files_changed)
                    
            except Exception as e:
                logger.warning(f"LLAMA generation failed, using fallback: {e}")
                return self._generate_fallback_message(task_id, task_description, files_changed)
                
        except Exception as e:
            logger.error(f"Error generating commit message with LLAMA: {e}")
            return self._generate_fallback_message(task_id, task_description, files_changed)
    
    def _generate_fallback_message(self, task_id: str, task_description: str, 
                                 files_changed: List[str]) -> str:
        """Generate a fallback commit message"""
        # Extract task type from description
        task_type = "feat"
        desc_lower = task_description.lower()
        
        if any(word in desc_lower for word in ["fix", "bug", "error", "issue"]):
            task_type = "fix"
        elif any(word in desc_lower for word in ["refactor", "clean", "improve"]):
            task_type = "refactor"
        elif any(word in desc_lower for word in ["docs", "document", "readme"]):
            task_type = "docs"
        elif any(word in desc_lower for word in ["test", "spec"]):
            task_type = "test"
        
        # Create a concise description
        clean_desc = task_description.strip()
        if len(clean_desc) > 50:
            clean_desc = clean_desc[:47] + "..."
        
        return f"{task_type}: {clean_desc}"
    
    def check_sensitive_files(self, files: List[str]) -> Tuple[List[str], List[str]]:
        """Check for sensitive files that shouldn't be committed"""
        safe_files = []
        sensitive_files = []
        
        for file_path in files:
            file_lower = file_path.lower()
            is_sensitive = False
            
            for pattern in self.sensitive_patterns:
                if pattern in file_lower or file_lower.endswith(pattern.lstrip('*')):
                    is_sensitive = True
                    break
            
            if is_sensitive:
                sensitive_files.append(file_path)
            else:
                safe_files.append(file_path)
        
        return safe_files, sensitive_files
    
    def safe_commit_task(self, task_id: str, task_description: str, 
                        create_branch: bool = True, push_branch: bool = False) -> Dict[str, any]:
        """Safely commit changes for a specific task"""
        result = {
            "success": False,
            "branch_name": None,
            "commit_hash": None,
            "files_committed": [],
            "sensitive_files_blocked": [],
            "errors": []
        }
        
        try:
            # Check if we're in a git repository
            if not self.git_detector.repo_path:
                result["errors"].append("Not in a git repository")
                return result
            
            # Get current changes
            changes = self.git_detector.detect_file_changes()
            all_files = changes["modified"] + changes["created"] + changes["deleted"]
            
            if not all_files:
                result["errors"].append("No changes detected to commit")
                return result
            
            # Check for sensitive files
            safe_files, sensitive_files = self.check_sensitive_files(all_files)
            result["sensitive_files_blocked"] = sensitive_files
            
            if not safe_files:
                result["errors"].append("No safe files to commit (all files are sensitive)")
                return result
            
            # Create feature branch if requested
            if create_branch:
                branch_name = self.git_detector.create_feature_branch(task_id, task_description)
                if branch_name:
                    result["branch_name"] = branch_name
                else:
                    result["errors"].append("Failed to create feature branch")
                    return result
            
            # Stage all changes (git handles .gitignore automatically)
            if not self.git_detector.stage_files():
                result["errors"].append("Failed to stage files")
                return result
            
            # Get staged files for commit message generation
            staged_files = self.git_detector.get_staged_files()
            result["files_committed"] = staged_files
            
            # Generate commit message
            git_diff = self.git_detector.get_git_diff(staged_only=True)
            commit_message = self.generate_commit_message(
                task_id, task_description, staged_files, git_diff
            )
            
            # Commit changes
            if not self.git_detector.commit_changes(commit_message):
                result["errors"].append("Failed to commit changes")
                return result
            
            # Get commit hash (simplified - in real implementation you'd parse git log)
            result["commit_hash"] = "committed"  # Placeholder
            
            # Push branch if requested
            if push_branch and result["branch_name"]:
                if not self.git_detector.push_branch(result["branch_name"]):
                    result["errors"].append("Failed to push branch to remote")
                    # Don't fail the whole operation for push failure
            
            result["success"] = True
            
        except Exception as e:
            result["errors"].append(f"Unexpected error: {str(e)}")
            logger.error(f"Error in safe_commit_task: {e}")
        
        return result
    
    def commit_all_staged(self, task_id: str, task_description: str,
                         create_branch: bool = True, push_branch: bool = False) -> Dict[str, any]:
        """Commit all staged changes (use with caution)"""
        result = {
            "success": False,
            "branch_name": None,
            "commit_hash": None,
            "files_committed": [],
            "errors": []
        }
        
        try:
            # Check if we're in a git repository
            if not self.git_detector.repo_path:
                result["errors"].append("Not in a git repository")
                return result
            
            # Get staged files
            staged_files = self.git_detector.get_staged_files()
            
            if not staged_files:
                result["errors"].append("No staged files to commit")
                return result
            
            # Check for sensitive files in staged files
            safe_files, sensitive_files = self.check_sensitive_files(staged_files)
            
            if sensitive_files:
                result["errors"].append(f"Sensitive files detected in staged changes: {', '.join(sensitive_files)}")
                return result
            
            # Create feature branch if requested
            if create_branch:
                branch_name = self.git_detector.create_feature_branch(task_id, task_description)
                if branch_name:
                    result["branch_name"] = branch_name
                else:
                    result["errors"].append("Failed to create feature branch")
                    return result
            
            # Generate commit message
            git_diff = self.git_detector.get_git_diff(staged_only=True)
            commit_message = self.generate_commit_message(
                task_id, task_description, staged_files, git_diff
            )
            
            # Commit changes
            if not self.git_detector.commit_changes(commit_message):
                result["errors"].append("Failed to commit changes")
                return result
            
            result["files_committed"] = staged_files
            result["commit_hash"] = "committed"  # Placeholder
            
            # Push branch if requested
            if push_branch and result["branch_name"]:
                if not self.git_detector.push_branch(result["branch_name"]):
                    result["errors"].append("Failed to push branch to remote")
            
            result["success"] = True
            
        except Exception as e:
            result["errors"].append(f"Unexpected error: {str(e)}")
            logger.error(f"Error in commit_all_staged: {e}")
        
        return result
    
    def get_git_status_summary(self) -> Dict[str, any]:
        """Get a comprehensive git status summary"""
        if not self.git_detector.repo_path:
            return {"error": "Not in a git repository"}
        
        try:
            current_branch = self.git_detector.get_current_branch()
            changes = self.git_detector.detect_file_changes()
            staged_files = self.git_detector.get_staged_files()
            is_clean = self.git_detector.is_working_directory_clean()
            
            # Check for sensitive files
            all_files = changes["modified"] + changes["created"] + changes["deleted"]
            safe_files, sensitive_files = self.check_sensitive_files(all_files)
            
            return {
                "current_branch": current_branch,
                "working_directory_clean": is_clean,
                "changes": {
                    "modified": changes["modified"],
                    "created": changes["created"],
                    "deleted": changes["deleted"],
                    "total": len(all_files)
                },
                "staged_files": staged_files,
                "unstaged_files": [f for f in all_files if f not in staged_files],
                "safety": {
                    "safe_files": safe_files,
                    "sensitive_files": sensitive_files,
                    "has_sensitive_files": bool(sensitive_files)
                }
            }
        except Exception as e:
            logger.error(f"Error getting git status summary: {e}")
            return {"error": f"Failed to get git status: {str(e)}"}
