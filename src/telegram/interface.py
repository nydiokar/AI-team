"""
Telegram bot interface for AI Task Orchestrator
"""
import asyncio
import logging
from typing import Dict, Any, Optional
from pathlib import Path

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    # Mock classes for when telegram is not available
    class Update:
        def __init__(self):
            self.message = None
            self.effective_user = None
    
    class ContextTypes:
        class Context:
            def __init__(self):
                self.args = []

logger = logging.getLogger(__name__)


class TelegramInterface:
    """Telegram bot interface for task management and notifications"""
    
    def __init__(self, bot_token: str, orchestrator, allowed_users: list[int] = None):
        self.bot_token = bot_token
        self.orchestrator = orchestrator
        self.allowed_users = allowed_users or []
        self.app: Optional[Application] = None
        self.is_running = False
        # Rate limiting for task creation
        self._rate_limit_state: Dict[int, list[float]] = {}
        
        if not TELEGRAM_AVAILABLE:
            logger.warning("python-telegram-bot not available. Telegram interface disabled.")
            return
            
        try:
            self.app = Application.builder().token(bot_token).build()
            self._setup_handlers()
            logger.info("Telegram interface initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Telegram interface: {e}")
            self.app = None
    
    def _setup_handlers(self):
        """Set up command and message handlers"""
        if not self.app:
            return
            
        # Command handlers
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("help", self._handle_help))
        self.app.add_handler(CommandHandler("task", self._handle_task_command))
        self.app.add_handler(CommandHandler("status", self._handle_status_command))
        self.app.add_handler(CommandHandler("progress", self._handle_progress_command))
        self.app.add_handler(CommandHandler("cancel", self._handle_cancel_command))
        # Agent command handlers (explicit)
        self.app.add_handler(CommandHandler("documentation", self._handle_agent_documentation))
        self.app.add_handler(CommandHandler("code_review", self._handle_agent_code_review))
        self.app.add_handler(CommandHandler("bug_fix", self._handle_agent_bug_fix))
        self.app.add_handler(CommandHandler("analyze", self._handle_agent_analyze))
        
        # Git automation command handlers
        self.app.add_handler(CommandHandler("commit", self._handle_git_commit))
        self.app.add_handler(CommandHandler("commit-all", self._handle_git_commit_all))
        self.app.add_handler(CommandHandler("git-status", self._handle_git_status))
        
        # Message handler for natural language task creation
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
    
    async def start(self):
        """Start the Telegram bot"""
        if not self.app or self.is_running:
            return
            
        try:
            await self.app.initialize()
            await self.app.start()
            await self.app.updater.start_polling()
            self.is_running = True
            logger.info("Telegram bot started successfully")
        except Exception as e:
            logger.error(f"Failed to start Telegram bot: {e}")
    
    async def stop(self):
        """Stop the Telegram bot"""
        if not self.app or not self.is_running:
            return
            
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            self.is_running = False
            logger.info("Telegram bot stopped")
        except Exception as e:
            logger.error(f"Error stopping Telegram bot: {e}")
    
    def _check_user_permission(self, user_id: int) -> bool:
        """Check if user is allowed to use the bot"""
        if not self.allowed_users:
            return True  # No restrictions if no allowed users specified
        return user_id in self.allowed_users
    
    def _check_rate_limit(self, user_id: int) -> bool:
        """Check if user is within rate limits for task creation"""
        import time
        try:
            from config import config as app_config
            max_requests = app_config.system.telegram_rate_limit_requests
            window_sec = app_config.system.telegram_rate_limit_window_sec
        except Exception:
            max_requests = 5
            window_sec = 60
        
        current_time = time.time()
        
        # Get user's request history
        user_requests = self._rate_limit_state.get(user_id, [])
        
        # Remove old requests outside the time window
        user_requests = [req_time for req_time in user_requests if current_time - req_time < window_sec]
        
        # Check if user is within limits
        if len(user_requests) >= max_requests:
            return False
        
        # Add current request
        user_requests.append(current_time)
        self._rate_limit_state[user_id] = user_requests
        
        return True
    
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied. You are not authorized to use this bot.")
            return
            
        welcome_text = """
🤖 AI Task Orchestrator Bot

Available commands:
/task <description> - Create a new task
/status - Show system status  
/cancel <task_id> - Cancel a running task
/help - Show this help message

You can also just send me a message describing what you want to do!
        """.strip()
        
        await update.message.reply_text(welcome_text)
    
    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
            
        help_text = """
📚 AI Task Orchestrator Help

**Commands:**
• `/task <description>` - Create a new AI task
• `/status` - Show current system status
• `/progress <task_id>` - Show recent events for a task
• `/cancel <task_id>` - Cancel a running task
• `/documentation <intent>` - Create a documentation task (attach files optionally)
• `/code_review <intent>` - Create a code review task
• `/bug_fix <intent>` - Create a bug fix task
• `/analyze <intent>` - Create an analysis task

**Git Automation:**
• `/commit <task_id> [--no-branch] [--push]` - Commit task changes safely
• `/commit-all <task_id> [--no-branch] [--push]` - Commit all staged changes
• `/git-status` - Show git repository status and safety info

**Examples:**
• `/task Review the authentication code in /auth-system`
• `/task Create a new pijama directory and set up a Python project there`
• `/task Fix the database connection timeout in /backend`

**Working Directories:**
• Use "in /project-name" to specify where to work
• Use "in C:\\path\\to\\project" for absolute paths
• If no path specified, Claude starts in Projects root

**Task Types:**
• `fix` - Bug fixes and error corrections
• `analyze` - Code analysis and improvements  
• `code_review` - Code review and feedback
• `summarize` - Code summarization and documentation
        """.strip()
        
        await update.message.reply_text(help_text)
    
    async def _handle_task_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /task command"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
            
        # Check rate limiting
        if not self._check_rate_limit(update.effective_user.id):
            try:
                from config import config as app_config
                window_sec = app_config.system.telegram_rate_limit_window_sec
                max_req = app_config.system.telegram_rate_limit_requests
            except Exception:
                window_sec = 60
                max_req = 5
            await update.message.reply_text(
                f"🚫 Rate limit exceeded. Maximum {max_req} task requests per {window_sec} seconds."
            )
            return
            
        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a task description.\n"
                "Example: /task Review the authentication code in /auth-system"
            )
            return
        
        task_description = " ".join(context.args)
        user_id = update.effective_user.id
        
        try:
            # Create task using orchestrator
            task_id = self.orchestrator.create_task_from_description(task_description)
            
            # Get task file path for confirmation
            try:
                from config import config as app_config
                tasks_dir = Path(app_config.system.tasks_dir)
            except Exception:
                tasks_dir = Path("tasks")
            task_file = tasks_dir / f"{task_id}.task.md"
            
            response = f"""
✅ Task created successfully!

**Task ID:** `{task_id}`
**Description:** {task_description}
**File:** `{task_file.name}`

The system will now process this task automatically. You'll receive a notification when it completes.
            """.strip()
            
            await update.message.reply_text(response)
            
            # Log the task creation
            logger.info(f"Telegram user {user_id} created task {task_id}: {task_description}")
            
        except Exception as e:
            error_msg = f"❌ Failed to create task: {str(e)}"
            await update.message.reply_text(error_msg)
            logger.error(f"Telegram task creation failed: {e}")
    
    async def _handle_status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
            
        try:
            # Get orchestrator status
            status = self.orchestrator.get_status()
            
            # Format status response
            # Resolve base working directory safely
            try:
                from config import config as app_config
                base_cwd = app_config.claude.base_cwd
            except Exception:
                base_cwd = ""
            status_text = f"""
📊 System Status

**Components:**
• Claude Code CLI: {'✅ Available' if status['components']['claude_available'] else '❌ Not available'}
• LLAMA/Ollama: {'✅ Available' if status['components']['llama_available'] else '❌ Not available'}
• File Watcher: {'✅ Running' if status['components']['file_watcher_running'] else '❌ Stopped'}

**Tasks:**
• Active: {status['tasks']['active']}
• Queued: {status['tasks']['queued']}
• Completed: {status['tasks']['completed']}
• Workers: {status['tasks']['workers']}

**Working Directory:** `{base_cwd}`
            """.strip()
            
            await update.message.reply_text(status_text)
            
        except Exception as e:
            error_msg = f"❌ Failed to get status: {str(e)}"
            await update.message.reply_text(error_msg)
            logger.error(f"Telegram status request failed: {e}")

    async def _handle_progress_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /progress <task_id> command to show recent events"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        if not context.args:
            await update.message.reply_text("❌ Please provide a task ID. Example: /progress task_abc123")
            return
        task_id = context.args[0]
        # Read recent events from NDJSON
        try:
            from config import config as app_config
            events_path = Path(app_config.system.logs_dir) / "events.ndjson"
            if not events_path.exists():
                await update.message.reply_text("No events found.")
                return
            import json as _json
            from collections import deque as _deque
            buf = _deque(maxlen=20)
            with events_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = _json.loads(line)
                    except Exception:
                        continue
                    if ev.get("task_id") == task_id:
                        buf.append(ev)
            if not buf:
                await update.message.reply_text(f"No recent events for `{task_id}`.")
                return
            lines = [self._format_progress_line(ev) for ev in list(buf)[-10:]]
            header = f"📈 Progress for `{task_id}` (last {len(lines)} events)"
            await update.message.reply_text("\n".join([header, *lines]))
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to load progress: {e}")
            logger.error(f"Telegram progress failed: {e}")

    def _format_progress_line(self, ev: Dict[str, Any]) -> str:
        """Format a single NDJSON event into a concise, human-readable line."""
        ts = ev.get("timestamp", "")
        # Use HH:MM:SS for brevity when possible
        try:
            tshort = ts.split("T", 1)[-1][:8]
        except Exception:
            tshort = ts
        name = ev.get("event", "")
        pretty = name
        icon = "•"
        details = ""
        if name == "task_received":
            icon = "📥"
            src = ev.get("file")
            details = f"from {Path(src).name}" if src else ""
        elif name == "parsed":
            icon = "🧩"
        elif name == "claude_started":
            icon = "🚀"
            worker = ev.get("worker")
            details = f"worker {worker}" if worker else ""
        elif name == "summarized":
            icon = "📝"
        elif name == "validated":
            icon = "✅"
            vl = ev.get("valid_llama")
            vr = ev.get("valid_result")
            if vl is not None or vr is not None:
                details = f"llama={vl} result={vr}"
        elif name == "retry":
            icon = "🔁"
            attempt = ev.get("attempt")
            cls = ev.get("class")
            delay = ev.get("delay_s")
            details = f"attempt {attempt} class={cls} delay={delay:.2f}s" if isinstance(delay, (int, float)) else f"attempt {attempt} class={cls}"
        elif name == "timeout":
            icon = "⏱️"
            to = ev.get("timeout_s")
            details = f"after {to}s" if to is not None else ""
        elif name == "claude_finished":
            icon = "🏁"
            status = ev.get("status")
            dur = ev.get("duration_s")
            details = f"{status} in {dur:.2f}s" if isinstance(dur, (int, float)) else f"{status}"
        elif name == "artifacts_written":
            icon = "💾"
        elif name == "task_archived":
            icon = "📦"
            to_path = ev.get("to")
            details = f"→ {Path(to_path).name}" if to_path else ""
        elif name == "artifacts_error":
            icon = "⚠️"
            details = ev.get("error", "")
        # Fallback pretty name
        pretty_map = {
            "task_received": "received",
            "parsed": "parsed",
            "claude_started": "started",
            "summarized": "summarized",
            "validated": "validated",
            "retry": "retry",
            "timeout": "timeout",
            "claude_finished": "finished",
            "artifacts_written": "artifacts",
            "artifacts_error": "artifacts error",
            "task_archived": "archived",
        }
        pretty = pretty_map.get(name, name)
        tail = f" — {details}" if details else ""
        return f"{tshort} {icon} {pretty}{tail}"
    
    async def _handle_cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel command"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
            
        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a task ID to cancel.\n"
                "Example: /cancel task_abc123"
            )
            return
        
        task_id = context.args[0]
        
        try:
            ok = False
            try:
                ok = bool(self.orchestrator.cancel_task(task_id))
            except Exception:
                ok = False
            if ok:
                response = f"🔄 Cancellation requested for task `{task_id}`."
                await update.message.reply_text(response)
                logger.info(f"Telegram user requested cancellation of task {task_id}")
            else:
                await update.message.reply_text(f"❌ Task `{task_id}` not found or already finished.")
                
        except Exception as e:
            error_msg = f"❌ Failed to cancel task: {str(e)}"
            await update.message.reply_text(error_msg)
            logger.error(f"Telegram task cancellation failed: {e}")
    
    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle natural language messages as task creation requests"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
            
        # Check rate limiting for task creation
        if not self._check_rate_limit(update.effective_user.id):
            try:
                from config import config as app_config
                window_sec = app_config.system.telegram_rate_limit_window_sec
                max_req = app_config.system.telegram_rate_limit_requests
            except Exception:
                window_sec = 60
                max_req = 5
            await update.message.reply_text(
                f"🚫 Rate limit exceeded. Maximum {max_req} task requests per {window_sec} seconds."
            )
            return
            
        message_text = update.message.text.strip()
        user_id = update.effective_user.id
        
        # Skip very short messages
        if len(message_text) < 10:
            await update.message.reply_text(
                "🤔 Please provide a more detailed description of what you'd like me to do.\n"
                "Example: 'Review the authentication code in /auth-system'"
            )
            return
        
        try:
            # Create task from natural language
            task_id = self.orchestrator.create_task_from_description(message_text)
            
            # Get task file path
            try:
                from config import config as app_config
                tasks_dir = Path(app_config.system.tasks_dir)
            except Exception:
                tasks_dir = Path("tasks")
            task_file = tasks_dir / f"{task_id}.task.md"
            
            response = f"""
✅ Task created from your message!

**Task ID:** `{task_id}`
**Description:** {message_text[:100]}{'...' if len(message_text) > 100 else ''}
**File:** `{task_file.name}`

The system will now process this task automatically. You'll receive a notification when it completes.
            """.strip()
            
            await update.message.reply_text(response)
            
            # Log the task creation
            logger.info(f"Telegram user {user_id} created task {task_id} from message: {message_text[:100]}...")
            
        except Exception as e:
            error_msg = f"❌ Failed to create task from message: {str(e)}"
            await update.message.reply_text(error_msg)
            logger.error(f"Telegram message-to-task creation failed: {e}")

    # --- Agent commands ---
    async def _handle_agent_command(self, agent: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
            
        # Check rate limiting for task creation
        if not self._check_rate_limit(update.effective_user.id):
            try:
                from config import config as app_config
                window_sec = app_config.system.telegram_rate_limit_window_sec
                max_req = app_config.system.telegram_rate_limit_requests
            except Exception:
                window_sec = 60
                max_req = 5
            await update.message.reply_text(
                f"🚫 Rate limit exceeded. Maximum {max_req} task requests per {window_sec} seconds."
            )
            return
        intent_text = " ".join(context.args).strip()
        if not intent_text:
            await update.message.reply_text("❌ Please provide a brief intent or description.")
            return
        try:
            # Download attached documents (if any) to a safe location under tasks/
            files: list[str] = []
            try:
                if update.message and update.message.document:
                    doc = update.message.document
                    tg_file = await context.bot.get_file(doc.file_id)
                    from config import config as app_config
                    attachments_dir = Path(app_config.system.tasks_dir) / "attachments"
                    attachments_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = doc.file_name or f"file_{doc.file_id}"
                    target_path = attachments_dir / safe_name
                    await tg_file.download_to_drive(custom_path=str(target_path))
                    files.append(str(target_path))
            except Exception as e:
                logger.warning(f"Attachment download failed or none present: {e}")

            # Create simple task directly (no LLAMA expansion)
            task_id = self.orchestrator.create_task_from_description(
                f"{agent.replace('_', ' ').title()}: {intent_text}",
                task_type=agent,
                target_files=files
            )

            await update.message.reply_text(
                f"✅ {agent.replace('_',' ').title()} task created: `{task_id}`\n"
                f"Description: {intent_text}"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to create {agent} task: {e}")
            logger.error(f"Agent command failed ({agent}): {e}")

    async def _handle_agent_documentation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._handle_agent_command("documentation", update, context)

    async def _handle_agent_code_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._handle_agent_command("code_review", update, context)

    async def _handle_agent_bug_fix(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._handle_agent_command("bug_fix", update, context)

    async def _handle_agent_analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._handle_agent_command("analyze", update, context)
    
    async def notify_completion(self, task_id: str, summary: str, success: bool = True):
        """Notify users of task completion"""
        if not self.app or not self.is_running:
            return
            
        try:
            status_icon = "✅" if success else "❌"
            status_text = "COMPLETED" if success else "FAILED"
            
            message = f"""
{status_icon} Task {task_id} {status_text}

**Summary:**
{summary[:500]}{'...' if len(summary) > 500 else ''}

**Next Steps:**
Check the results in `results/{task_id}.json` and summary in `summaries/{task_id}_summary.txt`
            """.strip()
            
            # Preferred: send to allowed users
            if self.allowed_users:
                for user_id in self.allowed_users:
                    try:
                        await self.app.bot.send_message(chat_id=user_id, text=message)
                    except Exception as e:
                        logger.warning(f"Failed to notify user {user_id}: {e}")
            else:
                # Fallback: use configured notification chat id when allowlist is empty
                try:
                    from config import config as app_config
                    chat_id = getattr(app_config.telegram, "notification_chat_id", None)
                    if chat_id:
                        await self.app.bot.send_message(chat_id=chat_id, text=message)
                    else:
                        logger.info(f"Task {task_id} completed, but no notification target configured")
                except Exception as e:
                    logger.warning(f"Failed to notify default chat: {e}")
                
        except Exception as e:
            logger.error(f"Failed to send completion notification for task {task_id}: {e}")
    
    async def notify_error(self, error_message: str):
        """Notify users of system errors"""
        if not self.app or not self.is_running:
            return
            
        try:
            message = f"""
🚨 System Error

**Error:** {error_message[:300]}{'...' if len(error_message) > 300 else ''}

Please check the system logs for more details.
            """.strip()
            
            # Send to all allowed users
            if self.allowed_users:
                for user_id in self.allowed_users:
                    try:
                        await self.app.bot.send_message(chat_id=user_id, text=message)
                    except Exception as e:
                        logger.warning(f"Failed to notify user {user_id} of error: {e}")
                        
        except Exception as e:
            logger.error(f"Failed to send error notification: {e}")
    
    def is_available(self) -> bool:
        """Check if Telegram interface is available and configured"""
        return TELEGRAM_AVAILABLE and self.app is not None and bool(self.bot_token)
    
    async def _handle_git_commit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /commit command for committing task-specific changes"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        
        try:
            # Parse command arguments
            args = context.args if context.args else []
            if len(args) < 1:
                await update.message.reply_text(
                    "❌ Usage: `/commit <task_id> [--no-branch] [--push]`\n"
                    "Example: `/commit abc123` or `/commit abc123 --push`"
                )
                return
            
            task_id = args[0]
            create_branch = "--no-branch" not in args
            push_branch = "--push" in args
            
            # Get task information from orchestrator
            task_result = self.orchestrator.task_results.get(task_id)
            if not task_result:
                await update.message.reply_text(f"❌ Task {task_id} not found or not completed")
                return
            
            # Import git automation service
            try:
                from src.core.git_automation import GitAutomationService
                git_service = GitAutomationService()
            except ImportError as e:
                await update.message.reply_text(f"❌ Git automation service not available: {e}")
                return
            
            # Get task description for commit message
            task_description = task_result.get('task', {}).get('description', f'Task {task_id}')
            
            # Perform safe commit
            result = git_service.safe_commit_task(
                task_id=task_id,
                task_description=task_description,
                create_branch=create_branch,
                push_branch=push_branch
            )
            
            if result["success"]:
                # Success message
                message_parts = [f"✅ Successfully committed task {task_id}"]
                
                if result["branch_name"]:
                    message_parts.append(f"📁 Branch: `{result['branch_name']}`")
                
                if result["files_committed"]:
                    file_count = len(result["files_committed"])
                    message_parts.append(f"📄 Files committed: {file_count}")
                    if file_count <= 5:
                        for file_path in result["files_committed"][:5]:
                            message_parts.append(f"  • {file_path}")
                    else:
                        message_parts.append(f"  • ... and {file_count - 5} more files")
                
                if result["sensitive_files_blocked"]:
                    blocked_count = len(result["sensitive_files_blocked"])
                    message_parts.append(f"🚫 Sensitive files blocked: {blocked_count}")
                
                if push_branch and result["branch_name"]:
                    message_parts.append(f"🚀 Branch pushed to remote")
                
                await update.message.reply_text("\n".join(message_parts))
            else:
                # Error message
                error_msg = f"❌ Failed to commit task {task_id}:\n"
                for error in result["errors"]:
                    error_msg += f"• {error}\n"
                await update.message.reply_text(error_msg)
                
        except Exception as e:
            await update.message.reply_text(f"❌ Error processing commit command: {e}")
            logger.error(f"Git commit command failed: {e}")
    
    async def _handle_git_commit_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /commit-all command for committing all staged changes"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        
        try:
            # Parse command arguments
            args = context.args if context.args else []
            if len(args) < 1:
                await update.message.reply_text(
                    "❌ Usage: `/commit-all <task_id> [--no-branch] [--push]`\n"
                    "⚠️  This commits ALL staged changes - use with caution!"
                )
                return
            
            task_id = args[0]
            create_branch = "--no-branch" not in args
            push_branch = "--push" in args
            
            # Import git automation service
            try:
                from src.core.git_automation import GitAutomationService
                git_service = GitAutomationService()
            except ImportError as e:
                await update.message.reply_text(f"❌ Git automation service not available: {e}")
                return
            
            # Get task information from orchestrator
            task_result = self.orchestrator.task_results.get(task_id)
            if not task_result:
                await update.message.reply_text(f"❌ Task {task_id} not found or not completed")
                return
            
            task_description = task_result.get('task', {}).get('description', f'Task {task_id}')
            
            # Perform commit all staged
            result = git_service.commit_all_staged(
                task_id=task_id,
                task_description=task_description,
                create_branch=create_branch,
                push_branch=push_branch
            )
            
            if result["success"]:
                message_parts = [f"✅ Successfully committed all staged changes for task {task_id}"]
                
                if result["branch_name"]:
                    message_parts.append(f"📁 Branch: `{result['branch_name']}`")
                
                if result["files_committed"]:
                    file_count = len(result["files_committed"])
                    message_parts.append(f"📄 Files committed: {file_count}")
                
                if push_branch and result["branch_name"]:
                    message_parts.append(f"🚀 Branch pushed to remote")
                
                await update.message.reply_text("\n".join(message_parts))
            else:
                error_msg = f"❌ Failed to commit staged changes for task {task_id}:\n"
                for error in result["errors"]:
                    error_msg += f"• {error}\n"
                await update.message.reply_text(error_msg)
                
        except Exception as e:
            await update.message.reply_text(f"❌ Error processing commit-all command: {e}")
            logger.error(f"Git commit-all command failed: {e}")
    
    async def _handle_git_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /git-status command for showing git repository status"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        
        try:
            # Import git automation service
            try:
                from src.core.git_automation import GitAutomationService
                git_service = GitAutomationService()
            except ImportError as e:
                await update.message.reply_text(f"❌ Git automation service not available: {e}")
                return
            
            # Get git status summary
            status = git_service.get_git_status_summary()
            
            if "error" in status:
                await update.message.reply_text(f"❌ {status['error']}")
                return
            
            # Format status message
            message_parts = ["📊 Git Repository Status"]
            message_parts.append(f"🌿 Branch: `{status['current_branch']}`")
            message_parts.append(f"🧹 Working directory: {'✅ Clean' if status['working_directory_clean'] else '⚠️  Has changes'}")
            
            if not status['working_directory_clean']:
                changes = status['changes']
                message_parts.append(f"\n📝 Changes:")
                message_parts.append(f"  • Modified: {changes['modified']}")
                message_parts.append(f"  • Created: {changes['created']}")
                message_parts.append(f"  • Deleted: {changes['deleted']}")
                message_parts.append(f"  • Total: {changes['total']}")
                
                if status['staged_files']:
                    message_parts.append(f"\n📦 Staged files: {len(status['staged_files'])}")
                    for file_path in status['staged_files'][:3]:
                        message_parts.append(f"  • {file_path}")
                    if len(status['staged_files']) > 3:
                        message_parts.append(f"  • ... and {len(status['staged_files']) - 3} more")
                
                if status['unstaged_files']:
                    message_parts.append(f"\n📋 Unstaged files: {len(status['unstaged_files'])}")
                    for file_path in status['unstaged_files'][:3]:
                        message_parts.append(f"  • {file_path}")
                    if len(status['unstaged_files']) > 3:
                        message_parts.append(f"  • ... and {len(status['unstaged_files']) - 3} more")
                
                # Safety information
                safety = status['safety']
                if safety['has_sensitive_files']:
                    message_parts.append(f"\n🚫 Sensitive files detected: {len(safety['sensitive_files'])}")
                    for file_path in safety['sensitive_files'][:3]:
                        message_parts.append(f"  • {file_path}")
                    if len(safety['sensitive_files']) > 3:
                        message_parts.append(f"  • ... and {len(safety['sensitive_files']) - 3} more")
            
            await update.message.reply_text("\n".join(message_parts))
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error getting git status: {e}")
            logger.error(f"Git status command failed: {e}")
