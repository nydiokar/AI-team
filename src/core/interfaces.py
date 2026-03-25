"""
Core interfaces for the Telegram Coding Gateway.
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
    file_changes: List[Dict[str, Any]] = None
    # Raw process data for artifact persistence and diagnostics
    raw_stdout: str = ""
    raw_stderr: str = ""
    parsed_output: Any = None
    return_code: int = 0
    # Retry metadata (filled by orchestrator)
    retries: int = 0
    error_class: str = ""

    def __post_init__(self):
        if self.file_changes is None:
            self.file_changes = []

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

class SessionStatus(Enum):
    IDLE = "idle"
    BUSY = "busy"
    AWAITING_INPUT = "awaiting_input"
    ERROR = "error"
    CANCELLED = "cancelled"
    CLOSED = "closed"

@dataclass
class Session:
    """Gateway session — maps a Telegram conversation to a backend coding agent session."""
    session_id: str
    backend: str                        # "claude" | "codex"
    repo_path: str                      # working directory / repo root
    status: SessionStatus
    created_at: str
    updated_at: str
    machine_id: str = ""
    backend_session_id: str = ""        # native session ID returned by the backend
    last_task_id: str = ""
    last_artifact_path: str = ""
    last_summary: str = ""
    last_user_message: str = ""
    last_result_summary: str = ""
    last_files_modified: List[str] = None
    telegram_chat_id: Optional[int] = None
    telegram_thread_id: Optional[int] = None
    owner_user_id: Optional[int] = None

    def __post_init__(self):
        if self.last_files_modified is None:
            self.last_files_modified = []


@dataclass
class ExecutionResult:
    """Result returned by a CodingBackend after one turn."""
    success: bool
    output: str
    backend_session_id: str = ""   # native session ID to store for next resume
    files_modified: List[str] = None
    errors: List[str] = None
    execution_time: float = 0.0
    raw_stdout: str = ""
    raw_stderr: str = ""
    parsed_output: Any = None
    return_code: int = 0
    file_changes: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.files_modified is None:
            self.files_modified = []
        if self.file_changes is None:
            self.file_changes = []
        if self.errors is None:
            self.errors = []


class CodingBackend(ABC):
    """Protocol for coding agent backends (Claude Code, Codex, …)."""

    @abstractmethod
    def create_session(self, session: "Session") -> ExecutionResult:
        """Start a new session — runs the first turn with no prior context."""
        pass

    @abstractmethod
    def resume_session(self, session: "Session", message: str) -> ExecutionResult:
        """Continue an existing session using the backend's native resume mechanism."""
        pass

    @abstractmethod
    def run_oneoff(self, cwd: str, message: str) -> ExecutionResult:
        """Run a single stateless turn with no session tracking."""
        pass

    @abstractmethod
    def cancel(self, session: "Session") -> None:
        """Best-effort cancellation of a running backend session."""
        pass

    @abstractmethod
    def close(self, session: "Session") -> None:
        """Mark the session closed on the backend side (cleanup if needed)."""
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
    async def submit_instruction(
        self,
        description: str,
        task_type: Optional[str] = None,
        target_files: List[str] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        source: str = "runtime",
    ) -> str:
        """Queue an instruction directly without writing a compatibility task file."""
        pass
    
    @abstractmethod
    def create_task_from_description(self, description: str, task_type: str = None, target_files: List[str] = None) -> str:
        """Compatibility helper that writes a .task.md file for external ingestion."""
        pass
