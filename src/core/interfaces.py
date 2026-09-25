"""
Core interfaces for the Telegram Coding Gateway.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
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
    usage: Optional[Dict[str, Any]] = None
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
    # Affinity fallback (A18) — a session pinned to a remote mesh node whose
    # node is currently offline. Never runs the turn off-host (A11 invariant).
    #   PAUSED_PINNED_NODE_OFFLINE — transient: bounded hold, polling liveness,
    #       within AFFINITY_OFFLINE_GRACE_SEC. Resolves back to BUSY when the
    #       node re-registers, or to PINNED_NODE_OFFLINE when the grace expires.
    #   PINNED_NODE_OFFLINE — honest terminal-but-resumable: grace expired with
    #       the node still down. NOT a bare ERROR; the operator can retry once
    #       the node is back, or re-pin the session to another node.
    PAUSED_PINNED_NODE_OFFLINE = "paused_pinned_node_offline"
    PINNED_NODE_OFFLINE = "pinned_node_offline"

@dataclass(frozen=True)
class SessionOrigin:
    """Transport-neutral tag describing where a session came from.

    Adopted (concept only) from OpenClaw's sessionKey. Descriptive, not a
    routing policy — see docs/COCKPIT_REFACTOR_SPEC.md §B.0. Defaults reproduce
    today's behavior so existing sessions are unchanged.
    """
    channel: str = "telegram"   # "telegram" | "web" | "cli" | future surfaces
    kind: str = "user"          # "user" | "cron" | "subagent" (future workflow)


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
    model: Optional[str] = None         # picked model name; None = use backend default
    effort: Optional[str] = None        # thinking/reasoning effort; None = backend default
    last_task_id: str = ""
    last_artifact_path: str = ""
    last_summary: str = ""
    last_user_message: str = ""
    last_result_summary: str = ""
    last_files_modified: List[str] = None
    telegram_chat_id: Optional[int] = None
    telegram_thread_id: Optional[int] = None
    owner_user_id: Optional[int] = None
    task_history: List[Dict[str, Any]] = None  # [{task_id, timestamp, success, execution_time}]
    origin: Optional[SessionOrigin] = None      # where the session came from; defaults telegram/user

    # Driver state (P0 replacement engine)
    driver_type: str = ""               # "sdk" | "print_resume" | "" (unknown/legacy)
    driver_status: str = ""             # "live" | "lost" | "closed" | ""
    cache_health: str = "unknown"       # "unknown" | "healthy" | "unhealthy"
    cache_unhealthy_count: int = 0
    previous_backend_session_ids: List[str] = None  # history when rolling over

    # [A36] Durable Case affiliation. A session attached to (or opened for) a
    # managed Case carries its flow_run_id + role here so membership survives
    # across turns — set on attach/open, cleared on Case close (A37). None ⇒ the
    # session is standalone (Pattern A: many Tasks, no Case).
    current_case_id: Optional[str] = None
    case_role: Optional[str] = None     # "manager" | "worker" | "reviewer"

    # [Worker role] EXPLICIT, opt-in role-boot signal — distinct from `case_role`.
    # Set at session-create time by dispatch_worker(role='worker'); read by the
    # driver's _role_boot to load worker.md + worker tools. It is SEPARATE from
    # `case_role` on purpose: every Case-joined worker already carries
    # case_role='worker' and must stay tier-0 (role-less). Only this field opts a
    # worker into a role-ful boot. None ⇒ byte-identical legacy default.
    role_boot: Optional[str] = None     # "worker" | None

    # [Session-fork] Session→session lineage. When a session is forked (continue a
    # stalled thread as a fresh session), the new session records the id of the one
    # it continues here. Purely a SESSION-axis pointer for a navigable thread — it
    # is INDEPENDENT of Case membership (current_case_id/case_role) and behavioral
    # role (role_boot), so a fork never collides with Manager/Worker semantics.
    # INSERT-seeded at create time, read-only after. None ⇒ not a continuation.
    continued_from: Optional[str] = None

    # Operator keep marker. This is intentionally NOT mesh affinity pinning
    # (`machine_id`); it only records that the operator wants this session easy to
    # find later, with a note explaining why.
    keep_pinned: bool = False
    keep_note: str = ""

    def __post_init__(self):
        if self.last_files_modified is None:
            self.last_files_modified = []
        if self.task_history is None:
            self.task_history = []
        if self.origin is None:
            self.origin = SessionOrigin()
        if self.previous_backend_session_ids is None:
            self.previous_backend_session_ids = []


@dataclass
class ExecutionTelemetry:
    """Bounded telemetry summary returned alongside a backend result."""

    invocation_id: str
    events: List[Dict[str, Any]] = field(default_factory=list)
    coverage: Dict[str, str] = field(default_factory=dict)


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
    error_class: str = ""   # categorised failure reason, e.g. "permission_block"
    telemetry: Optional[ExecutionTelemetry] = None

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
    def create_session(
        self, session: "Session", *, telemetry_context: Any = None, telemetry_sink: Any = None
    ) -> ExecutionResult:
        """Start a new session — runs the first turn with no prior context."""
        pass

    @abstractmethod
    def resume_session(
        self, session: "Session", message: str, *, telemetry_context: Any = None,
        telemetry_sink: Any = None
    ) -> ExecutionResult:
        """Continue an existing session using the backend's native resume mechanism."""
        pass

    @abstractmethod
    def run_oneoff(
        self, cwd: str, message: str, *, telemetry_context: Any = None,
        telemetry_sink: Any = None
    ) -> ExecutionResult:
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

    def compact_session(self, session: "Session") -> ExecutionResult:
        """Compact the session's context window by sending /compact to the backend.

        Default implementation delegates to resume_session with the /compact
        slash command. Backends that do not support compaction should override
        this to raise NotImplementedError or return an error result.
        """
        return self.resume_session(session, "/compact")

    # ------------------------------------------------------------------ #
    # [A82 Stage 3] Managed (protocol-1) turn contract — the ONE seam a carrier
    # uses for a claimed managed row. Default: unsupported (fail closed). A
    # backend opts in by overriding all three; it is then advertised for queue
    # protocol 1. Legacy create/resume/run_oneoff are untouched.
    # ------------------------------------------------------------------ #
    def supports_managed_turns(self) -> bool:
        """True iff this backend implements :meth:`run_managed_turn`."""
        return False

    def run_managed_turn(
        self, session: "Session", message: str, ownership: Any, *,
        telemetry_context: Any = None, telemetry_sink: Any = None,
        on_process: Any = None,
    ) -> ExecutionResult:
        """Execute one managed turn for ``ownership`` (a
        ``turn_queue.ManagedTurnOwnership``). Must NEVER interrupt an in-flight
        turn on conflict (typed ``OwnershipConflictError`` instead) and must
        raise/report a typed ``RecoveryRequiredError`` when the outcome cannot
        be attributed (uncorrelated result / deadline). ``on_process`` (optional
        callable) receives the backend process identity ({pid, create_time})
        BEFORE the prompt is submitted, so a successor carrier can prove that
        process is gone. Default: unsupported."""
        from src.control.turn_queue import ManagedUnsupportedError

        raise ManagedUnsupportedError(
            "backend has no managed execution path", backend=type(self).__name__,
        )

    def is_quiescent(self, session: "Session") -> bool:
        """True iff no native work for ``session`` is in flight. Default False
        (unknown ⇒ not quiescent, fail closed)."""
        return False

    def forget_managed_turn(self, session: "Session", turn_uuid: str) -> bool:
        """The carrier learned the managed turn ``turn_uuid`` is terminal on the
        server (operator-resolved / definitively refused): drop any in-memory
        wait for it so the session can become quiescent again. Default: no-op."""
        return False

    # [A82 Stage 4b] Producer 2 — managed compaction + operator cancel.
    def run_managed_compaction(
        self, session: "Session", ownership: Any, *,
        telemetry_context: Any = None, telemetry_sink: Any = None,
        on_process: Any = None,
    ) -> ExecutionResult:
        """Compact ``session``'s native context as ONE managed turn owned by
        ``ownership``. Same contract as :meth:`run_managed_turn`: never
        interrupts native work (not quiescent ⇒ typed ``OwnershipConflictError``
        before anything is submitted), and an unattributable outcome is a typed
        ``RecoveryRequiredError``. Default: unsupported (fail closed)."""
        from src.control.turn_queue import ManagedUnsupportedError

        raise ManagedUnsupportedError(
            "backend has no managed compaction path", backend=type(self).__name__,
        )

    def cancel_managed_turn(self, session: "Session", turn_uuid: str) -> bool:
        """Operator cancel of the managed turn ``turn_uuid`` ONLY (never another
        turn): interrupt it if it is the turn the backend is running, or arm the
        interrupt for when it begins. True iff a cancel was delivered/armed.
        Default: False (nothing to cancel on this backend)."""
        return False


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
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Queue an instruction directly without writing a compatibility task file."""
        pass
    
    @abstractmethod
    def create_task_from_description(self, description: str, task_type: str = None, target_files: List[str] = None) -> str:
        """Compatibility helper that writes a .task.md file for external ingestion."""
        pass
