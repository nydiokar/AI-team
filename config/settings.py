"""
Configuration settings for the AI Task Orchestrator
"""
import os
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

# Environment variables owned by this process config. When AI_TEAM_ENV_FILE is
# explicit, that file is authoritative for these keys: commented/deleted keys
# clear stale supervisor environment values instead of silently reusing them.
_MANAGED_ENV_KEYS = {
    "CLAUDE_ALLOWED_ROOT",
    "CLAUDE_BASE_CWD",
    "CLAUDE_DEFAULT_MODEL",
    "CLAUDE_MAX_TURNS",
    "CLAUDE_SKIP_PERMISSIONS",
    "CLAUDE_TIMEOUT_SEC",
    "CODEX_DEFAULT_MODEL",
    "CONTROL_API_ENABLED",
    "CONTROL_API_HOST",
    "DASHBOARD_PORT",
    "DASHBOARD_TOKEN",
    "GATEWAY_HEARTBEAT_INTERVAL_SEC",
    "GATEWAY_INACTIVITY_TIMEOUT_SEC",
    "GATEWAY_SDK_TURN_TIMEOUT_SEC",
    "GATEWAY_TASK_TIMEOUT_SEC",
    "GATEWAY_TELEGRAM_ALLOWED_USERS",
    "GATEWAY_TELEGRAM_BOT_TOKEN",
    "GATEWAY_TELEGRAM_CHAT_ID",
    "GATEWAY_UPLOAD_MAX_MB",
    "GUARDED_WRITE",
    "MAX_CONCURRENT_TASKS",
    "MAX_QUEUE_SIZE",
    "MESH_CLAIM_LEASE_SEC",
    "MESH_CLAIM_MAX_RUNTIME_SEC",
    "MESH_DB_PATH",
    "MESH_EMBEDDED_SERVER",
    "MESH_ENABLED",
    "MESH_HEALTH_FAILURE_THRESHOLD",
    "MESH_HEALTH_WINDOW_SIZE",
    "MESH_ONEOFF_QUEUE_TIMEOUT_SEC",
    "MESH_ROUTING_FRESHNESS_WAIT_SEC",
    "MESH_ROUTING_LIVE_STATE_MAX_AGE_SEC",
    "MESH_SESSION_RECONCILE_INTERVAL_SEC",
    "MESH_SHADOW_WRITE",
    "MESH_TAILSCALE_IP",
    "MESH_TASK_SERVER_PORT",
    "OPENCODE_DEFAULT_AGENT",
    "OPENCODE_DEFAULT_MODEL",
    "OPENCODE_MODE",
    "OPENCODE_SERVER_ENABLED",
    "OPENCODE_TIMEOUT_SEC",
    "TELEGRAM_MESSAGE_BUFFER_SEC",
    "TELEGRAM_RATE_LIMIT_REQUESTS",
    "TELEGRAM_RATE_LIMIT_WINDOW_SEC",
    "TELEMETRY_DETAILED_EVENTS",
    "TELEMETRY_ENABLED",
    "TELEMETRY_EVENT_RETENTION_DAYS",
    "TELEMETRY_OTLP_ENDPOINT",
    "TELEMETRY_SPOOL_MAX_BYTES",
    "TELEMETRY_SUMMARY_RETENTION_DAYS",
    "TELEMETRY_TASK_SERVER_URL",
    "TELEMETRY_UPLOAD_BATCH_SIZE",
    "TELEMETRY_UPLOAD_INTERVAL_MS",
    "TELEMETRY_UPLOAD_MAX_BYTES",
    "WORKER_TOKEN",
}
# Load environment variables from .env file
try:
    from dotenv import dotenv_values, load_dotenv
    # Prefer explicit PM2 env file, then this project root, then CWD.
    configured_env = os.getenv("AI_TEAM_ENV_FILE")
    env_candidates = (
        [Path(configured_env).expanduser()]
        if configured_env
        else [Path(__file__).parent.parent / ".env", Path.cwd() / ".env"]
    )
    for env_path in env_candidates:
        if env_path.exists():
            if configured_env:
                configured_values = dotenv_values(env_path)
                for env_key in _MANAGED_ENV_KEYS - set(configured_values):
                    os.environ.pop(env_key, None)
            load_dotenv(env_path, override=bool(configured_env))
            print(f"Loaded environment from: {env_path}")
            break
    else:
        print("Warning: .env file not found in project or current directory")
except ImportError:
    print("Warning: python-dotenv not available, using system environment only")

@dataclass
class ClaudeConfig:
    """Claude Code CLI configuration"""
    base_command: List[str]
    output_format: str = "json"
    headless_mode: bool = True
    skip_permissions: bool = False
    timeout: int = 300  # 5 minutes
    max_turns: int = 0
    # Working directory controls
    base_cwd: Optional[str] = None
    allowed_root: Optional[str] = None
    # Default model (alias like "sonnet"/"opus", or full name). None = CLI default.
    default_model: Optional[str] = None

@dataclass
class CodexConfig:
    """Codex CLI configuration"""
    # Default model name passed via `-m` (e.g. "gpt-5.5"). None = CLI/config.toml default.
    default_model: Optional[str] = None

@dataclass
class LlamaConfig:
    """Local LLAMA configuration"""
    model: str = "llama3.2:latest"
    host: str = "localhost"
    port: int = 11434
    timeout: int = 120
    context_window: int = 128000  # 128k context for llama3.2
    # Soft caps to keep prompts within reliable size in characters
    max_parse_chars: int = 200_000
    max_prompt_chars: int = 32_000
    max_summary_input_chars: int = 40_000
    
@dataclass
class TelegramConfig:
    """Telegram bot configuration"""
    bot_token: str = ""
    allowed_users: List[int] = None
    notification_chat_id: Optional[int] = None
    upload_max_mb: int = 0  # 0 = no cap; Telegram's own limits apply
    
    def __post_init__(self):
        if self.allowed_users is None:
            self.allowed_users = []
            
@dataclass
class OpenCodeConfig:
    """OpenCode backend configuration.

    mode="cli"    → OpenCodeBackend    (subprocess per turn, env: OPENCODE_MODE=cli)
    mode="server" → OpenCodeServerBackend (persistent HTTP server, env: OPENCODE_MODE=server)

    The active backend is selected by the gateway session's `backend` field:
      "opencode"        → CLI mode
      "opencode-server" → server mode
    OPENCODE_MODE is reserved for future auto-wiring.
    """
    default_model: Optional[str] = "opencode/big-pickle"
    default_agent: Optional[str] = None
    timeout_seconds: int = 1800           # 30 min wall-clock cap (inactivity timeout is primary)
    collect_diff: bool = True
    run_tests_after: bool = False
    test_command: Optional[str] = None
    mode: str = "cli"                     # "cli" | "server" — informational; see docstring
    server_host: str = "127.0.0.1"        # bind address for opencode serve
    server_port: int = 4096               # preferred port; falls back to any free port
    server_enabled: bool = False          # legacy flag — kept for env-compat, not used in logic

@dataclass
class ValidationConfig:
    """Validation engine configuration"""
    similarity_threshold: float = 0.7
    entropy_threshold: float = 0.8
    max_retries: int = 3
    backoff_multiplier: int = 2
    
@dataclass
class SystemConfig:
    """System-wide configuration"""
    tasks_dir: str = "tasks"
    results_dir: str = "results"
    summaries_dir: str = "summaries"
    logs_dir: str = "logs"
    log_level: str = "INFO"
    max_concurrent_tasks: int = 3
    task_timeout: int = 0  # wall-clock kill (0 = disabled; backend inactivity timeout is the primary mechanism)
    inactivity_timeout_sec: int = 600  # PrintResume driver: kill process after N seconds of no stdout (10 min default)
    sdk_turn_timeout_sec: int = 7200   # SDK driver: total-turn deadline in seconds (2 hours; 0 = no limit)
    task_heartbeat_interval_sec: int = 300  # send "still working" every 5 min for long tasks
    guarded_write: bool = False
    # When True, _write_artifacts moves the heavy raw_stdout NDJSON (87% of
    # artifact bytes) to a gzipped sidecar (results/raw/<id>.ndjson.gz) and drops
    # it from the JSON. Safe to enable once the DB backfill parity check passes;
    # the conversation + structured fields then live in mesh_tasks, not the files.
    slim_artifacts: bool = False
    # Rate limiting and backpressure settings
    max_queue_size: int = 50
    telegram_rate_limit_requests: int = 5
    telegram_rate_limit_window_sec: int = 60
    telegram_message_buffer_sec: float = 3.0
    
@dataclass
class MeshConfig:
    """Agent mesh network configuration.

    Phase 2: shadow-write DB is built and sessions/tasks are mirrored into SQLite.
    Phase 3: MESH_ENABLED=true activates worker dispatch routing in the orchestrator.

    DB_PATH is relative to the project root unless absolute.
    On the VPS this will point to a persistent volume path.
    """
    enabled: bool = False                   # MESH_ENABLED — activates worker dispatch
    db_path: str = "state/mesh.db"          # MESH_DB_PATH — SQLite file location
    tailscale_ip: str = ""                  # MESH_TAILSCALE_IP — this node's TS IP
    task_server_port: int = 9002            # MESH_TASK_SERVER_PORT
    worker_token: str = ""                  # WORKER_TOKEN — shared mesh auth secret
    node_heartbeat_timeout_sec: int = 90    # MESH_HEARTBEAT_TIMEOUT_SEC
    oneoff_queue_timeout_sec: int = 600     # MESH_ONEOFF_QUEUE_TIMEOUT_SEC
    claim_lease_sec: int = 300              # MESH_CLAIM_LEASE_SEC — stale-claim reaper threshold (T4)
    claim_max_runtime_sec: int = 1800       # MESH_CLAIM_MAX_RUNTIME_SEC — hard cap for active claimed tasks
    session_reconcile_interval_sec: int = 60  # MESH_SESSION_RECONCILE_INTERVAL_SEC — 0 disables M3 loop
    routing_freshness_wait_sec: float = 2.0  # MESH_ROUTING_FRESHNESS_WAIT_SEC — pre-route nudge wait; 0 disables
    routing_live_state_max_age_sec: int = 90  # MESH_ROUTING_LIVE_STATE_MAX_AGE_SEC — stale state is ignored for slot routing
    shadow_write: bool = True               # always mirror to DB even when mesh routing is off
    # State Separation Phase 2: when False (default), the task server runs as its
    # own process (server_main.py / ai-team-server) and the gateway reaches it
    # over HTTP + the shared DB. Set MESH_EMBEDDED_SERVER=true to run it embedded
    # inside the gateway (single-process / fallback mode). Never run both at once.
    embedded_server: bool = False           # MESH_EMBEDDED_SERVER
    # Mesh health sliding-window detection (Phase 4.1)
    mesh_health_window_size: int = 6         # MESH_HEALTH_WINDOW_SIZE
    mesh_health_failure_threshold: int = 3   # MESH_HEALTH_FAILURE_THRESHOLD
    # Cockpit M3 read-only web dashboard (consumes db.list_* + events.ndjson).
    dashboard_port: int = 9003               # DASHBOARD_PORT
    dashboard_token: str = ""                # DASHBOARD_TOKEN — falls back to worker_token
    # Control API embedded in the gateway process (U1 — replaces dashboard_main).
    # Serves the read API on dashboard_port from inside the gateway, sharing its
    # live SessionService / NodeRegistry. Default on; set false to disable.
    control_api_enabled: bool = True         # CONTROL_API_ENABLED
    # Bind host for the Control API (UI + API). Empty → falls back to tailscale_ip,
    # then 127.0.0.1. Set to the machine's Tailscale IP to expose only to the tailnet.
    # Never set 0.0.0.0 unless you intend to expose it on every interface.
    control_api_host: str = ""               # CONTROL_API_HOST


@dataclass
class TelemetryConfig:
    enabled: bool = True
    detailed_events: bool = True
    upload_batch_size: int = 50
    upload_interval_ms: int = 1000
    upload_max_bytes: int = 524_288
    spool_max_bytes: int = 268_435_456
    event_retention_days: int = 30
    summary_retention_days: int = 180
    task_server_url: str = ""
    otlp_endpoint: str = ""


class Config:
    """Main configuration class"""

    def __init__(self):
        self.claude = ClaudeConfig(
            base_command=self._get_claude_command(),
            skip_permissions=os.getenv("CLAUDE_SKIP_PERMISSIONS", "false").lower() == "true"
        )
        # Base working directory and allowlist root are expected via environment overrides; no OS-specific default.
        # Optional overrides from env
        try:
            mt = os.getenv("CLAUDE_MAX_TURNS")
            if mt is not None:
                self.claude.max_turns = max(1, int(mt))
        except Exception:
            pass
        try:
            to = os.getenv("CLAUDE_TIMEOUT_SEC")
            if to is not None:
                self.claude.timeout = max(1, int(to))
        except Exception:
            pass
        self.llama = LlamaConfig()
        self.telegram = TelegramConfig(
            bot_token=os.getenv("GATEWAY_TELEGRAM_BOT_TOKEN", ""),
            allowed_users=self._parse_allowed_users(),
            notification_chat_id=self._parse_chat_id()
        )
        self.validation = ValidationConfig()
        self.system = SystemConfig()
        self.codex = CodexConfig()
        self.opencode = OpenCodeConfig()
        self.mesh = MeshConfig()
        self.telemetry = TelemetryConfig()
        # Apply env overrides for selected runtime-tunable settings
        self._apply_env_overrides()
        
    def _get_claude_command(self) -> List[str]:
        """Determine the best Claude Code command configuration"""
        base_cmd = ["claude"]  # Use the correct claude command
        
        # Check for automation flags
        if os.getenv("CLAUDE_SKIP_PERMISSIONS", "false").lower() == "true":
            base_cmd.append("--dangerously-skip-permissions")
        
        base_cmd.extend([
            "--output-format", "json",
            "-p"  # Headless mode
        ])
        
        return base_cmd
        
    def _parse_allowed_users(self) -> List[int]:
        """Parse allowed Telegram users from environment"""
        users_str = os.getenv("GATEWAY_TELEGRAM_ALLOWED_USERS", "")
        if not users_str:
            return []
        
        try:
            return [int(uid.strip()) for uid in users_str.split(",") if uid.strip()]
        except ValueError:
            return []
    
    def _parse_chat_id(self) -> Optional[int]:
        """Parse Telegram chat ID from environment"""
        chat_id_str = os.getenv("GATEWAY_TELEGRAM_CHAT_ID", "")
        if not chat_id_str:
            return None
        
        try:
            return int(chat_id_str)
        except ValueError:
            return None
        
    def validate(self) -> List[str]:
        """Validate configuration and return any errors"""
        errors = []
        
        if not self.telegram.bot_token:
            errors.append("GATEWAY_TELEGRAM_BOT_TOKEN environment variable is required")
            
        if not self.telegram.allowed_users:
            errors.append("GATEWAY_TELEGRAM_ALLOWED_USERS environment variable is required")
            
        if self.telegram.notification_chat_id == 0:
            errors.append("GATEWAY_TELEGRAM_CHAT_ID environment variable is required")
            
        return errors

    @staticmethod
    def _validated_default_model(backend: str, value: str) -> Optional[str]:
        """Validate a *_DEFAULT_MODEL env value through the model catalog.

        Strict backends (claude/codex) reject unknown names → None (the catalog
        default applies downstream) so one .env typo can't break every session.
        Advisory backends (opencode) pass unknown names through (provider set is
        environment-specific). See config/models.py.
        """
        try:
            from config.models import validate as _validate_model
            return _validate_model(backend, value)
        except Exception:
            # If the catalog can't be imported for any reason, don't block startup.
            return value

    def _apply_env_overrides(self) -> None:
        """Apply environment variable overrides to runtime-tunable settings."""
        try:
            mt = os.getenv("CLAUDE_MAX_TURNS")
            if mt is not None:
                self.claude.max_turns = max(0, int(mt))
        except Exception:
            pass
        try:
            to = os.getenv("CLAUDE_TIMEOUT_SEC")
            if to is not None:
                self.claude.timeout = max(1, int(to))
        except Exception:
            pass
        # Optional working directory overrides (Windows-first). If provided, use them.
        try:
            base = os.getenv("CLAUDE_BASE_CWD")
            if base:
                self.claude.base_cwd = base
                # Default allowed_root to base if not explicitly set via env
                self.claude.allowed_root = base
            allowed = os.getenv("CLAUDE_ALLOWED_ROOT")
            if allowed:
                self.claude.allowed_root = allowed
        except Exception:
            pass
        # Guarded write mode
        try:
            gw = os.getenv("GUARDED_WRITE")
            if gw is not None:
                self.system.guarded_write = gw.lower() == "true"
        except Exception:
            pass
        # Rate limiting and backpressure settings
        try:
            max_tasks = os.getenv("MAX_CONCURRENT_TASKS")
            if max_tasks is not None:
                self.system.max_concurrent_tasks = max(1, int(max_tasks))
        except Exception:
            pass
        try:
            max_queue = os.getenv("MAX_QUEUE_SIZE")
            if max_queue is not None:
                self.system.max_queue_size = max(1, int(max_queue))
        except Exception:
            pass
        try:
            tg_rate = os.getenv("TELEGRAM_RATE_LIMIT_REQUESTS")
            if tg_rate is not None:
                self.system.telegram_rate_limit_requests = max(1, int(tg_rate))
        except Exception:
            pass
        try:
            tg_window = os.getenv("TELEGRAM_RATE_LIMIT_WINDOW_SEC")
            if tg_window is not None:
                self.system.telegram_rate_limit_window_sec = max(1, int(tg_window))
        except Exception:
            pass
        try:
            tg_buffer = os.getenv("TELEGRAM_MESSAGE_BUFFER_SEC")
            if tg_buffer is not None:
                self.system.telegram_message_buffer_sec = max(0.0, float(tg_buffer))
        except Exception:
            pass
        try:
            v = os.getenv("GATEWAY_UPLOAD_MAX_MB")
            if v is not None:
                self.telegram.upload_max_mb = max(0, int(v))
        except Exception:
            pass
        try:
            gtt = os.getenv("GATEWAY_TASK_TIMEOUT_SEC")
            if gtt is not None:
                self.system.task_timeout = max(60, int(gtt))
        except Exception:
            pass
        try:
            ghi = os.getenv("GATEWAY_HEARTBEAT_INTERVAL_SEC")
            if ghi is not None:
                self.system.task_heartbeat_interval_sec = max(30, int(ghi))
        except Exception:
            pass
        try:
            gia = os.getenv("GATEWAY_INACTIVITY_TIMEOUT_SEC")
            if gia is not None:
                self.system.inactivity_timeout_sec = max(60, int(gia))
        except Exception:
            pass
        try:
            gst = os.getenv("GATEWAY_SDK_TURN_TIMEOUT_SEC")
            if gst is not None:
                self.system.sdk_turn_timeout_sec = max(0, int(gst))
        except Exception:
            pass
        # OpenCode env overrides
        try:
            v = os.getenv("OPENCODE_TIMEOUT_SEC")
            if v is not None:
                self.opencode.timeout_seconds = max(60, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("OPENCODE_DEFAULT_MODEL")
            if v:
                self.opencode.default_model = self._validated_default_model("opencode", v) or self.opencode.default_model
        except Exception:
            pass
        # Claude / Codex default model env overrides (validated through the catalog).
        try:
            v = os.getenv("CLAUDE_DEFAULT_MODEL")
            if v:
                self.claude.default_model = self._validated_default_model("claude", v)
        except Exception:
            pass
        try:
            v = os.getenv("CODEX_DEFAULT_MODEL")
            if v:
                self.codex.default_model = self._validated_default_model("codex", v)
        except Exception:
            pass
        try:
            v = os.getenv("OPENCODE_DEFAULT_AGENT")
            if v:
                self.opencode.default_agent = v
        except Exception:
            pass
        try:
            v = os.getenv("OPENCODE_SERVER_ENABLED")
            if v is not None:
                self.opencode.server_enabled = v.lower() == "true"
        except Exception:
            pass
        try:
            v = os.getenv("OPENCODE_MODE")
            if v in ("cli", "server"):
                self.opencode.mode = v
        except Exception:
            pass
        # Mesh env overrides
        try:
            v = os.getenv("MESH_ENABLED")
            if v is not None:
                self.mesh.enabled = v.lower() == "true"
        except Exception:
            pass
        try:
            v = os.getenv("MESH_DB_PATH")
            if v:
                self.mesh.db_path = v
        except Exception:
            pass
        try:
            v = os.getenv("MESH_TAILSCALE_IP")
            if v:
                self.mesh.tailscale_ip = v
        except Exception:
            pass
        try:
            v = os.getenv("MESH_TASK_SERVER_PORT")
            if v is not None:
                self.mesh.task_server_port = int(v)
        except Exception:
            pass
        try:
            v = os.getenv("WORKER_TOKEN")
            if v:
                self.mesh.worker_token = v
        except Exception:
            pass
        try:
            v = os.getenv("MESH_EMBEDDED_SERVER")
            if v is not None:
                self.mesh.embedded_server = v.lower() == "true"
        except Exception:
            pass
        try:
            v = os.getenv("DASHBOARD_PORT")
            if v is not None:
                self.mesh.dashboard_port = int(v)
        except Exception:
            pass
        try:
            v = os.getenv("DASHBOARD_TOKEN")
            if v:
                self.mesh.dashboard_token = v
        except Exception:
            pass
        try:
            v = os.getenv("CONTROL_API_ENABLED")
            if v is not None:
                self.mesh.control_api_enabled = v.lower() == "true"
        except Exception:
            pass
        try:
            v = os.getenv("CONTROL_API_HOST")
            if v is not None:
                self.mesh.control_api_host = v.strip()
        except Exception:
            pass
        try:
            v = os.getenv("MESH_HEARTBEAT_TIMEOUT_SEC")
            if v is not None:
                self.mesh.node_heartbeat_timeout_sec = max(10, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("MESH_ONEOFF_QUEUE_TIMEOUT_SEC")
            if v is not None:
                self.mesh.oneoff_queue_timeout_sec = max(60, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("MESH_CLAIM_LEASE_SEC")
            if v is not None:
                self.mesh.claim_lease_sec = max(30, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("MESH_CLAIM_MAX_RUNTIME_SEC")
            if v is not None:
                self.mesh.claim_max_runtime_sec = max(60, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("MESH_SESSION_RECONCILE_INTERVAL_SEC")
            if v is not None:
                self.mesh.session_reconcile_interval_sec = max(0, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("MESH_ROUTING_FRESHNESS_WAIT_SEC")
            if v is not None:
                self.mesh.routing_freshness_wait_sec = max(0.0, float(v))
        except Exception:
            pass
        try:
            v = os.getenv("MESH_ROUTING_LIVE_STATE_MAX_AGE_SEC")
            if v is not None:
                self.mesh.routing_live_state_max_age_sec = max(1, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("MESH_SHADOW_WRITE")
            if v is not None:
                self.mesh.shadow_write = v.lower() != "false"
        except Exception:
            pass
        try:
            v = os.getenv("MESH_HEALTH_WINDOW_SIZE")
            if v is not None:
                self.mesh.mesh_health_window_size = max(2, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("MESH_HEALTH_FAILURE_THRESHOLD")
            if v is not None:
                self.mesh.mesh_health_failure_threshold = max(1, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("TELEMETRY_ENABLED")
            if v is not None:
                self.telemetry.enabled = v.lower() != "false"
        except Exception:
            pass
        try:
            v = os.getenv("TELEMETRY_DETAILED_EVENTS")
            if v is not None:
                self.telemetry.detailed_events = v.lower() != "false"
        except Exception:
            pass
        try:
            v = os.getenv("TELEMETRY_UPLOAD_BATCH_SIZE")
            if v is not None:
                self.telemetry.upload_batch_size = max(1, min(200, int(v)))
        except Exception:
            pass
        try:
            v = os.getenv("TELEMETRY_UPLOAD_INTERVAL_MS")
            if v is not None:
                self.telemetry.upload_interval_ms = max(100, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("TELEMETRY_UPLOAD_MAX_BYTES")
            if v is not None:
                self.telemetry.upload_max_bytes = max(65_536, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("TELEMETRY_SPOOL_MAX_BYTES")
            if v is not None:
                self.telemetry.spool_max_bytes = max(1_048_576, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("TELEMETRY_EVENT_RETENTION_DAYS")
            if v is not None:
                self.telemetry.event_retention_days = max(1, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("TELEMETRY_SUMMARY_RETENTION_DAYS")
            if v is not None:
                self.telemetry.summary_retention_days = max(1, int(v))
        except Exception:
            pass
        try:
            v = os.getenv("TELEMETRY_TASK_SERVER_URL")
            if v:
                self.telemetry.task_server_url = v.rstrip("/")
        except Exception:
            pass
        try:
            v = os.getenv("TELEMETRY_OTLP_ENDPOINT")
            if v:
                self.telemetry.otlp_endpoint = v
        except Exception:
            pass

    def reload_from_env(self) -> None:
        """Reload environment-derived configuration fields at runtime.

        Notes:
        - Safe to call during runtime; only adjusts fields that are read on turn execution.
        - Base command is re-evaluated to reflect flag env changes.
        """
        # Recompute fields derived from env
        self.claude.skip_permissions = os.getenv("CLAUDE_SKIP_PERMISSIONS", "false").lower() == "true"
        self.claude.base_command = self._get_claude_command()
        self._apply_env_overrides()

# Global config instance
config = Config()
