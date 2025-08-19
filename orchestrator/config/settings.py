"""
Configuration settings for the AI Task Orchestrator
"""
import os
from dataclasses import dataclass
from typing import List, Dict, Any

@dataclass
class ClaudeConfig:
    """Claude Code CLI configuration"""
    base_command: List[str]
    output_format: str = "json"
    headless_mode: bool = True
    skip_permissions: bool = False
    timeout: int = 300  # 5 minutes
    max_turns: int = 10
    
@dataclass
class LlamaConfig:
    """Local LLAMA configuration"""
    model: str = "llama3.2:latest"
    host: str = "localhost"
    port: int = 11434
    timeout: int = 120
    context_window: int = 128000  # 128k context for llama3.2
    
@dataclass
class TelegramConfig:
    """Telegram bot configuration"""
    bot_token: str
    allowed_users: List[int]
    notification_chat_id: int
    
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
        self.llama = LlamaConfig()
        self.telegram = TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            allowed_users=self._parse_allowed_users(),
            notification_chat_id=int(os.getenv("TELEGRAM_CHAT_ID", 0))
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
        return [int(uid.strip()) for uid in users_str.split(",")]
        
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