from .interfaces import (
    Task, TaskResult, TaskType, TaskPriority, TaskStatus,
    Session, SessionStatus,
    ExecutionResult, CodingBackend,
    ValidationResult,
    ITaskParser, ILlamaMediator, IValidationEngine,
    ITelegramInterface, IFileWatcher, ITaskOrchestrator
)
from .task_parser import TaskParser
from .path_resolver import PathResolver, PathResolution
from .session_store import SessionStore
from .result_text import (
    extract_text_from_payload, session_reply_text, short_failure_reason,
    format_file_change_lines,
)
from .notification_service import NotificationService

try:
    from .file_watcher import FileWatcher, AsyncFileWatcher
except ModuleNotFoundError as exc:
    if exc.name != "watchdog":
        raise

    class _MissingWatchdog:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("File watching requires the optional 'watchdog' package")

    FileWatcher = _MissingWatchdog
    AsyncFileWatcher = _MissingWatchdog

__all__ = [
    "Task", "TaskResult", "TaskType", "TaskPriority", "TaskStatus",
    "Session", "SessionStatus",
    "ExecutionResult", "CodingBackend",
    "ValidationResult",
    "ITaskParser", "ILlamaMediator", "IValidationEngine",
    "ITelegramInterface", "IFileWatcher", "ITaskOrchestrator",
    "TaskParser", "FileWatcher", "AsyncFileWatcher",
    "PathResolver", "PathResolution",
    "SessionStore",
    "NotificationService",
]
