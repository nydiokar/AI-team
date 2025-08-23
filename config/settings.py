"""
Configuration settings for the AI Task Orchestrator
"""
import os
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    # Prefer .env from this project root (AI-team/.env); fallback to CWD
    project_env = Path(__file__).parent.parent / ".env"
    cwd_env = Path.cwd() / ".env"
    if project_env.exists():
        load_dotenv(project_env)
        print(f"Loaded environment from: {project_env}")
    elif cwd_env.exists():
        load_dotenv(cwd_env)
        print(f"Loaded environment from: {cwd_env}")
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
    
    def __post_init__(self):
        if self.allowed_users is None:
            self.allowed_users = []
            
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
    task_timeout: int = 1800  # 30 minutes
    
class Config:
    """Main configuration class"""
    
    def __init__(self):
        self.claude = ClaudeConfig(
            base_command=self._get_claude_command(),
            skip_permissions=os.getenv("CLAUDE_SKIP_PERMISSIONS", "false").lower() == "true"
        )
        # Base working directory and allowlist root (configured in code by request)
        # Note: This is intentionally not sourced from environment variables.
        self.claude.base_cwd = r"C:\Users\Cicada38\Projects"
        self.claude.allowed_root = self.claude.base_cwd
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
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            allowed_users=self._parse_allowed_users(),
            notification_chat_id=self._parse_chat_id()
        )
        self.validation = ValidationConfig()
        self.system = SystemConfig()
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
        users_str = os.getenv("TELEGRAM_ALLOWED_USERS", "")
        if not users_str:
            return []
        
        try:
            return [int(uid.strip()) for uid in users_str.split(",") if uid.strip()]
        except ValueError:
            return []
    
    def _parse_chat_id(self) -> Optional[int]:
        """Parse Telegram chat ID from environment"""
        chat_id_str = os.getenv("TELEGRAM_CHAT_ID", "")
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
            errors.append("TELEGRAM_BOT_TOKEN environment variable is required")
            
        if not self.telegram.allowed_users:
            errors.append("TELEGRAM_ALLOWED_USERS environment variable is required")
            
        if self.telegram.notification_chat_id == 0:
            errors.append("TELEGRAM_CHAT_ID environment variable is required")
            
        return errors

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

    def reload_from_env(self) -> None:
        """Reload environment-derived configuration fields at runtime.

        Notes:
        - Safe to call during runtime; only adjusts fields that are read on turn execution.
        - Base command is re-evaluated to reflect flag env changes.
        """
        # Recompute fields derived from env
        self.claude.skip_permissions = os.getenv("CLAUDE_SKIP_PERMISSIONS", "false").lower() == "true"
        self.claude.base_command = self._get_claude_command()
        # Re-apply runtime overrides
        self._apply_env_overrides()

# Global config instance
config = Config()