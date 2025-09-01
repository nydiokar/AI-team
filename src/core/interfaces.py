"""
Core interfaces for the AI Task Orchestrator
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from enum import Enum

class TaskType(Enum):
    CODE_REVIEW = "code_review"
    SUMMARIZE = "summarize"
    FIX = "fix"
    ANALYZE = "analyze"
    DOCUMENTATION = "documentation"
    BUG_FIX = "bug_fix"  # Alias for FIX to match agent names

class TaskPriority(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class TaskStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class Task:
    """Task data structure"""
    id: str
    type: TaskType
    priority: TaskPriority
    status: TaskStatus
    created: str
    title: str
    target_files: List[str]
    prompt: str
    success_criteria: List[str]
    context: str
    metadata: Dict[str, Any] = None

@dataclass
class TaskResult:
    """Task execution result"""
    task_id: str
    success: bool
    output: str
    errors: List[str]
    files_modified: List[str]
    execution_time: float
    timestamp: str
    # Raw process data for artifact persistence and diagnostics
    raw_stdout: str = ""
    raw_stderr: str = ""
    parsed_output: Any = None
    return_code: int = 0
    # Retry metadata (filled by orchestrator)
    retries: int = 0
    error_class: str = ""

@dataclass
class ValidationResult:
    """Validation result"""
    valid: bool
    similarity: float
    entropy: float
    issues: List[str]

class ITaskParser(ABC):
    """Interface for parsing task files"""
    
    @abstractmethod
    def parse_task_file(self, file_path: str) -> Task:
        """Parse a .task.md file into a Task object"""
        pass
    
    @abstractmethod
    def validate_task_format(self, file_path: str) -> List[str]:
        """Validate task file format and return errors"""
        pass

class IClaudeBridge(ABC):
    """Interface for Claude Code integration"""
    
    @abstractmethod
    async def execute_task(self, task: Task) -> TaskResult:
        """Execute a task using Claude Code"""
        pass
    
    @abstractmethod
    def test_connection(self) -> bool:
        """Test if Claude Code is available and working"""
        pass

class ILlamaMediator(ABC):
    """Interface for local LLAMA integration"""
    
    @abstractmethod
    def parse_task(self, task_content: str) -> Dict[str, Any]:
        """Parse task content using LLAMA"""
        pass
    
    @abstractmethod
    def create_claude_prompt(self, parsed_task: Dict[str, Any]) -> str:
        """Create Claude-optimized prompt"""
        pass
    
    @abstractmethod
    def summarize_result(self, result: TaskResult, original_task: Task) -> str:
        """Summarize task result for user notification"""
        pass

class IValidationEngine(ABC):
    """Interface for validation operations"""
    
    @abstractmethod
    def validate_llama_output(self, input_text: str, output: str, task_type: TaskType) -> ValidationResult:
        """Validate LLAMA's output for hallucinations"""
        pass
    
    @abstractmethod
    def validate_task_result(self, result: TaskResult, expected_files: List[str]) -> ValidationResult:
        """Validate task execution result"""
        pass

class ITelegramInterface(ABC):
    """Interface for Telegram bot operations"""
    
    @abstractmethod
    async def notify_completion(self, task_id: str, summary: str, success: bool):
        """Notify user of task completion"""
        pass
    
    @abstractmethod
    async def notify_error(self, error_message: str):
        """Notify user of system errors"""
        pass
    
    @abstractmethod
    async def handle_task_command(self, task_description: str) -> str:
        """Handle /task command and return task ID"""
        pass

class IFileWatcher(ABC):
    """Interface for file system monitoring"""
    
    @abstractmethod
    def start(self, callback):
        """Start watching for new task files"""
        pass
    
    @abstractmethod
    def stop(self):
        """Stop file watching"""
        pass

class ITaskOrchestrator(ABC):
    """Main orchestrator interface"""
    
    @abstractmethod
    async def start(self):
        """Start all system components"""
        pass
    
    @abstractmethod
    async def stop(self):
        """Stop all system components"""
        pass
    
    @abstractmethod
    async def process_task(self, task: Task) -> TaskResult:
        """Process a single task through the complete pipeline"""
        pass
    
    @abstractmethod
    def create_task_from_description(self, description: str, task_type: str = None, target_files: List[str] = None) -> str:
        """Create a task file from natural language description"""
        pass

class IAgent(ABC):
    """Interface for modular task agents"""
    
    @abstractmethod
    def get_agent_name(self) -> str:
        """Return the agent's name"""
        pass
    
    @abstractmethod
    def get_agent_instructions(self) -> str:
        """Return the agent's specific instructions"""
        pass
    
    @abstractmethod
    def get_allowed_tools(self) -> List[str]:
        """Return tools this agent is allowed to use"""
        pass
    
    @abstractmethod
    def should_modify_files(self) -> bool:
        """Whether this agent should modify files"""
        pass
    
    @abstractmethod
    def get_validation_thresholds(self) -> Dict[str, float]:
        """Get validation thresholds specific to this agent"""
        pass