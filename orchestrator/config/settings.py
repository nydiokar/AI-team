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
    # Load .env from the orchestrator directory
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"Loaded environment from: {env_path}")
    else:
        print(f"Warning: .env file not found at {env_path}")
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
    max_turns: int = 10
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
        self.llama = LlamaConfig()
        self.telegram = TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            allowed_users=self._parse_allowed_users(),
            notification_chat_id=self._parse_chat_id()
        )
        self.validation = ValidationConfig()
        self.system = SystemConfig()
        
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

# Global config instance
config = Config()