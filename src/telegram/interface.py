"""
Telegram bot interface for the Telegram Coding Gateway.
"""
import asyncio
import logging
from typing import Dict, Any, Optional
from pathlib import Path

from src.core.session_store import SessionStore
from src.core.interfaces import Session, SessionStatus
from src.core.path_resolver import PathResolver, PathResolution

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
        self.session_store = SessionStore()
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
        # Session command handlers
        self.app.add_handler(CommandHandler("session_new", self._handle_session_new))
        self.app.add_handler(CommandHandler("session_list", self._handle_session_list))
        self.app.add_handler(CommandHandler("session_use", self._handle_session_use))
        self.app.add_handler(CommandHandler("session_dirs", self._handle_session_dirs))
        self.app.add_handler(CommandHandler("session_status", self._handle_session_status))
        self.app.add_handler(CommandHandler("session_cancel", self._handle_session_cancel))
        self.app.add_handler(CommandHandler("session_close", self._handle_session_close))
        self.app.add_handler(CommandHandler("run", self._handle_run_command))
        self.app.add_handler(CommandHandler("say", self._handle_say_command))
        # Git automation command handlers
        self.app.add_handler(CommandHandler("commit", self._handle_git_commit))
        self.app.add_handler(CommandHandler("commit_all", self._handle_git_commit_all))
        self.app.add_handler(CommandHandler("git_status", self._handle_git_status))
        
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

    def _path_resolver(self) -> PathResolver:
        return PathResolver.from_config()

    def _user_can_access_session(self, user_id: int, session: Optional[Session]) -> bool:
        if session is None:
            return False
        if session.owner_user_id is None:
            return True
        return session.owner_user_id == user_id

    def _format_path_resolution_error(self, result: PathResolution) -> str:
        lines = [f"❌ {result.error}"]
        if result.suggestions:
            lines.append("Closest matches:")
            for item in result.suggestions[:6]:
                lines.append(f"• `{item}`")
        elif result.available_dirs:
            lines.append("Available directories nearby:")
            for item in result.available_dirs[:6]:
                lines.append(f"• `{item}`")
        else:
            roots = self._path_resolver().list_root_directories(limit=8)
            if roots:
                lines.append("Configured root directories:")
                for item in roots:
                    lines.append(f"• `{item}`")
        return "\n".join(lines)

    def _format_session_overview(self, session: Session, include_dirs: bool = False) -> str:
        lines = [
            f"Session: `{session.session_id}`",
            f"Backend: {session.backend}  |  Status: {session.status.value}",
            f"Path: `{session.repo_path}`",
            f"Machine: {session.machine_id}",
            f"Updated: {session.updated_at}",
        ]
        if session.backend_session_id:
            lines.append(f"Backend session ID: `{session.backend_session_id}`")
        if session.last_task_id:
            lines.append(f"Last task: `{session.last_task_id}`")
        if session.last_result_summary:
            lines.append(f"Last result: {session.last_result_summary[:200]}")
        if include_dirs:
            dirs = self._path_resolver().list_child_directories(session.repo_path, limit=8)
            if dirs:
                lines.append("Top directories: " + ", ".join(f"`{item}`" for item in dirs))
        return "\n".join(lines)

    async def _queue_instruction(
        self,
        update: Update,
        message_text: str,
        active_session: Optional[Session],
        session_only: bool = False,
    ) -> None:
        message_text = (message_text or "").strip()
        if len(message_text) < 3:
            await update.message.reply_text("❌ Message is too short.")
            return

        if active_session:
            if not self._user_can_access_session(update.effective_user.id, active_session):
                await update.message.reply_text("❌ You do not own the active session.")
                return
            active_session.last_user_message = message_text
            active_session.status = SessionStatus.BUSY
            task_id = self.orchestrator.create_task_from_description(
                message_text,
                session_id=active_session.session_id,
                cwd=active_session.repo_path,
            )
            active_session.last_task_id = task_id
            self.session_store.save(active_session)
            await update.message.reply_text(
                f"Running in session `{active_session.session_id}` [{active_session.backend}]\n"
                f"Task: `{task_id}`"
            )
            logger.info(
                "user=%s chat=%s task=%s session=%s",
                update.effective_user.id,
                update.effective_chat.id,
                task_id,
                active_session.session_id,
            )
            return

        if session_only:
            await update.message.reply_text("❌ No active session. Use /session_new first.")
            return

        task_id = self.orchestrator.create_task_from_description(message_text)
        await update.message.reply_text(
            f"One-off task created: `{task_id}`\n"
            f"Tip: use /session_new to open a persistent coding session."
        )
        logger.info(
            "user=%s chat=%s task=%s session=none",
            update.effective_user.id,
            update.effective_chat.id,
            task_id,
        )
    
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied. You are not authorized to use this bot.")
            return

        await update.message.reply_text(
            "Telegram Coding Gateway\n\n"
            "Primary flow:\n"
            "• `/session_new claude <path>` opens a persistent coding session\n"
            "• plain messages continue the active session\n"
            "• `/task <instruction>` runs a one-off task\n\n"
            "Use `/help` for the full command set."
        )
    
    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return

        await update.message.reply_text(
            "Telegram Coding Gateway\n\n"
            "Sessions:\n"
            "• `/session_new <backend> <path>` open a session in a bounded repo path\n"
            "• `/session_list` list recent sessions\n"
            "• `/session_use <session_id>` switch the active session\n"
            "• `/session_status [session_id]` inspect session state\n"
            "• `/session_dirs [path]` list child directories for the active session or a path\n"
            "• `/session_cancel [session_id]` cancel the last queued or running task for a session\n"
            "• `/session_close [session_id]` close a session\n\n"
            "Execution:\n"
            "• plain text continues the active session\n"
            "• `/say <instruction>` same as plain text but session-only\n"
            "• `/run <instruction>` route to the active session or create a one-off task\n"
            "• `/task <instruction>` create a one-off task only\n"
            "• `/progress <task_id>` show recent task events\n"
            "• `/cancel <task_id>` cancel a task by id\n"
            "• `/status` show gateway status and configured scope\n\n"
            "Git:\n"
            "• `/git_status`\n"
            "• `/commit <task_id> [--no-branch] [--push]`\n"
            "• `/commit_all <task_id> [--no-branch] [--push]`\n\n"
            "Path handling:\n"
            "• relative paths resolve under your configured base workspace\n"
            "• invalid paths return close matches and nearby directories\n"
            "• successful `/session_new` replies include top directories in that repo"
        )
    
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
                "Example: /task Review the authentication code"
            )
            return

        try:
            await self._queue_instruction(
                update,
                " ".join(context.args),
                active_session=None,
                session_only=False,
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to create task: {e}")
            logger.error(f"Telegram task creation failed: {e}")
    
    async def _handle_status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
            
        try:
            status = self.orchestrator.get_status()

            telegram = status.get("telegram", {})
            scope = status.get("scope", {})
            active_session = self.session_store.get_active(update.effective_chat.id)
            if active_session and not self._user_can_access_session(update.effective_user.id, active_session):
                active_session = None
            lines = [
                "Gateway Status",
                "",
                "Components:",
                f"• Claude Code CLI: {'✅ Available' if status['components']['claude_available'] else '❌ Not available'}",
                f"• LLAMA/Ollama: {'✅ Available' if status['components']['llama_available'] else '❌ Not available'}",
                f"• File Watcher: {'✅ Running' if status['components']['file_watcher_running'] else '❌ Stopped'}",
                f"• Telegram Bot: {'✅ Running' if telegram.get('running') else ('⚠️ Configured but stopped' if telegram.get('configured') else '❌ Not configured')}",
                "",
                "Tasks:",
                f"• Active: {status['tasks']['active']}",
                f"• Queued: {status['tasks']['queued']}",
                f"• Completed: {status['tasks']['completed']}",
                f"• Workers: {status['tasks']['workers']}",
                "",
                "Scope:",
                f"• Base CWD: `{scope.get('base_cwd') or 'unset'}`",
                f"• Allowed root: `{scope.get('allowed_root') or 'unset'}`",
            ]
            if scope.get("root_dirs"):
                lines.append("• Root directories: " + ", ".join(f"`{item}`" for item in scope["root_dirs"][:8]))
            if active_session:
                lines.extend(["", "Active session:", self._format_session_overview(active_session)])
            await update.message.reply_text("\n".join(lines))

        except Exception as e:
            await update.message.reply_text(f"❌ Failed to get status: {e}")
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
        chat_id = update.effective_chat.id

        # Skip very short messages
        if len(message_text) < 10:
            await update.message.reply_text(
                "Please provide a more detailed description of what you'd like me to do."
            )
            return

        try:
            active_session = self.session_store.get_active(chat_id)
            await self._queue_instruction(update, message_text, active_session, session_only=False)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to create task: {e}")
            logger.error(f"message handler failed: {e}")

    async def _handle_run_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Route instruction to active session or one-off task."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /run <instruction>")
            return
        active_session = self.session_store.get_active(update.effective_chat.id)
        await self._queue_instruction(update, " ".join(context.args), active_session, session_only=False)

    async def _handle_say_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Route instruction to the active session only."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /say <instruction>")
            return
        active_session = self.session_store.get_active(update.effective_chat.id)
        await self._queue_instruction(update, " ".join(context.args), active_session, session_only=True)

    # ------------------------------------------------------------------
    # Session commands
    # ------------------------------------------------------------------

    async def _handle_session_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_new <backend> <path>"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /session_new <backend> <path>\n"
                "Example: /session_new claude myrepo"
            )
            return
        backend, repo_path = args[0].lower(), " ".join(args[1:])
        if backend not in ("claude", "codex"):
            await update.message.reply_text("❌ Backend must be 'claude' or 'codex'.")
            return
        resolution = self._path_resolver().resolve_session_path(repo_path)
        if not resolution.ok or not resolution.resolved_path:
            await update.message.reply_text(self._format_path_resolution_error(resolution))
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        session = self.session_store.create(
            backend=backend,
            repo_path=resolution.resolved_path,
            telegram_chat_id=chat_id,
            owner_user_id=user_id,
        )
        self.session_store.bind(chat_id, session.session_id)
        lines = [
            "✅ Session created and set as active.",
            f"ID: `{session.session_id}`",
            f"Backend: {backend}",
            f"Path: `{session.repo_path}`",
        ]
        if resolution.available_dirs:
            lines.append("Top directories: " + ", ".join(f"`{item}`" for item in resolution.available_dirs[:8]))
        await update.message.reply_text("\n".join(lines))

    async def _handle_session_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_list"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        sessions = [s for s in self.session_store.list_all() if self._user_can_access_session(update.effective_user.id, s)]
        if not sessions:
            await update.message.reply_text("No sessions found.")
            return
        active = self.session_store.get_active(update.effective_chat.id)
        active_id = active.session_id if active else None
        lines = []
        for s in sessions[:10]:
            marker = " <-- active" if s.session_id == active_id else ""
            lines.append(f"`{s.session_id}` [{s.backend}] {s.status.value} — {s.repo_path}{marker}")
        await update.message.reply_text("Sessions:\n" + "\n".join(lines))

    async def _handle_session_use(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_use <session_id>"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /session_use <session_id>")
            return
        session_id = args[0]
        session = self.session_store.get(session_id)
        if not session:
            await update.message.reply_text(f"❌ Session `{session_id}` not found.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await update.message.reply_text("❌ You do not own that session.")
            return
        if session.status == SessionStatus.CLOSED:
            await update.message.reply_text(f"❌ Session `{session_id}` is closed.")
            return
        self.session_store.bind(update.effective_chat.id, session_id)
        await update.message.reply_text(
            f"✅ Active session set to `{session_id}` [{session.backend}] — {session.repo_path}"
        )

    async def _handle_session_dirs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_dirs [path]"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        if args:
            resolution = self._path_resolver().resolve_session_path(" ".join(args))
            if not resolution.ok or not resolution.resolved_path:
                await update.message.reply_text(self._format_path_resolution_error(resolution))
                return
            path = resolution.resolved_path
            dirs = resolution.available_dirs
        else:
            session = self.session_store.get_active(update.effective_chat.id)
            if not session:
                await update.message.reply_text("No active session. Use /session_new, /session_use, or pass a path.")
                return
            if not self._user_can_access_session(update.effective_user.id, session):
                await update.message.reply_text("❌ You do not own the active session.")
                return
            path = session.repo_path
            dirs = self._path_resolver().list_child_directories(path, limit=12)

        if not dirs:
            await update.message.reply_text(f"No child directories found under `{path}`.")
            return
        await update.message.reply_text(
            "Directories under "
            f"`{path}`:\n" + "\n".join(f"• `{item}`" for item in dirs[:12])
        )

    async def _handle_session_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_status [session_id]"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        if args:
            session = self.session_store.get(args[0])
        else:
            session = self.session_store.get_active(update.effective_chat.id)
        if not session:
            await update.message.reply_text("No active session. Use /session_new or /session_use.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await update.message.reply_text("❌ You do not own that session.")
            return
        await update.message.reply_text(self._format_session_overview(session, include_dirs=True))

    async def _handle_session_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_cancel [session_id]"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        session = self.session_store.get(args[0]) if args else self.session_store.get_active(update.effective_chat.id)
        if not session:
            await update.message.reply_text("No session found.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await update.message.reply_text("❌ You do not own that session.")
            return
        if not session.last_task_id:
            await update.message.reply_text("No task is associated with that session yet.")
            return
        ok = bool(self.orchestrator.cancel_task(session.last_task_id))
        if ok:
            session.status = SessionStatus.CANCELLED
            self.session_store.save(session)
            await update.message.reply_text(
                f"Cancellation requested for `{session.last_task_id}` in session `{session.session_id}`."
            )
        else:
            await update.message.reply_text(f"Task `{session.last_task_id}` is not cancellable.")

    async def _handle_session_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_close [session_id]"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        if args:
            session = self.session_store.get(args[0])
        else:
            session = self.session_store.get_active(update.effective_chat.id)
        if not session:
            await update.message.reply_text("No session found.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await update.message.reply_text("❌ You do not own that session.")
            return
        session.status = SessionStatus.CLOSED
        self.session_store.save(session)
        active = self.session_store.get_active(update.effective_chat.id)
        if active and active.session_id == session.session_id:
            self.session_store.unbind(update.effective_chat.id)
        await update.message.reply_text(f"Session `{session.session_id}` closed.")

    async def notify_completion(self, task_id: str, summary: str, success: bool = True, chat_id: Optional[int] = None):
        """Notify of task completion.

        If chat_id is given (session tasks), send only to that chat.
        Otherwise broadcast to allowed_users or notification_chat_id.
        """
        if not self.app or not self.is_running:
            return

        try:
            status_icon = "✅" if success else "❌"
            status_text = "COMPLETED" if success else "FAILED"
            # For session tasks send the raw output directly; for standalone wrap it.
            if chat_id:
                message = f"{status_icon} {summary[:4000]}"
            else:
                message = (
                    f"{status_icon} Task {task_id} {status_text}\n\n"
                    f"{summary[:500]}{'...' if len(summary) > 500 else ''}"
                )

            if chat_id:
                try:
                    await self.app.bot.send_message(chat_id=chat_id, text=message)
                except Exception as e:
                    logger.warning(f"Failed to notify chat {chat_id}: {e}")
            elif self.allowed_users:
                for uid in self.allowed_users:
                    try:
                        await self.app.bot.send_message(chat_id=uid, text=message)
                    except Exception as e:
                        logger.warning(f"Failed to notify user {uid}: {e}")
            else:
                try:
                    from config import config as app_config
                    fallback_chat = getattr(app_config.telegram, "notification_chat_id", None)
                    if fallback_chat:
                        await self.app.bot.send_message(chat_id=fallback_chat, text=message)
                    else:
                        logger.info(f"Task {task_id} completed, no notification target configured")
                except Exception as e:
                    logger.warning(f"Failed to notify fallback chat: {e}")

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
            task_description = f"Task {task_id} changes"
            
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
        """Handle /commit_all command for committing all staged changes"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        
        try:
            # Parse command arguments
            args = context.args if context.args else []
            if len(args) < 1:
                await update.message.reply_text(
                    "❌ Usage: `/commit_all <task_id> [--no-branch] [--push]`\n"
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
            
            task_description = f"Task {task_id} changes"
            
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
            await update.message.reply_text(f"❌ Error processing commit_all command: {e}")
            logger.error(f"Git commit_all command failed: {e}")
    
    async def _handle_git_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /git_status command for showing git repository status"""
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
