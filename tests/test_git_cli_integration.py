"""
Integration tests for git CLI commands
"""
import pytest
import tempfile
import shutil
import subprocess
import sys
import os
from pathlib import Path
from unittest.mock import patch, Mock


class TestGitCLIIntegration:
    """Integration tests for git CLI commands"""
    
    @pytest.fixture
    def temp_repo(self):
        """Create a temporary git repository for testing"""
        temp_dir = tempfile.mkdtemp()
        repo_path = Path(temp_dir)
        
        # Initialize git repository
        subprocess.run(['git', 'init'], cwd=repo_path, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=repo_path, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo_path, check=True)
        
        yield repo_path
        
        # Cleanup - handle Windows file locking issues
        try:
            # Force close any git processes that might be holding file handles
            subprocess.run(['git', 'gc'], cwd=repo_path, capture_output=True, timeout=5)
        except:
            pass
        
        try:
            shutil.rmtree(temp_dir)
        except PermissionError:
            # On Windows, sometimes files are still locked
            import time
            time.sleep(0.1)
            try:
                shutil.rmtree(temp_dir)
            except:
                # If still failing, just log it and continue
                print(f"Warning: Could not clean up temp directory {temp_dir}")
    
    @pytest.fixture
    def temp_project(self, temp_repo):
        """Create a temporary project structure for testing"""
        # Create some test files
        (temp_repo / "src").mkdir()
        (temp_repo / "tests").mkdir()
        
        # Create test files
        (temp_repo / "src" / "main.py").write_text("print('Hello, World!')")
        (temp_repo / "tests" / "test_main.py").write_text("def test_main(): pass")
        (temp_repo / "README.md").write_text("# Test Project")
        
        # Create a .env file (should be filtered out)
        (temp_repo / ".env").write_text("SECRET_KEY=test123")
        
        yield temp_repo
    
    def test_git_status_command(self, temp_project, capsys):
        """Test git-status CLI command"""
        # Change to the temp project directory
        original_cwd = Path.cwd()
        try:
            os.chdir(temp_project)
            
            # Run the git-status command
            from main import _handle_git_status
            _handle_git_status()
            
            # Capture output
            captured = capsys.readouterr()
            output = captured.out
            
            # Verify output contains expected information
            assert "📊 Git Repository Status" in output
            assert "🌿 Branch:" in output
            assert "🧹 Working directory:" in output
            assert "📝 Changes:" in output
            assert "🚫 Sensitive files detected:" in output
            assert ".env" in output  # Should detect sensitive file
            
        finally:
            os.chdir(original_cwd)
    
    def test_git_commit_command_success(self, temp_project, capsys):
        """Test git-commit CLI command with successful commit"""
        # Change to the temp project directory
        original_cwd = Path.cwd()
        try:
            os.chdir(temp_project)
            
            # Stage some files
            subprocess.run(['git', 'add', 'src/main.py'], cwd=temp_project, check=True)
            subprocess.run(['git', 'add', 'README.md'], cwd=temp_project, check=True)
            
            # Run the git-commit command
            from main import _handle_git_commit
            _handle_git_commit(['test_123'])
            
            # Capture output
            captured = capsys.readouterr()
            output = captured.out
            
            # Verify output contains success information
            assert "✅ Successfully committed task test_123" in output
            assert "📁 Branch:" in output
            assert "📄 Files committed:" in output
            
            # Verify the commit was actually made
            result = subprocess.run(
                ['git', 'log', '--oneline', '-1'],
                cwd=temp_project,
                capture_output=True,
                text=True,
                check=True
            )
            assert "test_123" in result.stdout
            
        finally:
            os.chdir(original_cwd)
    
    def test_git_commit_command_no_args(self, capsys):
        """Test git-commit CLI command with no arguments"""
        from main import _handle_git_commit
        _handle_git_commit([])
        
        captured = capsys.readouterr()
        output = captured.out
        
        assert "Usage:" in output
        assert "git-commit <task_id>" in output
    
    def test_git_commit_command_with_flags(self, temp_project, capsys):
        """Test git-commit CLI command with various flags"""
        # Change to the temp project directory
        original_cwd = Path.cwd()
        try:
            os.chdir(temp_project)
            
            # Stage some files
            subprocess.run(['git', 'add', 'src/main.py'], cwd=temp_project, check=True)
            
            # Test with --no-branch flag
            from main import _handle_git_commit
            _handle_git_commit(['test_456', '--no-branch'])
            
            captured = capsys.readouterr()
            output = captured.out
            
            # Should still succeed but not create a new branch
            assert "✅ Successfully committed task test_456" in output
            
            # Verify we're still on the original branch
            result = subprocess.run(
                ['git', 'branch', '--show-current'],
                cwd=temp_project,
                capture_output=True,
                text=True,
                check=True
            )
            # Git might create 'master' or 'main' as default branch
            current_branch = result.stdout.strip()
            assert current_branch in ["main", "master"]  # Should still be on default branch
            
        finally:
            os.chdir(original_cwd)
    
    def test_git_commit_all_command(self, temp_project, capsys):
        """Test git-commit-all CLI command"""
        # Change to the temp project directory
        original_cwd = Path.cwd()
        try:
            os.chdir(temp_project)
            
            # Stage all files
            subprocess.run(['git', 'add', '.'], cwd=temp_project, check=True)
            
            # Run the git-commit-all command
            from main import _handle_git_commit_all
            _handle_git_commit_all(['test_789'])
            
            # Capture output
            captured = capsys.readouterr()
            output = captured.out
            
            # Verify output contains success information
            assert "✅ Successfully committed all staged changes for task test_789" in output
            assert "📄 Files committed:" in output
            
            # Verify the commit was actually made
            result = subprocess.run(
                ['git', 'log', '--oneline', '-1'],
                cwd=temp_project,
                capture_output=True,
                text=True,
                check=True
            )
            assert "test_789" in result.stdout
            
        finally:
            os.chdir(original_cwd)
    
    def test_git_commit_all_command_no_staged_files(self, temp_project, capsys):
        """Test git-commit-all CLI command with no staged files"""
        # Change to the temp project directory
        original_cwd = Path.cwd()
        try:
            os.chdir(temp_project)
            
            # Run the git-commit-all command without staging anything
            from main import _handle_git_commit_all
            _handle_git_commit_all(['test_999'])
            
            # Capture output
            captured = capsys.readouterr()
            output = captured.out
            
            # Should fail with appropriate error
            assert "❌ Failed to commit staged changes for task test_999" in output
            assert "No staged files to commit" in output
            
        finally:
            os.chdir(original_cwd)
    
    def test_git_commit_command_sensitive_files_filtered(self, temp_project, capsys):
        """Test that sensitive files are filtered out during commit"""
        # Change to the temp project directory
        original_cwd = Path.cwd()
        try:
            os.chdir(temp_project)
            
            # Stage both safe and sensitive files
            subprocess.run(['git', 'add', 'src/main.py'], cwd=temp_project, check=True)
            subprocess.run(['git', 'add', '.env'], cwd=temp_project, check=True)
            
            # Run the git-commit command
            from main import _handle_git_commit
            _handle_git_commit(['test_sensitive'])
            
            # Capture output
            captured = capsys.readouterr()
            output = captured.out
            
            # Should succeed but mention sensitive files were blocked
            assert "✅ Successfully committed task test_sensitive" in output
            assert "🚫 Sensitive files blocked:" in output
            assert ".env" in output  # Should mention the blocked file
            
            # Verify only safe files were committed
            result = subprocess.run(
                ['git', 'log', '--oneline', '-1', '--name-only'],
                cwd=temp_project,
                capture_output=True,
                text=True,
                check=True
            )
            assert "src/main.py" in result.stdout
            assert ".env" not in result.stdout  # Should not be in commit
            
        finally:
            os.chdir(original_cwd)
    
    def test_git_commands_outside_repository(self, tmp_path, capsys):
        """Test git commands outside of a git repository"""
        # Change to a non-git directory
        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            
            # Test git-status
            from main import _handle_git_status
            _handle_git_status()
            
            captured = capsys.readouterr()
            output = captured.out
            
            assert "❌ Not in a git repository" in output
            
        finally:
            os.chdir(original_cwd)
    
    @patch('src.core.git_automation.GitAutomationService')
    def test_git_commands_service_unavailable(self, mock_service, capsys):
        """Test git commands when the service is unavailable"""
        # Mock the service to raise ImportError
        mock_service.side_effect = ImportError("Git automation service not available")
        
        # Test git-status
        from main import _handle_git_status
        _handle_git_status()
        
        captured = capsys.readouterr()
        output = captured.out
        
        assert "❌ Git automation service not available" in output


if __name__ == "__main__":
    pytest.main([__file__])
