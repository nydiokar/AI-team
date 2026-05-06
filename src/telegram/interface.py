"""
Telegram bot interface for the Telegram Coding Gateway.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Dict, Any, Optional
from pathlib import Path

from src.core.process_utils import (
    current_process_create_time,
    pid_exists,
    process_matches_entrypoint,
    terminate_process_tree,
)
from src.core.session_store import SessionStore
from src.core.interfaces import Session, SessionStatus
from src.core.path_resolver import PathResolver, PathResolution

try:
    from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters, ContextTypes
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    # Mock classes for when telegram is not available
    class BotCommand:
        def __init__(self, command: str, description: str):
            self.command = command
            self.description = description

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
        self._lock_path = Path("logs") / "telegram_bot.lock"
        self._lock_acquired = False
        self._app_root = Path(__file__).resolve().parents[2]
        # Rate limiting for task creation
        self._rate_limit_state: Dict[int, list[float]] = {}
        # Per-chat plain-text debounce buffer to merge split Telegram messages
        self._message_buffers: Dict[int, Dict[str, Any]] = {}

        if not self.bot_token:
            logger.info("Telegram bot token not configured. Command interface available without live bot app.")
            return
        
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
        self.app.add_handler(MessageHandler(filters.COMMAND, self._flush_pending_buffer_on_command), group=-1)

        # Command handlers
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("help", self._handle_help))
        self.app.add_handler(CommandHandler("task", self._handle_task_command))
        self.app.add_handler(CommandHandler("status", self._handle_status_command))
        # Session command handlers
        self.app.add_handler(CommandHandler("session_new", self._handle_session_new))
        self.app.add_handler(CommandHandler("session_list", self._handle_session_list))
        self.app.add_handler(CommandHandler("session_use", self._handle_session_use))
        self.app.add_handler(CommandHandler("session_dirs", self._handle_session_dirs))
        self.app.add_handler(CommandHandler("session_status", self._handle_session_status))
        self.app.add_handler(CommandHandler("session_cancel", self._handle_session_cancel))
        self.app.add_handler(CommandHandler("session_close", self._handle_session_close))
        self.app.add_handler(CommandHandler("session_restore", self._handle_session_restore))
        # Git automation command handlers
        self.app.add_handler(CommandHandler("commit", self._handle_git_commit))
        self.app.add_handler(CommandHandler("commit_all", self._handle_git_commit_all))
        self.app.add_handler(CommandHandler("git_status", self._handle_git_status))
        self.app.add_handler(CallbackQueryHandler(self._handle_session_picker_callback, pattern=r"^session_use:"))
        self.app.add_handler(CallbackQueryHandler(self._handle_session_new_callback, pattern=r"^session_new_"))
        self.app.add_handler(CallbackQueryHandler(self._handle_session_restore_callback, pattern=r"^session_restore:"))
        
        # Message handler for natural language task creation
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

    @staticmethod
    def _bot_commands() -> list[BotCommand]:
        """Commands exposed to Telegram clients via the slash-command chooser."""
        entries = [
            ("session_new", "Create a new coding session"),
            ("session_list", "List open sessions"),
            ("session_close", "Close the active session"),
            ("status", "Show gateway status"),
            ("start", "Show welcome text"),
            ("help", "Show command help"),
            ("task", "Create a one-off task"),
            ("session_use", "Switch the active session"),
            ("session_dirs", "List allowed session roots"),
            ("session_status", "Show active session details"),
            ("session_cancel", "Cancel the active session task"),
            ("session_restore", "Restore a recently closed session"),
            ("commit", "Commit safe session changes"),
            ("commit_all", "Commit all staged session changes"),
            ("git_status", "Show repository git status"),
        ]
        return [BotCommand(command, description) for command, description in entries]

    async def start(self):
        """Start the Telegram bot"""
        if not self.app or self.is_running:
            return
            
        try:
            self._acquire_instance_lock()
            await self.app.initialize()
            await self.app.bot.set_my_commands(self._bot_commands())
            await self.app.start()
            await self.app.updater.start_polling()
            self.is_running = True
            logger.info("Telegram bot started successfully")
        except Exception as e:
            self._release_instance_lock()
            logger.error(f"Failed to start Telegram bot: {e}")
            raise
    
    async def stop(self):
        """Stop the Telegram bot"""
        await self._drop_all_pending_buffers()
        if not self.app or not self.is_running:
            self._release_instance_lock()
            return
            
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            self.is_running = False
            self._release_instance_lock()
            logger.info("Telegram bot stopped")
        except Exception as e:
            self._release_instance_lock()
            logger.error(f"Error stopping Telegram bot: {e}")

    async def _flush_pending_buffer_on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ensure pending plain-text intent is submitted before command handling."""
        if not update.effective_chat:
            return
        await self._flush_buffer(update.effective_chat.id)

    async def _drop_all_pending_buffers(self) -> None:
        for chat_id in list(self._message_buffers.keys()):
            entry = self._message_buffers.pop(chat_id, None)
            if not entry:
                continue
            task = entry.get("task")
            if task and not task.done():
                task.cancel()

    async def _buffer_message(self, update: Update, message_text: str) -> None:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        message_text = (message_text or "").strip()
        if not message_text:
            return

        try:
            from config import config as app_config
            debounce_sec = float(getattr(app_config.system, "telegram_message_buffer_sec", 3.0))
        except Exception:
            debounce_sec = 3.0

        entry = self._message_buffers.get(chat_id)
        if entry is None:
            entry = {
                "parts": [],
                "user_id": user_id,
                "chat_id": chat_id,
                "task": None,
            }
            self._message_buffers[chat_id] = entry

        parts = entry["parts"]
        if not parts or parts[-1] != message_text:
            parts.append(message_text)

        task = entry.get("task")
        if task and not task.done():
            task.cancel()

        if debounce_sec <= 0:
            await self._flush_buffer(chat_id)
            return

        entry["task"] = asyncio.create_task(self._debounced_flush(chat_id, debounce_sec))

    async def _debounced_flush(self, chat_id: int, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
            await self._flush_buffer(chat_id)
        except asyncio.CancelledError:
            return

    async def _flush_buffer(self, chat_id: int) -> None:
        entry = self._message_buffers.pop(chat_id, None)
        if not entry:
            return

        task = entry.get("task")
        current = asyncio.current_task()
        if task and task is not current and not task.done():
            task.cancel()

        parts = [str(part).strip() for part in entry.get("parts", []) if str(part).strip()]
        if not parts:
            return

        combined = "\n".join(parts)
        await self._submit_buffered_instruction(
            chat_id=entry["chat_id"],
            user_id=entry["user_id"],
            message_text=combined,
            session_only=False,
        )

    async def _submit_buffered_instruction(
        self,
        *,
        chat_id: int,
        user_id: int,
        message_text: str,
        session_only: bool = False,
    ) -> None:
        if not self.app:
            return

        message_text = (message_text or "").strip()
        if len(message_text) < 3:
            await self.app.bot.send_message(chat_id=chat_id, text="❌ Message is too short.")
            return

        active_session = self.session_store.get_active(chat_id)
        if active_session:
            if not self._user_can_access_session(user_id, active_session):
                await self.app.bot.send_message(chat_id=chat_id, text="❌ You do not own the active session.")
                return
            active_session.last_user_message = message_text
            active_session.status = SessionStatus.BUSY
            task_id = await self.orchestrator.submit_instruction(
                description=message_text,
                session_id=active_session.session_id,
                cwd=active_session.repo_path,
                source="telegram_session",
            )
            active_session.last_task_id = task_id
            self.session_store.save(active_session)
            await self.app.bot.send_message(chat_id=chat_id, text="⏳ Working...")
            logger.info(
                "user=%s chat=%s task=%s session=%s",
                user_id,
                chat_id,
                task_id,
                active_session.session_id,
            )
            return

        if session_only:
            await self.app.bot.send_message(chat_id=chat_id, text="❌ No active session. Use /session_new first.")
            return

        task_id = await self.orchestrator.submit_instruction(
            description=message_text,
            source="telegram_oneoff",
        )
        await self.app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"One-off task created: `{task_id}`\n"
                f"Tip: use /session_new to open a persistent coding session."
            ),
            parse_mode="Markdown",
        )
        logger.info(
            "user=%s chat=%s task=%s session=none",
            user_id,
            chat_id,
            task_id,
        )

    def _acquire_instance_lock(self) -> None:
        """Prevent multiple local polling instances from using the same bot token."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()

        existing = self._read_lock()
        if existing:
            existing_pid = int(existing.get("pid") or 0)
            existing_started = float(existing.get("create_time") or 0)
            if existing_pid and process_matches_entrypoint(
                existing_pid,
                started=existing_started,
                app_root=self._app_root,
                entrypoint=self._app_root / "main.py",
            ):
                logger.warning(f"Existing Telegram poller detected (pid={existing_pid}); terminating it before restart")
                terminate_process_tree(existing_pid)
            elif existing_pid and pid_exists(existing_pid):
                raise RuntimeError(
                    f"Telegram bot lock is already held by PID {existing_pid}. "
                    "Stop the other gateway instance before starting a new one."
                )
            self._lock_path.unlink(missing_ok=True)

        payload = {
            "pid": pid,
            "create_time": current_process_create_time(),
            "root": str(self._app_root),
        }
        self._lock_path.write_text(json.dumps(payload), encoding="utf-8")
        self._lock_acquired = True

    def _release_instance_lock(self) -> None:
        if not self._lock_acquired:
            return
        try:
            current = self._read_lock()
            if current and int(current.get("pid") or 0) == os.getpid():
                self._lock_path.unlink(missing_ok=True)
        except Exception:
            pass
        self._lock_acquired = False

    def _read_lock(self) -> dict | None:
        if not self._lock_path.exists():
            return None
        try:
            raw = self._lock_path.read_text(encoding="utf-8").strip()
            if not raw:
                return None
            if raw.startswith("{"):
                data = json.loads(raw)
                return data if isinstance(data, dict) else None
            return {"pid": int(raw)}
        except Exception:
            return None

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
            dirs = self._path_resolver().list_child_directories(session.repo_path, limit=8, include_hidden=False)
            if dirs:
                lines.append("Directories: " + ", ".join(f"`{item}`" for item in dirs))
        return "\n".join(lines)

    @staticmethod
    def _split_message(text: str, limit: int = 4096) -> list[str]:
        """Split text into chunks that fit within Telegram's character limit."""
        if len(text) <= limit:
            return [text]
        chunks = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            split_at = text.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = limit
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks

    async def _send_long_message(self, chat_id: int, text: str) -> None:
        """Send a message, splitting it into multiple parts if it exceeds Telegram's limit."""
        for chunk in self._split_message(text):
            await self.app.bot.send_message(chat_id=chat_id, text=chunk)

    @staticmethod
    def _session_repo_name(session: Session) -> str:
        return Path(session.repo_path).name or session.repo_path

    @staticmethod
    def _format_session_timestamp(value: str) -> str:
        if not value:
            return "unknown"
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value[:16]

    @staticmethod
    def _session_status_label(status: SessionStatus) -> str:
        labels = {
            SessionStatus.IDLE: "🟢 idle",
            SessionStatus.BUSY: "🔵 busy",
            SessionStatus.AWAITING_INPUT: "🟡 waiting for input",
            SessionStatus.ERROR: "🔴 needs attention",
            SessionStatus.CANCELLED: "⚪ cancelled",
            SessionStatus.CLOSED: "⚫ closed",
        }
        return labels.get(status, status.value)

    @staticmethod
    def _compact_session_note(session: Session, limit: int = 80) -> str:
        note = (session.last_user_message or session.last_result_summary or "").strip()
        note = " ".join(note.split())
        if len(note) > limit:
            return note[: limit - 1].rstrip() + "..."
        return note

    def _format_session_list_item(self, session: Session, active_id: Optional[str]) -> str:
        active = session.session_id == active_id
        prefix = "⭐ ACTIVE" if active else "💬 open"
        backend_icon = "🤖" if session.backend == "codex" else "🧠"
        title = f"{prefix}  {backend_icon} {session.backend} / {self._session_repo_name(session)}"
        details = (
            f"  🆔 `{session.session_id}`  •  "
            f"{self._session_status_label(session.status)} | "
            f"🕒 {self._format_session_timestamp(session.updated_at)}"
        )
        note = self._compact_session_note(session)
        if note:
            return "\n".join([title, details, f"  📝 {note}"])
        return "\n".join([title, details])

    def _format_closed_session_list_item(self, session: Session) -> str:
        backend_icon = "🤖" if session.backend == "codex" else "🧠"
        lines = [
            f"↩️ {backend_icon} {session.backend} / {self._session_repo_name(session)}",
            f"  🆔 `{session.session_id}`  •  ⚫ closed | 🕒 {self._format_session_timestamp(session.updated_at)}",
        ]
        note = self._compact_session_note(session)
        if note:
            lines.append(f"  📝 {note}")
        return "\n".join(lines)

    def _format_session_switched_message(self, session: Session) -> str:
        backend_icon = "🤖" if session.backend == "codex" else "🧠"
        lines = [
            "⭐ Active session switched",
            "",
            f"{backend_icon} {session.backend} / {self._session_repo_name(session)}",
            f"🆔 `{session.session_id}`",
            f"{self._session_status_label(session.status)}  •  🕒 {self._format_session_timestamp(session.updated_at)}",
        ]
        note = self._compact_session_note(session)
        if note:
            lines.append(f"📝 {note}")
        return "\n".join(lines)

    def _build_session_picker_markup(self, sessions: list[Session], active_id: Optional[str]) -> Optional["InlineKeyboardMarkup"]:
        if not TELEGRAM_AVAILABLE or not sessions:
            return None
        rows = []
        for session in sessions[:10]:
            name = self._session_repo_name(session)
            icon = "🤖" if session.backend == "codex" else "🧠"
            label = f"{icon} {session.backend}: {name}"
            if session.session_id == active_id:
                label = f"⭐ {label}"
            rows.append([
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"session_use:{session.session_id}",
                )
            ])
        return InlineKeyboardMarkup(rows)

    def _build_closed_session_picker_markup(self, sessions: list[Session]) -> Optional["InlineKeyboardMarkup"]:
        """Inline keyboard for restoring closed sessions."""
        if not TELEGRAM_AVAILABLE or not sessions:
            return None
        rows = []
        for session in sessions[:5]:
            name = self._session_repo_name(session)
            updated = self._format_session_timestamp(session.updated_at)[:10]
            icon = "🤖" if session.backend == "codex" else "🧠"
            label = f"↩️ {icon} {session.backend}: {name} ({updated})"
            rows.append([
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"session_restore:{session.session_id}",
                )
            ])
        return InlineKeyboardMarkup(rows)

    def _build_session_backend_markup(self) -> Optional["InlineKeyboardMarkup"]:
        if not TELEGRAM_AVAILABLE:
            return None
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(text="Codex", callback_data="session_new_backend:codex")],
                [InlineKeyboardButton(text="Claude", callback_data="session_new_backend:claude")],
            ]
        )

    def _recent_session_repo_choices(self, limit: int = 10) -> list[tuple[str, str]]:
        resolver = self._path_resolver()
        root = resolver.base_cwd or resolver.allowed_root
        if not root:
            return []

        try:
            root_path = Path(root).resolve()
            children = [child for child in root_path.iterdir() if child.is_dir() and not child.name.startswith(".")]
            children.sort(key=lambda child: child.stat().st_mtime, reverse=True)
        except Exception:
            return []

        repos = [child for child in children if (child / ".git").exists()]
        if len(repos) < limit:
            seen = {item.resolve() for item in repos}
            for child in children:
                resolved = child.resolve()
                if resolved in seen:
                    continue
                repos.append(child)
                seen.add(resolved)
                if len(repos) >= limit:
                    break

        return [(child.name, str(child.resolve())) for child in repos[:limit]]

    def _build_session_repo_markup(self, backend: str) -> Optional["InlineKeyboardMarkup"]:
        if not TELEGRAM_AVAILABLE:
            return None
        choices = self._recent_session_repo_choices(limit=10)
        if not choices:
            return None
        rows = []
        for idx, (name, _repo_path) in enumerate(choices):
            rows.append(
                [
                    InlineKeyboardButton(
                        text=name[:64],
                        callback_data=f"session_new_repo:{backend}:{idx}",
                    )
                ]
            )
        return InlineKeyboardMarkup(rows)

    async def _create_and_bind_session(
        self,
        *,
        chat_id: int,
        user_id: int,
        backend: str,
        repo_path: str,
    ) -> Session:
        session = self.session_store.create(
            backend=backend,
            repo_path=repo_path,
            telegram_chat_id=chat_id,
            owner_user_id=user_id,
        )
        self.session_store.bind(chat_id, session.session_id)
        return session

    def _get_accessible_session(
        self,
        update: Update,
        session_id: Optional[str] = None,
        require_active: bool = True,
    ) -> tuple[Optional[Session], Optional[str]]:
        if session_id:
            session = self.session_store.get(session_id)
            if not session:
                return None, f"❌ Session `{session_id}` not found."
        else:
            session = self.session_store.get_active(update.effective_chat.id)
            if not session:
                if require_active:
                    return None, "❌ No active session. Use /session_new or /session_use."
                return None, None

        if not self._user_can_access_session(update.effective_user.id, session):
            return None, "❌ You do not own that session."
        if session.status == SessionStatus.CLOSED:
            return None, f"❌ Session `{session.session_id}` is closed."
        return session, None

    @staticmethod
    def _split_git_args(args: list[str]) -> tuple[Optional[str], bool, bool]:
        session_id = None
        create_branch = True
        push_branch = False
        for arg in args:
            if arg == "--no-branch":
                create_branch = False
            elif arg == "--push":
                push_branch = True
            elif session_id is None and re.fullmatch(r"[0-9a-f]{12}", arg):
                session_id = arg
        return session_id, create_branch, push_branch

    def _build_git_commit_context(self, session: Session) -> tuple[str, str]:
        commit_key = session.last_task_id or f"session_{session.session_id}"
        description = (session.last_user_message or "").strip()
        if not description:
            description = f"Session {session.session_id} changes"
        return commit_key, description

    def _format_git_result(self, header: str, result: Dict[str, Any], push_branch: bool = False) -> str:
        if not result.get("success"):
            lines = [header]
            for error in result.get("errors", []):
                lines.append(f"• {error}")
            return "\n".join(lines)

        lines = [header.replace("❌ Failed to", "✅")]
        branch_name = result.get("branch_name")
        if branch_name:
            lines.append(f"Branch: `{branch_name}`")
        files_committed = result.get("files_committed") or []
        if files_committed:
            lines.append(f"Files committed: {len(files_committed)}")
            for file_path in files_committed[:5]:
                lines.append(f"• `{file_path}`")
            if len(files_committed) > 5:
                lines.append(f"• ... and {len(files_committed) - 5} more")
        blocked = result.get("sensitive_files_blocked") or []
        if blocked:
            lines.append(f"Sensitive files blocked: {len(blocked)}")
        if push_branch and branch_name:
            lines.append("Branch pushed to remote.")
        return "\n".join(lines)

    @staticmethod
    def _git_usage(command: str) -> str:
        return (
            f"Usage: /{command} [session_id] [--no-branch] [--push]\n"
            "If omitted, `session_id` defaults to the active session."
        )

    def _resolve_task_scope(
        self,
        update: Update,
        args: list[str],
        require_task: bool = True,
    ) -> tuple[Optional[str], Optional[Session], Optional[str]]:
        session = None
        task_id = None

        if args:
            candidate = str(args[0]).strip()
            if re.fullmatch(r"[0-9a-f]{12}", candidate):
                session, error = self._get_accessible_session(update, session_id=candidate)
                if error:
                    return None, None, error
                task_id = session.last_task_id or None
            else:
                task_id = candidate
        else:
            session, error = self._get_accessible_session(update, require_active=False)
            if error:
                return None, None, error
            if session is not None:
                task_id = session.last_task_id or None

        if require_task and not task_id:
            if session is not None:
                return None, session, f"❌ Session `{session.session_id}` has no active or recent task."
            return None, None, "❌ No active session task found. Use `/session_cancel`, `/session_status`, or pass a task ID explicitly."

        return task_id, session, None

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
            task_id = await self.orchestrator.submit_instruction(
                description=message_text,
                session_id=active_session.session_id,
                cwd=active_session.repo_path,
                source="telegram_session",
            )
            active_session.last_task_id = task_id
            self.session_store.save(active_session)
            await update.message.reply_text("⏳ Working...")
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

        task_id = await self.orchestrator.submit_instruction(
            description=message_text,
            source="telegram_oneoff",
        )
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
            "• `/task <instruction>` runs a one-off task outside any session\n"
            "• `/session_dirs` shows likely project folders when you need to browse\n\n"
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
            "• `/session_list` list open sessions\n"
            "• `/session_use <session_id>` switch the active session\n"
            "• `/session_status [session_id]` inspect session state\n"
            "• `/session_dirs [path]` list useful child directories for the active session or a path\n"
            "• `/session_cancel [session_id]` cancel the last queued or running task for a session\n"
            "• `/session_close [session_id]` close a session\n"
            "• `/session_restore [session_id]` restore a closed session\n\n"
            "Execution:\n"
            "• plain text continues the active session\n"
            "• `/task <instruction>` create a one-off task only\n"
            "• `/status` show gateway status and configured scope\n\n"
            "Git:\n"
            "• `/git_status [session_id]`\n"
            "• `/commit [session_id] [--no-branch] [--push]`\n"
            "• `/commit_all [session_id] [--no-branch] [--push]`\n\n"
            "Path handling:\n"
            "• relative paths resolve under your configured base workspace\n"
            "• invalid paths return close matches and nearby directories\n"
            "• `/session_dirs` without an active session shows likely project folders under the workspace"
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
                f"• Ollama helpers: {'✅ Available' if status['components']['llama_available'] else '➖ Optional / disabled'}",
                f"• External task watcher: {'✅ Running' if status['components']['file_watcher_running'] else '❌ Stopped'}",
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
        """Compatibility progress view for the active session or an explicit task."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        task_id, session, error = self._resolve_task_scope(update, context.args or [])
        if error:
            await update.message.reply_text(error)
            return
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
                label = f"session `{session.session_id}`" if session else f"task `{task_id}`"
                await update.message.reply_text(f"No recent events for {label}.")
                return
            lines = [self._format_progress_line(ev) for ev in list(buf)[-10:]]
            header_target = f"session `{session.session_id}` / task `{task_id}`" if session else f"task `{task_id}`"
            header = f"📈 Progress for {header_target} (last {len(lines)} events)"
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
        backend = str(ev.get("backend") or "").strip().lower()
        pretty = name
        icon = "•"
        details = ""
        if name == "task_received":
            icon = "📥"
            src = ev.get("file")
            details = f"from {Path(src).name}" if src else ""
        elif name == "parsed":
            icon = "🧩"
        elif name.endswith("_started"):
            icon = "🚀"
            worker = ev.get("worker")
            if not backend and "_" in name:
                backend = name.split("_", 1)[0]
            details = f"{backend} on {worker}" if worker and backend else (f"worker {worker}" if worker else backend)
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
        elif name.endswith("_finished"):
            icon = "🏁"
            status = ev.get("status")
            dur = ev.get("duration_s")
            if not backend and "_" in name:
                backend = name.split("_", 1)[0]
            summary = f"{status} in {dur:.2f}s" if isinstance(dur, (int, float)) else f"{status}"
            details = f"{backend} {summary}".strip() if backend else summary
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
            "codex_started": "started",
            "summarized": "summarized",
            "validated": "validated",
            "retry": "retry",
            "timeout": "timeout",
            "claude_finished": "finished",
            "codex_finished": "finished",
            "artifacts_written": "artifacts",
            "artifacts_error": "artifacts error",
            "task_archived": "archived",
        }
        pretty = pretty_map.get(name, name)
        tail = f" — {details}" if details else ""
        return f"{tshort} {icon} {pretty}{tail}"
    
    async def _handle_cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Compatibility cancellation path for the active session or an explicit task."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return

        task_id, session, error = self._resolve_task_scope(update, context.args or [])
        if error:
            await update.message.reply_text(error)
            return

        try:
            ok = False
            try:
                ok = bool(self.orchestrator.cancel_task(task_id))
            except Exception:
                ok = False
            if ok:
                response = (
                    f"🔄 Cancellation requested for session `{session.session_id}` task `{task_id}`."
                    if session else
                    f"🔄 Cancellation requested for task `{task_id}`."
                )
                await update.message.reply_text(response)
                logger.info(f"Telegram user requested cancellation of task {task_id}")
            else:
                response = (
                    f"❌ Task `{task_id}` from session `{session.session_id}` is not cancellable."
                    if session else
                    f"❌ Task `{task_id}` not found or already finished."
                )
                await update.message.reply_text(response)

        except Exception as e:
            error_msg = f"❌ Failed to cancel task: {str(e)}"
            await update.message.reply_text(error_msg)
            logger.error(f"Telegram task cancellation failed: {e}")
    
    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle natural language messages as task creation requests"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
            
        chat_id = update.effective_chat.id
        is_new_buffer = chat_id not in self._message_buffers

        # Check rate limiting only once per buffered intent.
        if is_new_buffer and not self._check_rate_limit(update.effective_user.id):
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

        # Skip very short messages
        if len(message_text) < 10:
            await update.message.reply_text(
                "Please provide a more detailed description of what you'd like me to do."
            )
            return

        try:
            await self._buffer_message(update, message_text)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to create task: {e}")
            logger.error(f"message handler failed: {e}")

    async def _handle_run_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Compatibility alias for the older task-runner UX."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        if not context.args:
            await update.message.reply_text("Use a plain message for the active session, or `/task <instruction>` for a one-off task.")
            return
        active_session = self.session_store.get_active(update.effective_chat.id)
        await update.message.reply_text("`/run` is kept for compatibility. Plain messages are the primary session flow.")
        await self._queue_instruction(update, " ".join(context.args), active_session, session_only=False)

    async def _handle_say_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Compatibility alias for the older task-runner UX."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        if not context.args:
            await update.message.reply_text("Use a plain message for the active session.")
            return
        active_session = self.session_store.get_active(update.effective_chat.id)
        await update.message.reply_text("`/say` is kept for compatibility. Plain messages are the primary session flow.")
        await self._queue_instruction(update, " ".join(context.args), active_session, session_only=True)

    # ------------------------------------------------------------------
    # Session commands
    # ------------------------------------------------------------------

    async def _handle_session_new_legacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        session = await self._create_and_bind_session(
            chat_id=chat_id,
            user_id=user_id,
            backend=backend,
            repo_path=resolution.resolved_path,
        )
        lines = [
            "✅ Session created and set as active.",
            f"ID: `{session.session_id}`",
            f"Backend: {backend}",
            f"Path: `{session.repo_path}`",
            "Send a plain message to continue in this session.",
            "Use `/session_dirs` to browse directories under this repo.",
        ]
        await update.message.reply_text("\n".join(lines))

    async def _handle_session_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_list — shows open sessions with a switch picker, then recently closed with restore buttons."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return

        all_sessions = [s for s in self.session_store.list_all() if self._user_can_access_session(update.effective_user.id, s)]
        open_sessions = [s for s in all_sessions if s.status != SessionStatus.CLOSED]

        active = self.session_store.get_active(update.effective_chat.id)
        active_id = active.session_id if active else None

        # --- Open sessions block ---
        if open_sessions:
            lines = [self._format_session_list_item(s, active_id) for s in open_sessions[:10]]
            if len(open_sessions) > 10:
                lines.append(f"...and {len(open_sessions) - 10} more. Use /session_status <session_id> for details.")
            await update.message.reply_text(
                f"Open sessions ({len(open_sessions)}) - tap to switch:\n\n" + "\n\n".join(lines),
                reply_markup=self._build_session_picker_markup(open_sessions, active_id),
            )
        else:
            await update.message.reply_text("No open sessions. Use /session_new to create one.")

    async def _handle_session_use_legacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_use <session_id>"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        if not args:
            markup = self._build_session_backend_markup()
            if markup is None:
                await update.message.reply_text("âŒ Telegram inline buttons are unavailable.")
                return
            await update.message.reply_text(
                "Choose the backend for the new session:",
                reply_markup=markup,
            )
            return
        if not args:
            sessions = [
                s for s in self.session_store.list_all()
                if s.status != SessionStatus.CLOSED and self._user_can_access_session(update.effective_user.id, s)
            ]
            if not sessions:
                await update.message.reply_text("No open sessions found.")
                return
            active = self.session_store.get_active(update.effective_chat.id)
            active_id = active.session_id if active else None
            await update.message.reply_text(
                "Choose the session to make active:",
                reply_markup=self._build_session_picker_markup(sessions, active_id),
            )
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
        await update.message.reply_text(self._format_session_switched_message(session))

    async def _handle_session_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_new <backend> <path>"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("âŒ Access denied.")
            return
        args = context.args or []
        if not args:
            markup = self._build_session_backend_markup()
            if markup is None:
                await update.message.reply_text("âŒ Telegram inline buttons are unavailable.")
                return
            await update.message.reply_text(
                "Choose the backend for the new session:",
                reply_markup=markup,
            )
            return
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /session_new <backend> <path>\n"
                "Example: /session_new claude myrepo"
            )
            return
        backend, repo_path = args[0].lower(), " ".join(args[1:])
        if backend not in ("claude", "codex"):
            await update.message.reply_text("âŒ Backend must be 'claude' or 'codex'.")
            return
        resolution = self._path_resolver().resolve_session_path(repo_path)
        if not resolution.ok or not resolution.resolved_path:
            await update.message.reply_text(self._format_path_resolution_error(resolution))
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        session = await self._create_and_bind_session(
            chat_id=chat_id,
            user_id=user_id,
            backend=backend,
            repo_path=resolution.resolved_path,
        )
        lines = [
            "âœ… Session created and set as active.",
            f"ID: `{session.session_id}`",
            f"Backend: {backend}",
            f"Path: `{session.repo_path}`",
            "Send a plain message to continue in this session.",
            "Use `/session_dirs` to browse directories under this repo.",
        ]
        await update.message.reply_text("\n".join(lines))

    async def _handle_session_use(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_use <session_id>"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("âŒ Access denied.")
            return
        args = context.args or []
        if not args:
            sessions = [
                s for s in self.session_store.list_all()
                if s.status != SessionStatus.CLOSED and self._user_can_access_session(update.effective_user.id, s)
            ]
            if not sessions:
                await update.message.reply_text("No open sessions found.")
                return
            active = self.session_store.get_active(update.effective_chat.id)
            active_id = active.session_id if active else None
            await update.message.reply_text(
                "Choose the session to make active:",
                reply_markup=self._build_session_picker_markup(sessions, active_id),
            )
            return
        session_id = args[0]
        session = self.session_store.get(session_id)
        if not session:
            await update.message.reply_text(f"âŒ Session `{session_id}` not found.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await update.message.reply_text("âŒ You do not own that session.")
            return
        if session.status == SessionStatus.CLOSED:
            await update.message.reply_text(f"âŒ Session `{session_id}` is closed.")
            return
        self.session_store.bind(update.effective_chat.id, session_id)
        await update.message.reply_text(self._format_session_switched_message(session))

    async def _handle_session_picker_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        await query.answer()

        if not self._check_user_permission(update.effective_user.id):
            await query.edit_message_text("âŒ Access denied.")
            return

        data = query.data or ""
        session_id = data.split(":", 1)[1] if ":" in data else ""
        session = self.session_store.get(session_id)
        if not session:
            await query.edit_message_text(f"âŒ Session `{session_id}` not found.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await query.edit_message_text("âŒ You do not own that session.")
            return
        if session.status == SessionStatus.CLOSED:
            await query.edit_message_text(f"âŒ Session `{session_id}` is closed.")
            return

        self.session_store.bind(update.effective_chat.id, session_id)
        await query.edit_message_text(self._format_session_switched_message(session))

    async def _handle_session_new_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        await query.answer()

        if not self._check_user_permission(update.effective_user.id):
            await query.edit_message_text("âŒ Access denied.")
            return

        data = query.data or ""
        if data.startswith("session_new_backend:"):
            backend = data.split(":", 1)[1].strip().lower()
            if backend not in ("claude", "codex"):
                await query.edit_message_text("âŒ Unknown backend.")
                return
            markup = self._build_session_repo_markup(backend)
            if markup is None:
                await query.edit_message_text(
                    "âŒ No recent repositories found. Use `/session_new <backend> <path>`.",
                    parse_mode="Markdown",
                )
                return
            await query.edit_message_text(
                f"Choose the repository for `{backend}`:",
                reply_markup=markup,
                parse_mode="Markdown",
            )
            return

        if data.startswith("session_new_repo:"):
            parts = data.split(":")
            if len(parts) != 3:
                await query.edit_message_text("âŒ Invalid repository selection.")
                return
            backend = parts[1].strip().lower()
            try:
                repo_index = int(parts[2])
            except ValueError:
                await query.edit_message_text("âŒ Invalid repository selection.")
                return
            if backend not in ("claude", "codex"):
                await query.edit_message_text("âŒ Unknown backend.")
                return
            choices = self._recent_session_repo_choices(limit=10)
            if repo_index < 0 or repo_index >= len(choices):
                await query.edit_message_text("âŒ Repository choice expired. Run /session_new again.")
                return
            _label, repo_path = choices[repo_index]
            session = await self._create_and_bind_session(
                chat_id=update.effective_chat.id,
                user_id=update.effective_user.id,
                backend=backend,
                repo_path=repo_path,
            )
            await query.edit_message_text(
                "\n".join(
                    [
                        "âœ… Session created and set as active.",
                        f"ID: `{session.session_id}`",
                        f"Backend: {backend}",
                        f"Path: `{session.repo_path}`",
                        "Send a plain message to continue in this session.",
                    ]
                ),
                parse_mode="Markdown",
            )
            return

        await query.edit_message_text("âŒ Unknown session_new action.")

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
            dirs = self._path_resolver().list_child_directories(path, limit=12, include_hidden=False, sort_by_recent=True)
        else:
            session = self.session_store.get_active(update.effective_chat.id)
            if session and self._user_can_access_session(update.effective_user.id, session):
                path = session.repo_path
                dirs = self._path_resolver().list_child_directories(path, limit=12, include_hidden=False, sort_by_recent=True)
            else:
                resolver = self._path_resolver()
                path = str(resolver.base_cwd or resolver.allowed_root or "")
                dirs = resolver.list_child_directories(path, limit=12, include_hidden=False, sort_by_recent=True) if path else []
                if not path:
                    await update.message.reply_text("No active session and no workspace root configured.")
                    return

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

    async def _handle_session_restore(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_restore [session_id] — reopen a closed session and make it active."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        if args:
            session = self.session_store.get(args[0])
            if not session:
                await update.message.reply_text(f"❌ Session `{args[0]}` not found.")
                return
            if not self._user_can_access_session(update.effective_user.id, session):
                await update.message.reply_text("❌ You do not own that session.")
                return
            if session.status != SessionStatus.CLOSED:
                await update.message.reply_text(f"Session `{session.session_id}` is already open ({session.status.value}).")
                return
            session.status = SessionStatus.IDLE
            self.session_store.save(session)
            self.session_store.bind(update.effective_chat.id, session.session_id)
            await update.message.reply_text(
                f"✅ Session `{session.session_id}` restored and set as active.\n"
                f"Backend: {session.backend} — {session.repo_path}"
            )
        else:
            all_sessions = [s for s in self.session_store.list_all() if self._user_can_access_session(update.effective_user.id, s)]
            closed = [s for s in all_sessions if s.status == SessionStatus.CLOSED]
            if not closed:
                await update.message.reply_text("No closed sessions to restore.")
                return
            recent = closed[:5]
            lines = [self._format_closed_session_list_item(s) for s in recent]
            await update.message.reply_text(
                "Recently closed sessions - tap to restore:\n\n" + "\n\n".join(lines),
                reply_markup=self._build_closed_session_picker_markup(recent),
            )

    async def _handle_session_restore_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Callback for restore buttons in the closed-session picker."""
        query = update.callback_query
        if not query:
            return
        await query.answer()

        if not self._check_user_permission(update.effective_user.id):
            await query.edit_message_text("❌ Access denied.")
            return

        data = query.data or ""
        session_id = data.split(":", 1)[1] if ":" in data else ""
        session = self.session_store.get(session_id)
        if not session:
            await query.edit_message_text(f"❌ Session `{session_id}` not found.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await query.edit_message_text("❌ You do not own that session.")
            return
        if session.status != SessionStatus.CLOSED:
            await query.edit_message_text(
                f"Session `{session_id}` is already open ({session.status.value}). Use /session_use to switch."
            )
            return

        session.status = SessionStatus.IDLE
        self.session_store.save(session)
        self.session_store.bind(update.effective_chat.id, session.session_id)
        await query.edit_message_text(
            f"✅ Session `{session.session_id}` restored and set as active.\n"
            f"Backend: {session.backend} — {session.repo_path}"
        )

    async def notify_completion(self, task_id: str, summary: str, success: bool = True, chat_id: Optional[int] = None):
        """Notify of task completion.

        If chat_id is given (session tasks), send only to that chat.
        Otherwise broadcast to allowed_users or notification_chat_id.
        """
        if not self.app or not self.is_running:
            return

        try:
            # Session tasks: send Claude's output directly, no task-runner framing.
            # Standalone tasks: wrap with status header.
            if chat_id:
                message = summary if success else f"❌ {summary}"
            else:
                status_icon = "✅" if success else "❌"
                status_text = "completed" if success else "failed"
                message = f"{status_icon} Task {task_id} {status_text}\n\n{summary}"

            if chat_id:
                try:
                    await self._send_long_message(chat_id=chat_id, text=message)
                except Exception as e:
                    logger.warning(f"Failed to notify chat {chat_id}: {e}")
            elif self.allowed_users:
                for uid in self.allowed_users:
                    try:
                        await self._send_long_message(chat_id=uid, text=message)
                    except Exception as e:
                        logger.warning(f"Failed to notify user {uid}: {e}")
            else:
                try:
                    from config import config as app_config
                    fallback_chat = getattr(app_config.telegram, "notification_chat_id", None)
                    if fallback_chat:
                        await self._send_long_message(chat_id=fallback_chat, text=message)
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
        """Handle /commit command for committing active-session changes."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return

        try:
            session_id, create_branch, push_branch = self._split_git_args(context.args or [])
            session, error = self._get_accessible_session(update, session_id=session_id)
            if error:
                if not context.args:
                    error = f"{error}\n{self._git_usage('commit')}"
                await update.message.reply_text(error)
                return

            from src.core.git_automation import GitAutomationService

            git_service = GitAutomationService(session.repo_path)
            commit_key, task_description = self._build_git_commit_context(session)
            result = git_service.safe_commit_task(
                task_id=commit_key,
                task_description=task_description,
                create_branch=create_branch,
                push_branch=push_branch,
            )
            await update.message.reply_text(
                self._format_git_result(
                    f"❌ Failed to commit changes in session `{session.session_id}`.",
                    result,
                    push_branch=push_branch,
                )
            )

        except Exception as e:
            await update.message.reply_text(f"❌ Error processing commit command: {e}")
            logger.error(f"Git commit command failed: {e}")
    
    async def _handle_git_commit_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /commit_all command for committing staged changes in a session repo."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return

        try:
            session_id, create_branch, push_branch = self._split_git_args(context.args or [])
            session, error = self._get_accessible_session(update, session_id=session_id)
            if error:
                if not context.args:
                    error = f"{error}\n{self._git_usage('commit_all')}"
                await update.message.reply_text(error)
                return

            from src.core.git_automation import GitAutomationService

            git_service = GitAutomationService(session.repo_path)
            commit_key, task_description = self._build_git_commit_context(session)
            result = git_service.commit_all_staged(
                task_id=commit_key,
                task_description=task_description,
                create_branch=create_branch,
                push_branch=push_branch,
            )
            await update.message.reply_text(
                self._format_git_result(
                    f"❌ Failed to commit staged changes in session `{session.session_id}`.",
                    result,
                    push_branch=push_branch,
                )
            )

        except Exception as e:
            await update.message.reply_text(f"❌ Error processing commit_all command: {e}")
            logger.error(f"Git commit_all command failed: {e}")
    
    async def _handle_git_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /git_status command for the active or specified session repo."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return

        try:
            args = context.args or []
            session_id = args[0] if args and re.fullmatch(r"[0-9a-f]{12}", args[0]) else None
            session, error = self._get_accessible_session(update, session_id=session_id)
            if error:
                if not args:
                    error = f"{error}\nUsage: /git_status [session_id]"
                await update.message.reply_text(error)
                return

            from src.core.git_automation import GitAutomationService

            git_service = GitAutomationService(session.repo_path)
            status = git_service.get_git_status_summary()

            if "error" in status:
                await update.message.reply_text(f"❌ {status['error']}")
                return

            changes = status["changes"]
            message_parts = [
                "Git Repository Status",
                f"Session: `{session.session_id}`",
                f"Path: `{session.repo_path}`",
                f"Branch: `{status['current_branch']}`",
                f"Working directory: {'✅ Clean' if status['working_directory_clean'] else '⚠️ Has changes'}",
            ]

            if not status["working_directory_clean"]:
                message_parts.append("")
                message_parts.append("Changes:")
                message_parts.append(f"• Modified: {len(changes['modified'])}")
                message_parts.append(f"• Created: {len(changes['created'])}")
                message_parts.append(f"• Deleted: {len(changes['deleted'])}")
                message_parts.append(f"• Total: {changes['total']}")

                if status["staged_files"]:
                    message_parts.append("")
                    message_parts.append(f"Staged files: {len(status['staged_files'])}")
                    for file_path in status["staged_files"][:3]:
                        message_parts.append(f"• `{file_path}`")
                    if len(status["staged_files"]) > 3:
                        message_parts.append(f"• ... and {len(status['staged_files']) - 3} more")

                if status["unstaged_files"]:
                    message_parts.append("")
                    message_parts.append(f"Unstaged files: {len(status['unstaged_files'])}")
                    for file_path in status["unstaged_files"][:3]:
                        message_parts.append(f"• `{file_path}`")
                    if len(status["unstaged_files"]) > 3:
                        message_parts.append(f"• ... and {len(status['unstaged_files']) - 3} more")

                safety = status["safety"]
                if safety["has_sensitive_files"]:
                    message_parts.append("")
                    message_parts.append(f"Sensitive files detected: {len(safety['sensitive_files'])}")
                    for file_path in safety["sensitive_files"][:3]:
                        message_parts.append(f"• `{file_path}`")
                    if len(safety["sensitive_files"]) > 3:
                        message_parts.append(f"• ... and {len(safety['sensitive_files']) - 3} more")

            await update.message.reply_text("\n".join(message_parts))

        except Exception as e:
            await update.message.reply_text(f"❌ Error getting git status: {e}")
            logger.error(f"Git status command failed: {e}")
