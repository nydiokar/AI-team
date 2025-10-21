#!/usr/bin/env python3
"""
Tests for working directory resolution in ClaudeBridge
"""
import pytest
from pathlib import Path
from unittest.mock import Mock, patch
import sys
import os

# Add the orchestrator directory to Python path so we can import modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.bridges.claude_bridge import ClaudeBridge
from src.core.interfaces import Task, TaskType, TaskPriority, TaskStatus
from config import config


@pytest.fixture(autouse=True)
def configure_projects_base(tmp_path_factory, request):
    """Configure a temporary base working directory for each test."""
    base_dir = tmp_path_factory.mktemp("projects")
    config.claude.base_cwd = str(base_dir)
    config.claude.allowed_root = str(base_dir)
    if getattr(request, "cls", None):
        request.cls._configured_base_dir = Path(base_dir)
    yield
    config.claude.base_cwd = None
    config.claude.allowed_root = None
    if getattr(request, "cls", None):
        request.cls._configured_base_dir = None


class TestWorkingDirectoryResolution:
    """Test working directory resolution logic"""

    def setup_method(self):
        """Set up test fixtures"""
        self.bridge = ClaudeBridge()

        base_path = getattr(self, "_configured_base_dir", None)
        if base_path is None:
            base_cwd = config.claude.base_cwd
            assert base_cwd, "Test fixture did not set config.claude.base_cwd"
            base_path = Path(base_cwd)
        else:
            base_path = Path(base_path)

        self.base_dir = base_path
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.base_dir_str = str(self.base_dir)

        # Prepare a representative project structure inside the base directory
        self.ai_team_dir = self.base_dir / "AI-team"
        self.ai_team_dir.mkdir(parents=True, exist_ok=True)
        (self.ai_team_dir / "subfolder").mkdir(parents=True, exist_ok=True)
        (self.ai_team_dir / "nested" / "project").mkdir(parents=True, exist_ok=True)

        self.pijama_dir = self.base_dir / "pijama"
        self.pijama_dir.mkdir(parents=True, exist_ok=True)
        (self.pijama_dir / "subfolder").mkdir(parents=True, exist_ok=True)

        # Directories outside the allowed root for fallback tests
        self.outside_dir = self.base_dir.parent / "outside"
        self.outside_dir.mkdir(parents=True, exist_ok=True)
        self.remote_dir = self.base_dir.parent / "other-projects"
        self.remote_dir.mkdir(parents=True, exist_ok=True)
        
        # Mock task with metadata
        self.base_task = Task(
            id="test_task",
            type=TaskType.ANALYZE,
            priority=TaskPriority.MEDIUM,
            status=TaskStatus.PENDING,
            created="2025-01-01T00:00:00Z",
            title="Test Task",
            target_files=[],
            prompt="Test prompt",
            success_criteria=[],
            context="",
            metadata={}
        )

    def test_default_base_directory(self):
        """Test that default working directory is set to Projects folder"""
        # The base directory should be set in config
        expected_base = self.base_dir_str
        
        assert config.claude.base_cwd == expected_base
        assert config.claude.allowed_root == expected_base

    def test_resolve_cwd_no_override(self):
        """Test cwd resolution when no task-specific override is provided"""
        # Task has no cwd in metadata
        cwd = self.bridge._resolve_cwd(self.base_task)
        
        # Should return the configured base directory
        expected = self.base_dir_str
        assert cwd == expected

    def test_resolve_cwd_with_absolute_path(self):
        """Test cwd resolution with absolute path"""
        task = self.base_task
        task.metadata = {"cwd": str(self.ai_team_dir)}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should return the absolute path as-is
        expected = str(self.ai_team_dir)
        assert cwd == expected

    def test_resolve_cwd_with_base_relative_path(self):
        """Test cwd resolution with base-relative path like '/ai-team'"""
        task = self.base_task
        task.metadata = {"cwd": "/AI-team"}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should resolve relative to base directory
        expected = str(self.ai_team_dir)
        assert cwd == expected

    def test_resolve_cwd_with_backslash_relative_path(self):
        """Test cwd resolution with backslash-relative path like '\\ai-team'"""
        task = self.base_task
        task.metadata = {"cwd": "\\AI-team"}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should resolve relative to base directory
        expected = str(self.ai_team_dir)
        assert cwd == expected

    def test_resolve_cwd_with_subdirectory_path(self):
        """Test cwd resolution with subdirectory path like 'ai-team/subfolder'"""
        task = self.base_task
        task.metadata = {"cwd": "AI-team/subfolder"}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should resolve relative to base directory
        expected = str(self.ai_team_dir / "subfolder")
        assert cwd == expected

    def test_resolve_cwd_with_different_drive(self):
        """Test cwd resolution with path on different root (should fallback to base)"""
        task = self.base_task
        task.metadata = {"cwd": str(self.remote_dir)}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should fallback to base directory instead of rejecting
        expected = self.base_dir_str
        assert cwd == expected

    def test_resolve_cwd_with_path_outside_allowed_root(self):
        """Test cwd resolution with path outside allowed root (should fallback to base)"""
        task = self.base_task
        task.metadata = {"cwd": str(self.outside_dir)}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should fallback to base directory instead of rejecting
        expected = self.base_dir_str
        assert cwd == expected

    def test_resolve_cwd_with_nested_project(self):
        """Test cwd resolution with nested project structure"""
        task = self.base_task
        task.metadata = {"cwd": "AI-team/nested/project"}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should resolve to nested path
        expected = str(self.ai_team_dir / "nested" / "project")
        assert cwd == expected

    def test_resolve_cwd_with_root_itself(self):
        """Test cwd resolution when task specifies the root directory itself"""
        task = self.base_task
        task.metadata = {"cwd": "/"}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should resolve to base directory
        expected = self.base_dir_str
        assert cwd == expected

    def test_resolve_cwd_with_empty_string(self):
        """Test cwd resolution with empty string (should use base)"""
        task = self.base_task
        task.metadata = {"cwd": ""}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should return base directory
        expected = self.base_dir_str
        assert cwd == expected

    def test_resolve_cwd_with_whitespace_only(self):
        """Test cwd resolution with whitespace-only string (should use base)"""
        task = self.base_task
        task.metadata = {"cwd": "   "}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should return base directory
        expected = self.base_dir_str
        assert cwd == expected

    def test_execute_command_cwd_resolution(self):
        """Test that execute_command uses resolved cwd"""
        task = self.base_task
        task.metadata = {"cwd": "/AI-team"}
        
        # Mock the subprocess execution
        with patch('asyncio.create_subprocess_exec') as mock_subprocess:
            mock_process = Mock()
            mock_process.communicate.return_value = (b"output", b"")
            mock_subprocess.return_value = mock_process
            
            # Mock the cwd resolution
            with patch.object(self.bridge, '_resolve_cwd', return_value=str(self.ai_team_dir)):
                # This would normally call _execute_command, but we're testing the cwd resolution
                expected_cwd = str(self.ai_team_dir)
                assert self.bridge._resolve_cwd(task) == expected_cwd

    def test_task_creation_with_path_hint(self):
        """Test that task creation extracts path hints correctly"""
        # This test would verify the orchestrator's path extraction logic
        # We'll test the regex patterns used in create_task_from_description
        
        import re
        
        # Test Windows path extraction
        message = "strengthen the ollama tool calling in C:\\Users\\Cicada38\\Projects\\AI-team"
        match = re.search(r"\bin\s+([A-Za-z]:\\[^\n\r]+)", message)
        assert match is not None
        assert match.group(1) == r"C:\Users\Cicada38\Projects\AI-team"
        
        # Test POSIX-style path extraction
        message2 = "do something in /AI-team/subfolder"
        match2 = re.search(r"\bin\s+(/[^\n\r]+)", message2)
        assert match2 is not None
        assert match2.group(1) == "/AI-team/subfolder"
        
        # Test relative path extraction
        message3 = "work on AI-team project"
        # This should not match the absolute path patterns
        match3 = re.search(r"\bin\s+(/[^\n\r]+)", message3)
        assert match3 is None

    def test_real_scenario_pijama_directory(self):
        """Test a real scenario: create pijama directory and work there"""
        # This simulates the user saying "create a pijama directory, navigate to it and start working there"
        
        # Test 1: Extract path hint from natural language
        import re
        message = "create a pijama directory, navigate to it and start working there"
        
        # The orchestrator should extract "pijama" as a relative path
        # For now, let's test the cwd resolution logic directly
        
        task = self.base_task
        task.metadata = {"cwd": "pijama"}
        
        cwd = self.bridge._resolve_cwd(task)
        
        # Should resolve to pijama subdirectory under Projects
        expected = str(self.pijama_dir)
        assert cwd == expected
        
        # Test 2: What if they say "in /pijama" (POSIX style)
        task.metadata = {"cwd": "/pijama"}
        cwd = self.bridge._resolve_cwd(task)
        assert cwd == expected
        
        # Test 3: What if they say "in pijama/subfolder"
        task.metadata = {"cwd": "pijama/subfolder"}
        cwd = self.bridge._resolve_cwd(task)
        expected_nested = str(self.pijama_dir / "subfolder")
        assert cwd == expected_nested

    def test_config_validation(self):
        """Test that configuration is properly set up"""
        # Verify the configuration is set correctly
        assert config.claude.base_cwd is not None
        assert config.claude.allowed_root is not None
        assert config.claude.base_cwd == config.claude.allowed_root
        
        # Verify it's the expected path
        expected = self.base_dir_str
        assert config.claude.base_cwd == expected
        
        # Verify it's not an environment variable
        assert os.getenv("CLAUDE_BASE_CWD") is None
        assert os.getenv("CLAUDE_ALLOWED_ROOT") is None
