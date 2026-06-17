"""
Telegram bot interface for the Telegram Coding Gateway.
"""
import asyncio
import json
import logging
import os
import re
import socket
import time
import uuid
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
        DEFAULT_TYPE = Context

logger = logging.getLogger(__name__)

_DANGEROUS_EXTENSIONS: set[str] = {
    ".exe", ".bat", ".cmd", ".com", ".msi", ".msp", ".scr", ".pif", ".cpl",
    ".vbs", ".vbe", ".ps1", ".psm1", ".psd1", ".wsf", ".wsh", ".hta",
    ".jar", ".dll", ".reg", ".lnk", ".gadget", ".application",
}

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
        self.app.add_handler(CommandHandler("nodes", self._handle_nodes_command))
        self.app.add_handler(CommandHandler("node", self._handle_node_detail_command))
        # Session command handlers
        self.app.add_handler(CommandHandler("session_new", self._handle_session_new))
        self.app.add_handler(CommandHandler("session_list", self._handle_session_list))
        self.app.add_handler(CommandHandler("session_closed", self._handle_session_closed))
        self.app.add_handler(CommandHandler("session_use", self._handle_session_use))
        self.app.add_handler(CommandHandler("session_dirs", self._handle_session_dirs))
        self.app.add_handler(CommandHandler("session_status", self._handle_session_status))
        self.app.add_handler(CommandHandler("session_cancel", self._handle_session_cancel))
        self.app.add_handler(CommandHandler("session_close", self._handle_session_close))
        self.app.add_handler(CommandHandler("session_restore", self._handle_session_restore))
        self.app.add_handler(CommandHandler("compact", self._handle_compact))
        # Git automation command handlers
        self.app.add_handler(CommandHandler("commit", self._handle_git_commit))
        self.app.add_handler(CommandHandler("commit_all", self._handle_git_commit_all))
        self.app.add_handler(CommandHandler("git_status", self._handle_git_status))
        self.app.add_handler(CommandHandler("jobs", self._handle_jobs_command))
        self.app.add_handler(CallbackQueryHandler(self._handle_session_picker_callback, pattern=r"^session_use:"))
        self.app.add_handler(CallbackQueryHandler(self._handle_session_new_callback, pattern=r"^session_new_"))
        self.app.add_handler(CallbackQueryHandler(self._handle_session_restore_callback, pattern=r"^session_restore:"))
        
        # Message handler for natural language task creation
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

        # Document / photo upload handler
        self.app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, self._handle_document))

        # Global error handler: without one, a failed reply (e.g. a Markdown
        # parse error) is logged but the user sees nothing — a button "flickers"
        # and the menu never goes away. This surfaces the failure to the user.
        self.app.add_error_handler(self._on_error)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Last-resort handler so UI failures never silently strand the user."""
        err = getattr(context, "error", None)
        logger.error(f"Telegram handler error: {err!r}", exc_info=err)
        try:
            # Acknowledge any callback query so the button stops spinning.
            if isinstance(update, Update) and update.callback_query:
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
                await update.callback_query.edit_message_text(
                    "⚠️ Something went wrong handling that. Please try again."
                )
            elif isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong handling that. Please try again."
                )
        except Exception as e:
            logger.error(f"error handler failed to notify user: {e}")

    @staticmethod
    def _bot_commands() -> list[BotCommand]:
        """Commands exposed to Telegram clients via the slash-command chooser."""
        entries = [
            ("session_new", "🆕 Start a new coding session"),
            ("session_list", "💬 Open sessions (tap to switch)"),
            ("session_close", "✖️ Close the active session"),
            ("status", "📊 Gateway health dashboard"),
            ("start", "👋 Welcome / quick start"),
            ("help", "❓ Full command help"),
            ("task", "⚡ Run a one-off task"),
            ("session_use", "🔀 Switch the active session"),
            ("session_dirs", "📂 List project folders"),
            ("session_status", "🔎 Active session details"),
            ("session_cancel", "🛑 Cancel the running task"),
            ("session_restore", "↩️ Restore a closed session"),
            ("session_closed", "💤 Browse & restore closed sessions"),
            ("compact", "🗜 Compact the session context"),
            ("nodes", "🌐 Worker nodes (mesh)"),
            ("node", "🌐 One node's detail"),
            ("commit", "💾 Commit safe session changes"),
            ("commit_all", "💾 Commit all session changes"),
            ("git_status", "🔧 Repository git status"),
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
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ Working... {self._session_message_ref(active_session, task_id)}",
            )
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

    _STALE_LOCK_AGE_SECS = 30

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
                lock_age = time.time() - self._lock_path.stat().st_mtime
                if lock_age < self._STALE_LOCK_AGE_SECS:
                    raise RuntimeError(
                        f"Telegram bot lock is already held by PID {existing_pid}. "
                        "Stop the other gateway instance before starting a new one."
                    )
                logger.warning(
                    f"Telegram bot lock (age={lock_age:.0f}s) points to PID {existing_pid} which is not the gateway "
                    "(PID recycled after crash/reboot). Reclaiming lock."
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

    async def _inspect(self, session: Session, op: str, params: Optional[dict] = None) -> dict:
        """Run a repo inspection op against the node that owns `session`.

        Returns the op result dict (which may contain an ``error`` key). Routing
        (local vs. owning remote node) and the offline-node honesty guard live in
        NodeInspector — the gateway is canonical about *where* the repo is.
        """
        from src.control.node_inspector import get_inspector, InspectError
        try:
            return await get_inspector().run(session, op, params or {})
        except InspectError as e:
            return {"error": str(e)}

    def _format_session_overview(self, session: Session, dirs: Optional[list] = None) -> str:
        lines = [
            f"Session: {self._session_tag(session.session_id)}",
            f"Backend: {session.backend}  |  Status: {session.status.value}",
            f"Path: `{session.repo_path}`",
            f"Machine: {session.machine_id}",
            f"Updated: {session.updated_at}",
        ]
        if session.backend_session_id:
            lines.append(f"Backend session ID: `{session.backend_session_id}`")
        if session.last_task_id:
            lines.append(f"Last task: `{session.last_task_id}`")
        lines.append(f"Ref: {self._session_message_ref(session)}")
        lines.append(f"Summary: {self._format_session_material_summary(session)}")
        # `dirs` is fetched by the caller via NodeInspector so it reflects the
        # owning node's filesystem, not the gateway host's.
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
        """Send a message, splitting into chunks if needed.

        Tries Markdown first (for bold/code formatting). If Telegram rejects
        the parse (e.g. unclosed backtick from agent output), retries as plain
        text so the message always arrives.
        """
        for chunk in self._split_message(text):
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id, text=chunk, parse_mode="Markdown"
                )
            except Exception:
                # Strip any partial markdown and send as plain text
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
    def _mesh_node_column_enabled() -> bool:
        """Show the node column only when mesh is on and workers exist (D4)."""
        try:
            from config import config as app_config
            if not getattr(app_config.mesh, "enabled", False):
                return False
        except Exception:
            return False
        try:
            from src.control.db import get_db
            db = get_db()
            return bool(db and db.list_nodes())
        except Exception:
            return False

    @staticmethod
    def _session_node_label(session: Session) -> str:
        """Friendly node label for a session: its machine_id, or 'this server'."""
        import socket
        mid = (session.machine_id or "").strip()
        if not mid or mid == socket.gethostname():
            return "this server"
        return mid

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
        note = (session.last_result_summary or session.last_summary or session.last_user_message or "").strip()
        note = " ".join(note.split())
        if len(note) > limit:
            return note[: limit - 1].rstrip() + "..."
        return note

    @staticmethod
    def _session_message_ref(session: Session, task_id: Optional[str] = None) -> str:
        task = task_id or session.last_task_id
        if task:
            return f"#s_{session.session_id} #t_{task}"
        return f"#s_{session.session_id}"

    @staticmethod
    def _safe_upload_name(file_name: str | None, fallback: str) -> str:
        name = (file_name or fallback).strip()
        name = name.replace("\\", "_").replace("/", "_")
        name = re.sub(r'[<>:"|?*]', "_", name)
        name = re.sub(r'_+', "_", name)
        if len(name) > 200:
            stem, ext = os.path.splitext(name)
            ext = ext[:20]
            name = stem[: 200 - len(ext) - 1] + ext
        return name.strip("._ ")

    @staticmethod
    def _is_dangerous_extension(file_name: str) -> bool:
        _, ext = os.path.splitext(file_name)
        return ext.lower() in _DANGEROUS_EXTENSIONS

    def _format_session_material_summary(self, session: Session, limit: int = 220) -> str:
        parts = []
        result = " ".join((session.last_result_summary or session.last_summary or "").split())
        request = " ".join((session.last_user_message or "").split())
        if result:
            parts.append(f"Last result: {result}")
        if request and request != result:
            parts.append(f"Last request: {request}")
        if session.last_files_modified:
            files = ", ".join(session.last_files_modified[:3])
            more = f" +{len(session.last_files_modified) - 3} more" if len(session.last_files_modified) > 3 else ""
            parts.append(f"Files: {files}{more}")
        recent = []
        for item in reversed(session.task_history or []):
            item_result = " ".join(str(item.get("result_summary", "")).split())
            if item_result and item_result != result and item_result not in recent:
                recent.append(item_result)
            if len(recent) >= 2:
                break
        if recent:
            parts.append("Recent: " + " / ".join(recent))
        if not parts:
            return "No completed work recorded yet."
        text = " | ".join(parts)
        if len(text) > limit:
            return text[: limit - 1].rstrip() + "..."
        return text

    # ------------------------------------------------------------------
    # Shared visual vocabulary — one consistent look across every command.
    # ------------------------------------------------------------------
    _BACKEND_ICONS = {
        "claude": "🧠",
        "codex": "🤖",
        "opencode": "🛠",
        "opencode-server": "🛰",
    }
    _STATUS_DOT = {
        SessionStatus.IDLE: "🟢",
        SessionStatus.BUSY: "🔵",
        SessionStatus.AWAITING_INPUT: "🟡",
        SessionStatus.ERROR: "🔴",
        SessionStatus.CANCELLED: "⚪",
        SessionStatus.CLOSED: "⚫",
    }
    _STATUS_WORD = {
        SessionStatus.IDLE: "idle",
        SessionStatus.BUSY: "working",
        SessionStatus.AWAITING_INPUT: "awaiting input",
        SessionStatus.ERROR: "needs attention",
        SessionStatus.CANCELLED: "cancelled",
        SessionStatus.CLOSED: "closed",
    }

    @classmethod
    def _backend_icon(cls, backend: str) -> str:
        return cls._BACKEND_ICONS.get(backend, "💠")

    @classmethod
    def _status_chip(cls, status: SessionStatus) -> str:
        """Compact 'dot word' chip, e.g. '🟡 awaiting input'."""
        return f"{cls._STATUS_DOT.get(status, '•')} {cls._STATUS_WORD.get(status, status.value)}"

    @staticmethod
    def _short_id(session_id: str) -> str:
        """First 8 chars — enough to recognise, short enough to scan."""
        return (session_id or "")[:8]

    @staticmethod
    def _session_tag(session_id: str) -> str:
        """Tappable Telegram hashtag for a session, e.g. '#s_69927c233d34'.

        Matches the `#s_<id>` tag emitted on agent responses
        (`_session_message_ref`) so tapping it in Telegram surfaces the whole
        thread for that session. Uses the FULL id — truncating would break the
        search link.
        """
        return f"#s_{session_id}" if session_id else ""

    @staticmethod
    def _relative_age(value: str) -> str:
        """Human 'time ago' from an ISO timestamp: '4m ago', '2h ago', 'just now'."""
        if not value:
            return "unknown"
        try:
            dt = datetime.fromisoformat(value)
            secs = (datetime.now() - dt).total_seconds()
            if secs < 0:
                secs = 0
            if secs < 10:
                return "just now"
            if secs < 90:
                return f"{int(secs)}s ago"
            if secs < 5400:
                return f"{int(secs // 60)}m ago"
            if secs < 172800:
                return f"{int(secs // 3600)}h ago"
            return f"{int(secs // 86400)}d ago"
        except Exception:
            return value

    def _session_one_liner(self, session: Session, active_id: Optional[str], show_node: bool) -> str:
        """A scannable multi-line entry with a short summary tail. Used in lists.

        Rendered as PLAIN TEXT — repo names and agent summaries contain `_`,
        `*`, and stray backticks that break Telegram Markdown parsing, so these
        messages must never be sent with parse_mode.
        """
        star = "⭐ " if session.session_id == active_id else "• "
        icon = self._backend_icon(session.backend)
        node = f" · {self._session_node_label(session)}" if show_node else ""
        head = f"{star}{self._session_tag(session.session_id)} · {icon} {session.backend}{node}"
        meta = (
            f"   {self._session_repo_name(session)} · "
            f"{self._status_chip(session.status)} · {self._relative_age(session.updated_at)}"
        )
        note = self._compact_session_note(session, limit=70)
        if note:
            return f"{head}\n{meta}\n   ↳ {note}"
        return f"{head}\n{meta}"

    def _session_card(self, session: Session, *, header: str = "📍 Active session", show_node: bool = True) -> str:
        """Rich card for one session (PLAIN TEXT — see _session_one_liner)."""
        icon = self._backend_icon(session.backend)
        node = f" · {self._session_node_label(session)}" if show_node else ""
        lines = [
            header,
            f"{icon} {session.backend}{node} · {self._status_chip(session.status)}",
            f"📂 {self._session_repo_name(session)}   🆔 {self._session_tag(session.session_id)}",
            f"🕒 last activity {self._relative_age(session.updated_at)}",
        ]
        note = self._compact_session_note(session, limit=160)
        if note:
            lines.append(f"↳ {note}")
        return "\n".join(lines)

    def _find_session_for_task(self, task_id: str, chat_id: Optional[int] = None) -> Optional[Session]:
        if not task_id:
            return None
        for session in self.session_store.list_all():
            if chat_id is not None and session.telegram_chat_id != chat_id:
                continue
            if session.last_task_id == task_id:
                return session
            for item in session.task_history or []:
                if str(item.get("task_id", "")) == task_id:
                    return session
        return None

    def _format_session_list_item(self, session: Session, active_id: Optional[str]) -> str:
        active = session.session_id == active_id
        prefix = "⭐ ACTIVE" if active else "💬 open"
        backend_icon = "🤖" if session.backend == "codex" else "🧠"
        title = f"{prefix}  {backend_icon} {session.backend} / {self._session_repo_name(session)}"
        details = (
            f"  🆔 {self._session_tag(session.session_id)}  •  "
            f"{self._session_status_label(session.status)} | "
            f"🕒 {self._format_session_timestamp(session.updated_at)}"
        )
        note = self._compact_session_note(session)
        if note:
            return "\n".join([title, details, f"  Summary: {note}"])
        return "\n".join([title, details])

    def _compact_session_line(self, session: Session, active_id: Optional[str], show_node: bool) -> str:
        """One-line session summary for /session_list (D4).

        Open:   ⭐ `id` — backend — [node —] status — repo
        Closed: [closed] `id` — backend — [node —] repo
        """
        sid = self._session_tag(session.session_id)
        node = f"{self._session_node_label(session)} — " if show_node else ""
        if session.status == SessionStatus.CLOSED:
            return f"[closed] {sid} — {session.backend} — {node}{self._session_repo_name(session)}"
        star = "⭐ " if session.session_id == active_id else ""
        return (
            f"{star}{sid} — {session.backend} — {node}"
            f"{session.status.value} — {self._session_repo_name(session)}"
        )

    def _format_closed_session_list_item(self, session: Session) -> str:
        backend_icon = "🤖" if session.backend == "codex" else "🧠"
        lines = [
            f"↩️ {backend_icon} {session.backend} / {self._session_repo_name(session)}",
            f"  🆔 {self._session_tag(session.session_id)}  •  ⚫ closed | 🕒 {self._format_session_timestamp(session.updated_at)}",
        ]
        note = self._compact_session_note(session)
        if note:
            lines.append(f"  Summary: {note}")
        return "\n".join(lines)

    def _format_session_switched_message(self, session: Session) -> str:
        show_node = self._mesh_node_column_enabled()
        return (
            self._session_card(session, header="⭐ Switched to this session", show_node=show_node)
            + "\n\n💬 Just type to continue here."
        )

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
                [InlineKeyboardButton(text="🧠 Claude", callback_data="session_new_backend:claude")],
                [InlineKeyboardButton(text="🤖 Codex", callback_data="session_new_backend:codex")],
                [InlineKeyboardButton(text="🛰 OpenCode (server)", callback_data="session_new_backend:opencode-server")],
                [InlineKeyboardButton(text="🛠 OpenCode (CLI)", callback_data="session_new_backend:opencode")],
                [InlineKeyboardButton(text="✖️ Cancel", callback_data="session_new_cancel:")],
            ]
        )

    def _mesh_online_nodes(self):
        """Return online nodes from the shared DB (works across processes)."""
        try:
            import json as _json
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return []
            rows = db.list_nodes(status="online")
            nodes = []
            for r in rows:
                nodes.append(type("_Node", (), {
                    "node_id": r["node_id"],
                    "tailscale_ip": r.get("tailscale_ip", ""),
                    "status": r.get("status", "online"),
                    "capabilities": type("_Caps", (), {
                        "projects_root": r.get("projects_root", ""),
                        "repos": _json.loads(r.get("repos") or "[]"),
                    })(),
                })())
            return nodes
        except Exception:
            return []

    def _build_session_node_markup(self, backend: str) -> Optional["InlineKeyboardMarkup"]:
        """Node picker: server (local) + any online remote workers."""
        if not TELEGRAM_AVAILABLE:
            return None
        import socket
        rows = [
            [InlineKeyboardButton(
                text=f"🖥 This server ({socket.gethostname()})",
                callback_data=f"session_new_node:{backend}:__local__",
            )]
        ]
        for node in self._mesh_online_nodes():
            label = f"🌐 {node.node_id} ({node.tailscale_ip})"
            rows.append([InlineKeyboardButton(
                text=label[:64],
                callback_data=f"session_new_node:{backend}:{node.node_id}",
            )])
        rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="session_new_back:backend")])
        return InlineKeyboardMarkup(rows)

    def _repo_choices_for_node(self, node_id: str, limit: int = 10) -> list[tuple[str, str]]:
        """Return [(name, path)] for the given node. __local__ uses server's own filesystem."""
        if node_id == "__local__":
            return self._local_repo_choices(limit=limit)
        try:
            import json as _json
            from src.control.db import get_db
            db = get_db()
            if db is None:
                return []
            row = db.get_node(node_id)
            if row:
                repos = _json.loads(row.get("repos") or "[]")
                return [(r["name"], r["path"]) for r in repos[:limit]]
        except Exception:
            pass
        return []

    def _local_repo_choices(self, limit: int = 10) -> list[tuple[str, str]]:
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

    def _build_session_repo_markup(
        self, backend: str, node_id: str = "__local__", back_to: str = "backend"
    ) -> Optional["InlineKeyboardMarkup"]:
        """Repo picker. Always includes a Back button; `back_to` is 'backend' or 'node'.

        Returns None only when Telegram is unavailable — an empty repo list still
        yields a keyboard (just Back) so the user is never stranded.
        """
        if not TELEGRAM_AVAILABLE:
            return None
        choices = self._repo_choices_for_node(node_id, limit=10)
        rows = []
        for idx, (name, _repo_path) in enumerate(choices):
            rows.append([
                InlineKeyboardButton(
                    text=f"📁 {name}"[:64],
                    callback_data=f"session_new_repo:{backend}:{node_id}:{idx}",
                )
            ])
        if back_to == "node":
            rows.append([InlineKeyboardButton(
                text="⬅️ Back", callback_data=f"session_new_back:node:{backend}")])
        else:
            rows.append([InlineKeyboardButton(
                text="⬅️ Back", callback_data="session_new_back:backend")])
        return InlineKeyboardMarkup(rows)

    async def _create_and_bind_session(
        self,
        *,
        chat_id: int,
        user_id: int,
        backend: str,
        repo_path: str,
        node_id: str = "__local__",
    ) -> Session:
        import socket
        session = self.session_store.create(
            backend=backend,
            repo_path=repo_path,
            telegram_chat_id=chat_id,
            owner_user_id=user_id,
        )
        # Pin to remote node; __local__ means this server (keep hostname set by session_store.create)
        if node_id and node_id != "__local__":
            session.machine_id = node_id
            self.session_store.save(session)
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
                return None, f"❌ Session {self._session_tag(session_id)} not found."
        else:
            session = self.session_store.get_active(update.effective_chat.id)
            if not session:
                if require_active:
                    return None, "❌ No active session. Use /session_new or /session_use."
                return None, None

        if not self._user_can_access_session(update.effective_user.id, session):
            return None, "❌ You do not own that session."
        if session.status == SessionStatus.CLOSED:
            return None, f"❌ Session {self._session_tag(session.session_id)} is closed."
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
                return None, session, f"❌ Session {self._session_tag(session.session_id)} has no active or recent task."
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
            await update.message.reply_text(f"⏳ Working... {self._session_message_ref(active_session, task_id)}")
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
            "👋 *Welcome to your Coding Gateway*\n\n"
            "Drive coding agents (Claude, Codex, OpenCode) from your phone.\n\n"
            "*Quick start*\n"
            "1️⃣ `/session_new` — pick a backend, machine & repo (guided)\n"
            "2️⃣ Just *type your request* — it goes to the agent\n"
            "3️⃣ `/status` anytime to see what's happening\n\n"
            "💡 `/task <instruction>` runs a quick one-off without a session.\n\n"
            "Type `/help` for the full command set.",
            parse_mode="Markdown",
        )
    
    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return

        await update.message.reply_text(
            "🧭 *Telegram Coding Gateway — command guide*\n\n"
            "Once a session is active, *just type normally* — your message goes "
            "straight to the agent. The commands below are for steering.\n\n"
            "💬 *Sessions*\n"
            "• `/session_new` — start one (guided picker, or `/session_new claude <path>`)\n"
            "• `/session_list` — open sessions, tap to switch\n"
            "• `/session_closed` — browse & restore closed ones\n"
            "• `/session_status [id]` — full detail on a session\n"
            "• `/session_use [id]` — switch active session\n"
            "• `/session_close [id]` — close · `/session_restore [id]` — reopen\n"
            "• `/session_cancel [id]` — stop the running task\n"
            "• `/compact [id]` — shrink the agent's context window\n\n"
            "⚡ *Work*\n"
            "• plain text → continues the active session\n"
            "• `/task <instruction>` — one-off task, no session\n\n"
            "📊 *Health & mesh*\n"
            "• `/status` — gateway dashboard + active session\n"
            "• `/nodes` — worker nodes (online/offline, last seen)\n"
            "• `/node <id>` — one node's backends, repos, heartbeat\n\n"
            "💾 *Git*\n"
            "• `/git_status [id]`\n"
            "• `/commit [id] [--no-branch] [--push]`\n"
            "• `/commit_all [id] [--no-branch] [--push]`\n\n"
            "📂 *Paths*\n"
            "• relative paths resolve under your workspace; bad paths suggest matches\n"
            "• `/session_dirs [path]` — browse project folders",
            parse_mode="Markdown",
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
            show_node = self._mesh_node_column_enabled()

            telegram = status.get("telegram", {})
            comps = status.get("components", {})
            active_session = self.session_store.get_active(update.effective_chat.id)
            if active_session and not self._user_can_access_session(update.effective_user.id, active_session):
                active_session = None

            # Count open sessions this user can see, for the headline.
            try:
                open_count = sum(
                    1 for s in self.session_store.list_all()
                    if s.status != SessionStatus.CLOSED
                    and self._user_can_access_session(update.effective_user.id, s)
                )
            except Exception:
                open_count = 0

            running = bool(status.get("running") or telegram.get("running"))
            workers = status["tasks"]["workers"]
            active_tasks = status["tasks"]["active"]
            queued = status["tasks"]["queued"]

            # --- Headline: one glanceable health line ---
            degraded = (not comps.get("claude_available")) or (
                telegram.get("configured") and not telegram.get("running")
            )
            head_icon = "✅" if running and not degraded else ("⚠️" if running else "🔴")
            head_word = "healthy" if running and not degraded else ("running" if running else "stopped")
            bits = [f"{workers} worker{'s' if workers != 1 else ''}"]
            bits.append(f"{open_count} session{'s' if open_count != 1 else ''}")
            if active_tasks:
                bits.append(f"{active_tasks} running")
            if queued:
                bits.append(f"{queued} queued")
            lines = [f"{head_icon} Gateway {head_word} · " + " · ".join(bits)]

            # --- Active session card (the thing you came to see) ---
            lines.append("")
            if active_session:
                lines.append(self._session_card(active_session, show_node=show_node))
            else:
                lines.append("💤 No active session.")
                lines.append("   /session_new to start · /session_list to switch")

            # --- Components: always shown, compact one line ---
            def _mark(ok, optional=False):
                if ok:
                    return "✅"
                return "➖" if optional else "❌"
            lines.append("")
            lines.append(
                f"⚙️ Claude {_mark(comps.get('claude_available'))}"
                f" · Watcher {_mark(comps.get('file_watcher_running'))}"
                f" · Bot {_mark(telegram.get('running'))}"
                f" · Ollama {_mark(comps.get('llama_available'), optional=True)}"
            )

            # --- Mesh line, only when enabled ---
            if show_node:
                nodes = self._mesh_online_nodes()
                if nodes:
                    names = " · ".join(f"{n.node_id} 🟢" for n in nodes[:4])
                    extra = f" +{len(nodes) - 4}" if len(nodes) > 4 else ""
                    lines.append(f"🌐 Mesh: {names}{extra}")
                else:
                    lines.append("🌐 Mesh: on, no workers online")

            await update.message.reply_text("\n".join(lines))

        except Exception as e:
            await update.message.reply_text(f"❌ Failed to get status: {e}")
            logger.error(f"Telegram status request failed: {e}")

    @staticmethod
    def _heartbeat_age(last_heartbeat: str) -> str:
        """Return a compact human age like '12s ago' / '3m ago' / '2h ago'."""
        if not last_heartbeat:
            return "never"
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(last_heartbeat)
            secs = (datetime.now() - dt).total_seconds()
            if secs < 0:
                secs = 0
            if secs < 90:
                return f"{int(secs)}s ago"
            if secs < 5400:
                return f"{int(secs // 60)}m ago"
            if secs < 172800:
                return f"{int(secs // 3600)}h ago"
            return f"{int(secs // 86400)}d ago"
        except Exception:
            return last_heartbeat

    async def _handle_nodes_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all mesh worker nodes (online + offline) with last-heartbeat ages."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        try:
            import json as _json
            import socket
            from src.control.db import get_db
            db = get_db()
            rows = db.list_nodes() if db else []

            online = [r for r in rows if r.get("status") == "online"]
            lines = [f"Nodes ({len(online)} online / {len(rows)} total)", ""]

            # This gateway/server, always shown as the local execution target.
            lines.append(f"• {socket.gethostname()} (this server) — local")

            for r in rows:
                try:
                    backends = ",".join(_json.loads(r.get("backends") or "[]")) or "—"
                except Exception:
                    backends = "—"
                dot = "🟢" if r.get("status") == "online" else "⚪"
                age = self._heartbeat_age(r.get("last_heartbeat", ""))
                ip = r.get("tailscale_ip") or "—"
                lines.append(
                    f"{dot} {r['node_id']} — {backends} — {ip} — last seen {age}"
                )

            if not rows:
                lines.append("_(no remote workers registered)_")
            lines.append("")
            lines.append("Use /node <id> for detail.")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to list nodes: {e}")
            logger.error(f"Telegram /nodes failed: {e}")

    async def _handle_node_detail_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detail for one node: /node <node_id>."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /node <node_id>  (see /nodes)")
            return
        node_id = args[0].strip()
        try:
            import json as _json
            from src.control.db import get_db
            db = get_db()
            row = db.get_node(node_id) if db else None
            if not row:
                await update.message.reply_text(f"❌ Node {node_id!r} not found. See /nodes.")
                return
            try:
                backends = ", ".join(_json.loads(row.get("backends") or "[]")) or "—"
            except Exception:
                backends = "—"
            try:
                repos = [rp.get("name", "?") for rp in _json.loads(row.get("repos") or "[]")]
            except Exception:
                repos = []

            dot = "🟢 online" if row.get("status") == "online" else "⚪ offline"
            lines = [
                f"Node: {row['node_id']}",
                f"• Status: {dot}",
                f"• Tailscale IP: {row.get('tailscale_ip') or '—'}:{row.get('api_port', '')}",
                f"• Backends: {backends}",
                f"• Max concurrent: {row.get('max_concurrent', '?')}",
                f"• Last heartbeat: {self._heartbeat_age(row.get('last_heartbeat', ''))}",
                f"• Registered: {self._heartbeat_age(row.get('registered_at', ''))}",
                f"• Projects root: `{row.get('projects_root') or '—'}`",
            ]
            if repos:
                shown = ", ".join(repos[:15])
                more = f" (+{len(repos) - 15} more)" if len(repos) > 15 else ""
                lines.append(f"• Repos ({len(repos)}): {shown}{more}")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to get node detail: {e}")
            logger.error(f"Telegram /node failed: {e}")

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
                label = f"session {self._session_tag(session.session_id)}" if session else f"task `{task_id}`"
                await update.message.reply_text(f"No recent events for {label}.")
                return
            lines = [self._format_progress_line(ev) for ev in list(buf)[-10:]]
            header_target = f"session {self._session_tag(session.session_id)} / task `{task_id}`" if session else f"task `{task_id}`"
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
                    f"🔄 Cancellation requested for session {self._session_tag(session.session_id)} task `{task_id}`."
                    if session else
                    f"🔄 Cancellation requested for task `{task_id}`."
                )
                await update.message.reply_text(response)
                logger.info(f"Telegram user requested cancellation of task {task_id}")
            else:
                response = (
                    f"❌ Task `{task_id}` from session {self._session_tag(session.session_id)} is not cancellable."
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

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        # Flush any pending text buffer so ordering is preserved
        await self._flush_buffer(chat_id)

        # Active session is required for file uploads
        active_session = self.session_store.get_active(chat_id)
        if not active_session:
            await update.message.reply_text(
                "❌ No active session. Use /session_new to start one, then send the file."
            )
            return
        if not self._user_can_access_session(user_id, active_session):
            await update.message.reply_text("❌ You do not own the active session.")
            return

        # Determine file source
        doc = update.message.document
        photo_arr = update.message.photo
        caption = (update.message.caption or "").strip()

        if doc:
            file_id = doc.file_id
            file_name = doc.file_name
            file_size = doc.file_size or 0
        elif photo_arr:
            largest = photo_arr[-1]
            file_id = largest.file_id
            file_name = None
            file_size = largest.file_size or 0
        else:
            await update.message.reply_text("❌ Unsupported file type.")
            return

        # Check extension blacklist
        raw_name = file_name or f"file_{file_id[:12]}"
        if self._is_dangerous_extension(raw_name):
            await update.message.reply_text(
                f"❌ File extension `{os.path.splitext(raw_name)[1]}` is not allowed for security reasons.",
                parse_mode="Markdown",
            )
            return

        # Check size cap (0 = disabled)
        try:
            from config import config as app_config
            max_mb = app_config.telegram.upload_max_mb
        except Exception:
            max_mb = 0
        if max_mb > 0 and file_size > max_mb * 1024 * 1024:
            await update.message.reply_text(
                f"❌ File exceeds {max_mb} MB limit ({file_size / 1024 / 1024:.1f} MB)."
            )
            return

        # Build destination and download
        fallback_name = f"photo_{file_id[:12]}.jpg" if photo_arr else f"file_{file_id[:12]}"
        safe_name = self._safe_upload_name(file_name, fallback_name)

        # Detect remote session via the canonical registry-based predicate
        # (same rule NodeInspector uses), so uploads and inspection agree on
        # where a session lives — including after the gateway moves to the VPS.
        from src.control.node_inspector import session_node
        is_remote = session_node(active_session) is not None

        file_size_kb = file_size / 1024
        size_str = f"{file_size_kb:.1f} KB" if file_size_kb < 1024 else f"{file_size_kb / 1024:.1f} MB"

        if is_remote:
            # Stage the file on the server; the remote worker will pull it via GET /files/{file_id}
            _staging_root = Path(__file__).resolve().parent.parent.parent / "state" / "uploads"
            stage_id = uuid.uuid4().hex[:16]
            stage_dir = _staging_root / stage_id
            try:
                stage_dir.mkdir(parents=True, exist_ok=True)
                dest = stage_dir / safe_name
                tg_file = await context.bot.get_file(file_id)
                await tg_file.download_to_drive(custom_path=dest)
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to download file: {e}")
                logger.error("file download failed: user=%s chat=%s error=%s", user_id, chat_id, e)
                return

            staged_file_meta = {"file_id": stage_id, "filename": safe_name}
            save_msg = f"📎 Staged `uploads/{safe_name}` ({size_str}) → sending to {active_session.machine_id}"

            if caption:
                full_instruction = f"{caption}\n\n📎 File: `uploads/{safe_name}`"
                active_session.last_user_message = full_instruction
                active_session.status = SessionStatus.BUSY
                try:
                    task_id = await self.orchestrator.submit_instruction(
                        description=full_instruction,
                        session_id=active_session.session_id,
                        cwd=active_session.repo_path,
                        source="telegram_session",
                        extra_metadata={"staged_file": staged_file_meta},
                    )
                except Exception as e:
                    await update.message.reply_text(f"❌ Failed to create task: {e}")
                    logger.error("instruction submission failed: user=%s chat=%s error=%s", user_id, chat_id, e)
                    return
                active_session.last_task_id = task_id
                self.session_store.save(active_session)
                await update.message.reply_text(
                    f"{save_msg}\n⏳ Working on your request... {self._session_message_ref(active_session, task_id)}",
                    parse_mode="Markdown",
                )
                logger.info(
                    "file+instruction user=%s chat=%s file=%s task=%s session=%s node=%s",
                    user_id, chat_id, safe_name, task_id, active_session.session_id, active_session.machine_id,
                )
            else:
                # No instruction — deliver the file now so the user can reference it next
                try:
                    task_id = await self.orchestrator.submit_instruction(
                        description=f"File `uploads/{safe_name}` delivered to session.",
                        session_id=active_session.session_id,
                        cwd=active_session.repo_path,
                        source="telegram_session",
                        extra_metadata={
                            "staged_file": staged_file_meta,
                            "task_type": "fetch_staged_file",
                        },
                    )
                except Exception as e:
                    await update.message.reply_text(f"❌ Failed to deliver file: {e}")
                    logger.error("file delivery task failed: user=%s chat=%s error=%s", user_id, chat_id, e)
                    return
                await update.message.reply_text(
                    f"{save_msg}\nFile is being delivered to {active_session.machine_id} — type an instruction once it arrives.",
                    parse_mode="Markdown",
                )
                logger.info(
                    "file user=%s chat=%s file=%s task=%s session=%s node=%s (no caption)",
                    user_id, chat_id, safe_name, task_id, active_session.session_id, active_session.machine_id,
                )
        else:
            # Local session: save directly into the repo's uploads/ folder
            dest_dir = Path(active_session.repo_path) / "uploads"
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / safe_name
                if dest.exists():
                    stem, ext = os.path.splitext(safe_name)
                    counter = 1
                    while dest.exists():
                        dest = dest_dir / f"{stem}_{counter}{ext}"
                        counter += 1
                    safe_name = dest.name
                tg_file = await context.bot.get_file(file_id)
                await tg_file.download_to_drive(custom_path=dest)
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to download file: {e}")
                logger.error("file download failed: user=%s chat=%s error=%s", user_id, chat_id, e)
                return

            save_msg = f"📎 Saved `uploads/{safe_name}` ({size_str})"

            if caption:
                full_instruction = f"{caption}\n\n📎 File: `uploads/{safe_name}`"
                active_session.last_user_message = full_instruction
                active_session.status = SessionStatus.BUSY
                try:
                    task_id = await self.orchestrator.submit_instruction(
                        description=full_instruction,
                        session_id=active_session.session_id,
                        target_files=[str(dest)],
                        cwd=active_session.repo_path,
                        source="telegram_session",
                    )
                except Exception as e:
                    await update.message.reply_text(f"❌ Failed to create task: {e}")
                    logger.error("instruction submission failed: user=%s chat=%s error=%s", user_id, chat_id, e)
                    return
                active_session.last_task_id = task_id
                self.session_store.save(active_session)
                await update.message.reply_text(
                    f"{save_msg}\n⏳ Working on your request... {self._session_message_ref(active_session, task_id)}",
                    parse_mode="Markdown",
                )
                logger.info(
                    "file+instruction user=%s chat=%s file=%s task=%s session=%s",
                    user_id, chat_id, safe_name, task_id, active_session.session_id,
                )
            else:
                await update.message.reply_text(
                    f"{save_msg}\nIt's in your session repo — type an instruction to work with it.",
                    parse_mode="Markdown",
                )
                logger.info(
                    "file user=%s chat=%s file=%s session=%s (no caption)",
                    user_id, chat_id, safe_name, active_session.session_id,
                )

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
        if backend not in ("claude", "codex", "opencode", "opencode-server"):
            await update.message.reply_text("❌ Backend must be 'claude', 'codex', 'opencode', or 'opencode-server'.")
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
        await update.message.reply_text(
            self._format_session_created_message(session),
        )

    async def _handle_session_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_list — shows open sessions with a switch picker, then recently closed with restore buttons."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return

        all_sessions = [s for s in self.session_store.list_all() if self._user_can_access_session(update.effective_user.id, s)]
        open_sessions = [s for s in all_sessions if s.status != SessionStatus.CLOSED]
        closed_sessions = [s for s in all_sessions if s.status == SessionStatus.CLOSED]

        active = self.session_store.get_active(update.effective_chat.id)
        active_id = active.session_id if active else None
        show_node = self._mesh_node_column_enabled()

        if not open_sessions and not closed_sessions:
            await update.message.reply_text(
                "📭 No sessions yet.\nUse /session_new to start your first coding session."
            )
            return

        if open_sessions:
            lines = [f"💬 Open sessions ({len(open_sessions)}) — tap below to switch", ""]
            for s in open_sessions[:12]:
                lines.append(self._session_one_liner(s, active_id, show_node))
                lines.append("")
            if len(open_sessions) > 12:
                lines.append(f"…and {len(open_sessions) - 12} more open.")
        else:
            lines = ["📭 No open sessions.", "Use /session_new to start one.", ""]

        # Closed sessions stay out of the way — just a count + how to reach them.
        if closed_sessions:
            lines.append("— — —")
            lines.append(
                f"💤 {len(closed_sessions)} closed. /session_closed to browse & restore."
            )

        await update.message.reply_text(
            "\n".join(lines).rstrip(),
            reply_markup=self._build_session_picker_markup(open_sessions, active_id),
        )

    async def _handle_session_closed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_closed — browse recently closed sessions with restore buttons."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        all_sessions = [s for s in self.session_store.list_all() if self._user_can_access_session(update.effective_user.id, s)]
        closed = [s for s in all_sessions if s.status == SessionStatus.CLOSED]
        closed.sort(key=lambda s: s.updated_at or "", reverse=True)
        show_node = self._mesh_node_column_enabled()

        if not closed:
            await update.message.reply_text("✨ No closed sessions. /session_list shows what's open.")
            return

        lines = [f"💤 Closed sessions ({len(closed)}) — tap to restore", ""]
        for s in closed[:10]:
            lines.append(self._session_one_liner(s, None, show_node))
            lines.append("")
        if len(closed) > 10:
            lines.append(f"…and {len(closed) - 10} older. Use /session_restore <id> for those.")

        await update.message.reply_text(
            "\n".join(lines).rstrip(),
            reply_markup=self._build_closed_session_picker_markup(closed[:10]),
        )

    async def _handle_session_use_legacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_use <session_id>"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        if not args:
            markup = self._build_session_backend_markup()
            if markup is None:
                await update.message.reply_text("❌ Telegram inline buttons are unavailable.")
                return
            await update.message.reply_text(
                "🆕 *New session* — choose a backend:",
                reply_markup=markup,
                parse_mode="Markdown",
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
            lines = [self._format_session_list_item(s, active_id) for s in sessions[:10]]
            await update.message.reply_text(
                "Choose the session to make active:\n\n" + "\n\n".join(lines),
                reply_markup=self._build_session_picker_markup(sessions, active_id),
            )
            return
        session_id = args[0]
        session = self.session_store.get(session_id)
        if not session:
            await update.message.reply_text(f"❌ Session {self._session_tag(session_id)} not found.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await update.message.reply_text("❌ You do not own that session.")
            return
        if session.status == SessionStatus.CLOSED:
            await update.message.reply_text(f"❌ Session {self._session_tag(session_id)} is closed.")
            return
        self.session_store.bind(update.effective_chat.id, session_id)
        await update.message.reply_text(self._format_session_switched_message(session))

    async def _handle_session_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_new [<backend> [<node_id>] <path>]"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        if not args:
            markup = self._build_session_backend_markup()
            if markup is None:
                await update.message.reply_text("❌ Telegram inline buttons are unavailable.")
                return
            await update.message.reply_text(
                "🆕 *New session* — choose a backend:",
                reply_markup=markup,
                parse_mode="Markdown",
            )
            return
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /session_new <backend> [<node_id>] <path>\n"
                "Examples:\n"
                "  /session_new claude AI-team\n"
                "  /session_new claude LP-1 AI-team"
            )
            return

        _valid_backends = ("claude", "codex", "opencode", "opencode-server")
        backend = args[0].lower()
        if backend not in _valid_backends:
            await update.message.reply_text("❌ Backend must be 'claude', 'codex', 'opencode', or 'opencode-server'.")
            return

        # Detect optional node_id: if args[1] matches a known online node treat as node
        known_nodes = {n.node_id for n in self._mesh_online_nodes()}
        if len(args) >= 3 and args[1] in known_nodes:
            node_id = args[1]
            repo_path = " ".join(args[2:])
        else:
            node_id = "__local__"
            repo_path = " ".join(args[1:])

        if node_id == "__local__":
            resolution = self._path_resolver().resolve_session_path(repo_path)
            if not resolution.ok or not resolution.resolved_path:
                await update.message.reply_text(self._format_path_resolution_error(resolution))
                return
            resolved_path = resolution.resolved_path
        else:
            from src.control.node_registry import get_registry
            node = get_registry().get(node_id)
            repos = node.capabilities.repos if node else []
            match = next(
                (r["path"] for r in repos if r["name"] == repo_path or r["path"] == repo_path),
                None,
            )
            if not match:
                names = ", ".join(r["name"] for r in repos) or "none advertised"
                await update.message.reply_text(
                    f"❌ Repo `{repo_path}` not found on `{node_id}`. Available: {names}"
                )
                return
            resolved_path = match

        session = await self._create_and_bind_session(
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id,
            backend=backend,
            repo_path=resolved_path,
            node_id=node_id,
        )
        await update.message.reply_text(
            self._format_session_created_message(session),
        )

    async def _handle_session_use(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_use <session_id>"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
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
            await update.message.reply_text(f"❌ Session {self._session_tag(session_id)} not found.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await update.message.reply_text("❌ You do not own that session.")
            return
        if session.status == SessionStatus.CLOSED:
            await update.message.reply_text(f"❌ Session {self._session_tag(session_id)} is closed.")
            return
        self.session_store.bind(update.effective_chat.id, session_id)
        await update.message.reply_text(self._format_session_switched_message(session))

    async def _handle_session_picker_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            await query.edit_message_text(f"❌ Session {self._session_tag(session_id)} not found.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await query.edit_message_text("❌ You do not own that session.")
            return
        if session.status == SessionStatus.CLOSED:
            await query.edit_message_text(f"❌ Session {self._session_tag(session_id)} is closed.")
            return

        self.session_store.bind(update.effective_chat.id, session_id)
        await query.edit_message_text(self._format_session_switched_message(session))

    async def _handle_session_new_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        await query.answer()

        if not self._check_user_permission(update.effective_user.id):
            await query.edit_message_text("❌ Access denied.")
            return

        data = query.data or ""
        _valid_backends = ("claude", "codex", "opencode", "opencode-server")

        # --- Cancel: bail out of the whole flow ---
        if data.startswith("session_new_cancel"):
            await query.edit_message_text("✖️ Cancelled. Run /session_new whenever you're ready.")
            return

        # --- Back: step backwards through the picker ---
        if data.startswith("session_new_back:"):
            parts = data.split(":")
            target = parts[1] if len(parts) > 1 else "backend"
            if target == "node" and len(parts) > 2:
                backend = parts[2].strip().lower()
                markup = self._build_session_node_markup(backend)
                await query.edit_message_text(
                    f"🖥 *New {backend} session* — which machine should run it?",
                    reply_markup=markup,
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    "🆕 *New session* — choose a backend:",
                    reply_markup=self._build_session_backend_markup(),
                    parse_mode="Markdown",
                )
            return

        if data.startswith("session_new_backend:"):
            backend = data.split(":", 1)[1].strip().lower()
            if backend not in _valid_backends:
                await query.edit_message_text("❌ Unknown backend.")
                return
            # If mesh is enabled and workers are online, show node picker first.
            nodes = self._mesh_online_nodes()
            if nodes:
                await query.edit_message_text(
                    f"🖥 *New {backend} session* — which machine should run it?",
                    reply_markup=self._build_session_node_markup(backend),
                    parse_mode="Markdown",
                )
            else:
                markup = self._build_session_repo_markup(backend, node_id="__local__", back_to="backend")
                if not self._repo_choices_for_node("__local__", limit=10):
                    await query.edit_message_text(
                        f"📂 *New {backend} session*\n\n"
                        "No repositories found under your workspace.\n"
                        f"Start one manually: `/session_new {backend} <path>`",
                        reply_markup=markup,
                        parse_mode="Markdown",
                    )
                    return
                await query.edit_message_text(
                    f"📂 *New {backend} session* — pick a repository:",
                    reply_markup=markup,
                    parse_mode="Markdown",
                )
            return

        if data.startswith("session_new_node:"):
            # format: session_new_node:{backend}:{node_id}
            parts = data.split(":", 2)
            if len(parts) != 3:
                await query.edit_message_text("❌ Invalid node selection.")
                return
            backend, node_id = parts[1].strip().lower(), parts[2].strip()
            if backend not in _valid_backends:
                await query.edit_message_text("❌ Unknown backend.")
                return
            node_label = "this server" if node_id == "__local__" else node_id
            markup = self._build_session_repo_markup(backend, node_id=node_id, back_to="node")
            if not self._repo_choices_for_node(node_id, limit=10):
                await query.edit_message_text(
                    f"📂 *{backend} on {node_label}*\n\n"
                    "No repositories found here.\n"
                    "Set `WORKER_PROJECTS_ROOT` on the worker, or start one manually with "
                    f"`/session_new {backend} <path>`.",
                    reply_markup=markup,
                    parse_mode="Markdown",
                )
                return
            await query.edit_message_text(
                f"📂 *{backend} on {node_label}* — pick a repository:",
                reply_markup=markup,
                parse_mode="Markdown",
            )
            return

        if data.startswith("session_new_repo:"):
            # format: session_new_repo:{backend}:{node_id}:{index}
            parts = data.split(":")
            if len(parts) != 4:
                await query.edit_message_text("❌ Invalid repository selection.")
                return
            backend, node_id = parts[1].strip().lower(), parts[2].strip()
            try:
                repo_index = int(parts[3])
            except ValueError:
                await query.edit_message_text("❌ Invalid repository selection.")
                return
            if backend not in _valid_backends:
                await query.edit_message_text("❌ Unknown backend.")
                return
            choices = self._repo_choices_for_node(node_id, limit=10)
            if repo_index < 0 or repo_index >= len(choices):
                await query.edit_message_text("❌ That choice expired. Run /session_new again.")
                return
            _label, repo_path = choices[repo_index]
            session = await self._create_and_bind_session(
                chat_id=update.effective_chat.id,
                user_id=update.effective_user.id,
                backend=backend,
                repo_path=repo_path,
                node_id=node_id,
            )
            await query.edit_message_text(
                self._format_session_created_message(session),
            )
            return

        await query.edit_message_text("❌ Unknown session_new action.")

    def _format_session_created_message(self, session: Session) -> str:
        """Friendly confirmation shown after a session is created."""
        show_node = self._mesh_node_column_enabled()
        return (
            self._session_card(session, header="✅ Session created & active", show_node=show_node)
            + "\n\n💬 Just type your request to start working.\n"
            "📂 /session_dirs to browse folders · /status to check in"
        )

    async def _handle_session_dirs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/session_dirs [path]"""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        session = self.session_store.get_active(update.effective_chat.id)
        has_session = bool(session and self._user_can_access_session(update.effective_user.id, session))

        if args and not has_session:
            # No session to anchor against — browse the gateway workspace locally,
            # exactly as before. (An explicit path is resolved against gateway roots.)
            resolution = self._path_resolver().resolve_session_path(" ".join(args))
            if not resolution.ok or not resolution.resolved_path:
                await update.message.reply_text(self._format_path_resolution_error(resolution))
                return
            path = resolution.resolved_path
            dirs = self._path_resolver().list_child_directories(path, limit=12, include_hidden=False, sort_by_recent=True)
        elif has_session:
            # Anchor on the session's repo and route to its owning node. An
            # optional path arg is treated as a child of the repo path.
            path = session.repo_path
            if args:
                import os
                path = os.path.join(session.repo_path, " ".join(args).lstrip("/\\"))
            result = await self._inspect(session, "list_dirs", {"path": path, "limit": 12, "sort_by_recent": True})
            if "error" in result:
                await update.message.reply_text(f"❌ {result['error']}")
                return
            dirs = result.get("dirs") or []
            path = result.get("path", path)
        else:
            resolver = self._path_resolver()
            path = str(resolver.base_cwd or resolver.allowed_root or "")
            if not path:
                await update.message.reply_text("No active session and no workspace root configured.")
                return
            dirs = resolver.list_child_directories(path, limit=12, include_hidden=False, sort_by_recent=True)

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
        result = await self._inspect(session, "list_dirs", {"limit": 8, "sort_by_recent": False})
        dirs = result.get("dirs") if "error" not in result else None
        await update.message.reply_text(self._format_session_overview(session, dirs=dirs))

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
                f"Cancellation requested for `{session.last_task_id}` in session {self._session_tag(session.session_id)}."
            )
        else:
            await update.message.reply_text(f"Task `{session.last_task_id}` is not cancellable.")

    async def _handle_compact(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/compact [session_id] — collapse the Claude context window for the active (or specified) session."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return
        args = context.args or []
        session = self.session_store.get(args[0]) if args else self.session_store.get_active(update.effective_chat.id)
        if not session:
            await update.message.reply_text("No active session. Use /session_new or /session_use first.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await update.message.reply_text("❌ You do not own that session.")
            return
        if not session.backend_session_id:
            await update.message.reply_text("Session has no backend context yet — nothing to compact.")
            return
        await update.message.reply_text("Compacting context...")
        try:
            result = await self.orchestrator.compact_session(session.session_id)
            if result.success:
                await update.message.reply_text("Context compacted. The session will continue with a condensed summary.")
            else:
                err = (result.errors or ["unknown error"])[0]
                await update.message.reply_text(f"Compaction failed: {err}")
        except Exception as e:
            logger.error(f"compact_session error: {e}")
            await update.message.reply_text(f"Error during compaction: {e}")

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
        backend_session_id = session.backend_session_id
        if backend_session_id:
            backend = getattr(self.orchestrator, "_backends", {}).get(session.backend)
            if backend and (not session.machine_id or session.machine_id == socket.gethostname()):
                try:
                    await asyncio.to_thread(backend.close, session)
                except Exception as e:
                    logger.warning(
                        "event=session_backend_close_failed session_id=%s backend=%s err=%s",
                        session.session_id,
                        session.backend,
                        e,
                    )
            elif session.machine_id and session.machine_id != socket.gethostname():
                logger.info(
                    "event=session_backend_close_remote_skipped session_id=%s backend=%s node=%s",
                    session.session_id,
                    session.backend,
                    session.machine_id,
                )
            session.backend_session_id = ""
        session.status = SessionStatus.CLOSED
        self.session_store.save(session)
        active = self.session_store.get_active(update.effective_chat.id)
        if active and active.session_id == session.session_id:
            self.session_store.unbind(update.effective_chat.id)
        await update.message.reply_text(
            f"Session {self._session_tag(session.session_id)} closed.\n"
            f"Ref: {self._session_message_ref(session)}\n"
            f"Summary: {self._format_session_material_summary(session)}"
        )

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
                await update.message.reply_text(f"Session {self._session_tag(session.session_id)} is already open ({session.status.value}).")
                return
            session.status = SessionStatus.IDLE
            self.session_store.save(session)
            self.session_store.bind(update.effective_chat.id, session.session_id)
            await update.message.reply_text(self._format_session_switched_message(session))
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
            await query.edit_message_text(f"❌ Session {self._session_tag(session_id)} not found.")
            return
        if not self._user_can_access_session(update.effective_user.id, session):
            await query.edit_message_text("❌ You do not own that session.")
            return
        if session.status != SessionStatus.CLOSED:
            await query.edit_message_text(
                f"Session {self._session_tag(session.session_id)} is already open ({session.status.value}). Use /session_use to switch."
            )
            return

        session.status = SessionStatus.IDLE
        self.session_store.save(session)
        self.session_store.bind(update.effective_chat.id, session.session_id)
        await query.edit_message_text(self._format_session_switched_message(session))

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
                session = self._find_session_for_task(task_id, chat_id=chat_id)
                ref = f"{self._session_message_ref(session, task_id)}\n" if session else f"#t_{task_id}\n"
                message = f"{ref}{summary if success else f'❌ {summary}'}"
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

            commit_key, task_description = self._build_git_commit_context(session)
            result = await self._inspect(session, "commit", {
                "task_id": commit_key,
                "task_description": task_description,
                "create_branch": create_branch,
                "push_branch": push_branch,
            })
            await update.message.reply_text(
                self._format_git_result(
                    f"❌ Failed to commit changes in session {self._session_tag(session.session_id)}.",
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

            commit_key, task_description = self._build_git_commit_context(session)
            result = await self._inspect(session, "commit_all", {
                "task_id": commit_key,
                "task_description": task_description,
                "create_branch": create_branch,
                "push_branch": push_branch,
            })
            await update.message.reply_text(
                self._format_git_result(
                    f"❌ Failed to commit staged changes in session {self._session_tag(session.session_id)}.",
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

            status = await self._inspect(session, "git_status")

            if "error" in status:
                await update.message.reply_text(f"❌ {status['error']}")
                return

            changes = status["changes"]
            message_parts = [
                "Git Repository Status",
                f"Session: {self._session_tag(session.session_id)}",
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

    async def _handle_jobs_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /jobs command — list watched jobs (running and recent)."""
        if not self._check_user_permission(update.effective_user.id):
            await update.message.reply_text("❌ Access denied.")
            return

        try:
            from src.control.db import get_db
            db = get_db()
            if db is None:
                await update.message.reply_text("❌ Mesh DB unavailable.")
                return

            args = context.args or []
            limit = 10
            if args:
                try:
                    limit = max(1, min(50, int(args[0])))
                except ValueError:
                    pass

            running = db.list_jobs(status="running", limit=limit)
            recent = db.list_jobs(limit=limit)

            lines = ["📋 **Watched Jobs**\n"]
            if running:
                lines.append(f"**Running ({len(running)}):**")
                for j in running:
                    label = j.get("label", j.get("id", "?"))
                    pid = j.get("pid")
                    pid_str = f" (PID {pid})" if pid else ""
                    lines.append(f"• `{label}`{pid_str}")
                lines.append("")

            done = [j for j in recent if j.get("status") in ("done", "failed", "lost")]
            if done:
                lines.append(f"**Recent ({len(done)}):**")
                for j in done[:limit]:
                    label = j.get("label", j.get("id", "?"))
                    s = j.get("status", "?")
                    ec = j.get("exit_code")
                    ec_str = f" exit={ec}" if ec is not None else ""
                    icon = {"done": "✅", "failed": "❌", "lost": "⚠️"}.get(s, "❓")
                    lines.append(f"{icon} `{label}` — {s}{ec_str}")
                lines.append("")

            if not running and not done:
                lines.append("No watched jobs.")

            await self._send_long_message(
                chat_id=update.effective_chat.id,
                text="\n".join(lines),
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error listing jobs: {e}")
            logger.error(f"Jobs command failed: {e}")
