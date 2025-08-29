"""
Unit tests for GitAutomationService
"""
import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from src.core.git_automation import GitAutomationService


class TestGitAutomationService:
    """Test GitAutomationService functionality"""
    
    @pytest.fixture
    def temp_repo(self):
        """Create a temporary git repository for testing"""
        temp_dir = tempfile.mkdtemp()
        repo_path = Path(temp_dir)
        
        # Initialize git repository
        import subprocess
        subprocess.run(['git', 'init'], cwd=repo_path, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=repo_path, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo_path, check=True)
        
        yield repo_path
        
        # Cleanup
        shutil.rmtree(temp_dir)
    
    @pytest.fixture
    def git_service(self, temp_repo):
        """Create GitAutomationService with temp repository"""
        return GitAutomationService(str(temp_repo))
    
    @pytest.fixture
    def mock_llama_mediator(self):
        """Mock LLAMA mediator for testing"""
        with patch('src.core.git_automation.LlamaMediator') as mock:
            mock_instance = Mock()
            mock_instance.ollama_available = True
            mock_instance.model_installed = True
            mock_instance.client = Mock()
            mock_instance.client.generate.return_value = {'response': 'feat: test commit message'}
            mock.return_value = mock_instance
            yield mock_instance
    
    def test_init_with_repo_path(self, temp_repo):
        """Test initialization with repository path"""
        service = GitAutomationService(str(temp_repo))
        assert service.git_detector.repo_path == temp_repo
        assert service.git_detector.repo_path is not None
    
    def test_init_without_repo_path(self):
        """Test initialization without repository path"""
        service = GitAutomationService()
        # Should use current working directory
        assert service.git_detector.repo_path is not None
    
    def test_init_not_git_repo(self, tmp_path):
        """Test initialization with non-git directory"""
        service = GitAutomationService(str(tmp_path))
        assert service.git_detector.repo_path is None
    
    def test_generate_commit_message_with_llama(self, git_service, mock_llama_mediator):
        """Test commit message generation with LLAMA available"""
        task_id = "test_123"
        task_description = "Test task description"
        files_changed = ["file1.py", "file2.py"]
        
        message = git_service.generate_commit_message(
            task_id, task_description, files_changed
        )
        
        assert message == "feat: test commit message"
        mock_llama_mediator.client.generate.assert_called_once()
    
    def test_generate_commit_message_fallback(self, git_service):
        """Test commit message generation fallback when LLAMA unavailable"""
        # Mock LLAMA to be unavailable
        git_service.llama_mediator.ollama_available = False
        
        task_id = "test_123"
        task_description = "Fix the bug in authentication"
        files_changed = ["auth.py", "test_auth.py"]
        
        message = git_service.generate_commit_message(
            task_id, task_description, files_changed
        )
        
        assert message.startswith("fix:")
        assert "bug in authentication" in message.lower()
    
    def test_generate_commit_message_fallback_different_types(self, git_service):
        """Test fallback commit message generation for different task types"""
        git_service.llama_mediator.ollama_available = False
        
        # Test bug fix
        message = git_service.generate_commit_message(
            "test_123", "Fix the database connection issue", ["db.py"]
        )
        assert message.startswith("fix:")
        
        # Test documentation
        message = git_service.generate_commit_message(
            "test_456", "Update README documentation", ["README.md"]
        )
        assert message.startswith("docs:")
        
        # Test refactor
        message = git_service.generate_commit_message(
            "test_789", "Refactor the user authentication code", ["auth.py"]
        )
        assert message.startswith("refactor:")
    
    def test_check_sensitive_files(self, git_service):
        """Test sensitive file detection"""
        files = [
            "src/main.py",
            ".env",
            "config.py",
            "secrets.json",
            "id_rsa",
            "database.db",
            "logs/app.log"
        ]
        
        safe_files, sensitive_files = git_service.check_sensitive_files(files)
        
        assert "src/main.py" in safe_files
        assert "config.py" in safe_files
        assert ".env" in sensitive_files
        assert "secrets.json" in sensitive_files
        assert "id_rsa" in sensitive_files
        assert "database.db" in sensitive_files
        assert "logs/app.log" in sensitive_files
    
    def test_safe_commit_task_success(self, git_service, temp_repo):
        """Test successful safe commit task"""
        # Create some test files
        test_file = temp_repo / "test_file.py"
        test_file.write_text("print('Hello, World!')")
        
        # Stage the file
        import subprocess
        subprocess.run(['git', 'add', 'test_file.py'], cwd=temp_repo, check=True)
        
        result = git_service.safe_commit_task(
            task_id="test_123",
            task_description="Add test file",
            create_branch=True,
            push_branch=False
        )
        
        assert result["success"] is True
        assert result["branch_name"] is not None
        assert "test_file.py" in result["files_committed"]
        assert result["sensitive_files_blocked"] == []
    
    def test_safe_commit_task_no_changes(self, git_service):
        """Test safe commit task with no changes"""
        result = git_service.safe_commit_task(
            task_id="test_123",
            task_description="No changes",
            create_branch=True,
            push_branch=False
        )
        
        assert result["success"] is False
        assert "No changes detected to commit" in result["errors"]
    
    def test_safe_commit_task_not_git_repo(self):
        """Test safe commit task outside git repository"""
        service = GitAutomationService("/tmp/non_existent")
        result = service.safe_commit_task(
            task_id="test_123",
            task_description="Test",
            create_branch=True,
            push_branch=False
        )
        
        assert result["success"] is False
        assert "Not in a git repository" in result["errors"]
    
    def test_commit_all_staged_success(self, git_service, temp_repo):
        """Test successful commit all staged"""
        # Create and stage test files
        test_file = temp_repo / "test_file.py"
        test_file.write_text("print('Hello, World!')")
        
        import subprocess
        subprocess.run(['git', 'add', 'test_file.py'], cwd=temp_repo, check=True)
        
        result = git_service.commit_all_staged(
            task_id="test_123",
            task_description="Add test file",
            create_branch=True,
            push_branch=False
        )
        
        assert result["success"] is True
        assert result["branch_name"] is not None
        assert "test_file.py" in result["files_committed"]
    
    def test_commit_all_staged_no_staged_files(self, git_service):
        """Test commit all staged with no staged files"""
        result = git_service.commit_all_staged(
            task_id="test_123",
            task_description="No staged files",
            create_branch=True,
            push_branch=False
        )
        
        assert result["success"] is False
        assert "No staged files to commit" in result["errors"]
    
    def test_get_git_status_summary(self, git_service, temp_repo):
        """Test git status summary"""
        status = git_service.get_git_status_summary()
        
        assert "error" not in status
        assert status["current_branch"] is not None
        assert "working_directory_clean" in status
        assert "changes" in status
        assert "staged_files" in status
        assert "unstaged_files" in status
        assert "safety" in status
    
    def test_get_git_status_summary_not_git_repo(self):
        """Test git status summary outside git repository"""
        service = GitAutomationService("/tmp/non_existent")
        status = service.get_git_status_summary()
        
        assert "error" in status
        assert "Not in a git repository" in status["error"]
    
    def test_create_feature_branch(self, git_service, temp_repo):
        """Test feature branch creation"""
        branch_name = git_service.git_detector.create_feature_branch(
            task_id="test_123",
            description="Add new authentication system"
        )
        
        assert branch_name is not None
        assert branch_name.startswith("feature/task-test_123-")
        assert "authentication" in branch_name.lower()
        
        # Verify branch was created
        import subprocess
        result = subprocess.run(
            ['git', 'branch', '--list', branch_name],
            cwd=temp_repo,
            capture_output=True,
            text=True,
            check=True
        )
        assert branch_name in result.stdout
    
    def test_create_feature_branch_clean_description(self, git_service, temp_repo):
        """Test feature branch creation with special characters in description"""
        branch_name = git_service.git_detector.create_feature_branch(
            task_id="test_456",
            description="Fix bug #123: Authentication fails with @#$%^&*()"
        )
        
        assert branch_name is not None
        assert "bug-123" in branch_name
        assert "authentication-fails" in branch_name
        # Should not contain special characters
        assert "@" not in branch_name
        assert "#" not in branch_name
        assert "$" not in branch_name
    
    def test_branch_name_length_limit(self, git_service, temp_repo):
        """Test that branch names are limited in length"""
        long_description = "A" * 100  # Very long description
        
        branch_name = git_service.git_detector.create_feature_branch(
            task_id="test_789",
            description=long_description
        )
        
        assert branch_name is not None
        assert len(branch_name) <= 100  # Reasonable limit for git branch names
    
    @patch('subprocess.run')
    def test_git_operations_error_handling(self, mock_run, git_service):
        """Test error handling in git operations"""
        # Mock git command to fail
        mock_run.side_effect = Exception("Git command failed")
        
        result = git_service.safe_commit_task(
            task_id="test_123",
            task_description="Test",
            create_branch=True,
            push_branch=False
        )
        
        assert result["success"] is False
        assert len(result["errors"]) > 0
        assert "Unexpected error" in result["errors"][0]


if __name__ == "__main__":
    pytest.main([__file__])
