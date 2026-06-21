"""
Notification dispatcher — single call site for all outbound notifications.

The orchestrator calls ``self.notifier.notify_*()`` instead of reaching into
``self.telegram_interface`` directly.  Each method:

1. emits a structured NDJSON event (observability / future Web UI stream)
2. forwards to Telegram if the TelegramInterface is configured

Adding a second delivery channel (e.g. WebSocket -> Web UI) means adding
one more handler call in the relevant ``notify_*`` method — **zero** changes
to the orchestrator or any other domain code.

Design rules (same as the rest of the codebase):
- never raise into the caller
- best-effort delivery per channel
- all formatting lives here or in ``result_text``, not in the orchestrator
"""
import logging
from typing import Any, Optional

from src.services.result_text import session_reply_text, short_failure_reason, format_file_change_lines

logger = logging.getLogger(__name__)


class NotificationService:
    """Central notification dispatcher owned by TaskOrchestrator.

    Accesses the orchestrator's ``telegram_interface`` dynamically so
    the interface can be swapped after construction (e.g. in tests).
    """

    def __init__(self, orchestrator: Any):
        self._orchestrator = orchestrator

    @property
    def _telegram(self) -> Optional[Any]:
        return getattr(self._orchestrator, "telegram_interface", None)

    # ------------------------------------------------------------------
    # Task outcome
    # ------------------------------------------------------------------

    async def notify_task_outcome(
        self,
        task_id: str,
        result: Any,
        *,
        session: Optional[Any] = None,
        chat_id: Optional[int] = None,
        prefix: str = "",
    ) -> None:
        """Deliver a task completion or failure notification.

        Builds the user-facing text from ``result``, emits a structured
        event, then sends via Telegram if a chat target is available.
        """
        from src.core.observability import emit_event

        success = bool(getattr(result, "success", False))
        text = self._build_outcome_text(result, success=success, prefix=prefix)

        emit_event(
            "task_notification",
            task_id=task_id,
            session_id=getattr(session, "session_id", None) if session else None,
            status="success" if success else "failed",
        )

        tg = self._telegram
        if chat_id and tg:
            try:
                await tg.notify_completion(
                    task_id, text, success=success, chat_id=chat_id,
                )
            except Exception as e:
                logger.warning("notify_task_outcome failed task=%s err=%s", task_id, e)

    # ------------------------------------------------------------------
    # Heartbeat (progress update for long-running work)
    # ------------------------------------------------------------------

    async def notify_heartbeat(
        self,
        task_id: str,
        *,
        session: Optional[Any] = None,
        chat_id: Optional[int] = None,
        elapsed_min: int = 0,
        remaining_min: int = 0,
    ) -> None:
        """Send a progress heartbeat for a long-running task."""
        tg = self._telegram
        if not chat_id or not tg:
            return

        from src.core.observability import emit_event

        session_ref = f"`{getattr(session, 'session_id', '')}`" if session else ""
        task_ref = f"`{task_id}`"
        limit_note = (
            f" ({remaining_min}m left before gateway timeout)" if remaining_min > 2
            else " (approaching timeout)"
        )
        msg = (
            f"\U000023F3 Still working\u2026 {elapsed_min}m elapsed{limit_note}\n"
            f"Session {session_ref} / task {task_ref}"
        )

        emit_event("heartbeat", task_id=task_id)

        try:
            await tg.app.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.debug("heartbeat send failed task=%s err=%s", task_id, e)

    # ------------------------------------------------------------------
    # Error notification
    # ------------------------------------------------------------------

    async def notify_error(
        self,
        message: str,
        *,
        task_id: Optional[str] = None,
        chat_id: Optional[int] = None,
    ) -> None:
        """Notify about a system-level error."""
        from src.core.observability import emit_event

        emit_event("error_notification", task_id=task_id)

        tg = self._telegram
        if chat_id and tg:
            try:
                await tg.notify_completion(
                    task_id or "unknown", message, success=False, chat_id=chat_id,
                )
            except Exception as e:
                logger.warning("notify_error failed err=%s", e)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_outcome_text(result: Any, *, success: bool, prefix: str = "") -> str:
        """Produce user-facing text from a TaskResult-like object."""
        if success:
            content = session_reply_text(result)
            files = getattr(result, "files_modified", None) or []
            if files:
                lines = format_file_change_lines(result, limit=20)
                content = content + "\n\n**Changed files:**\n" + "\n".join(lines)
            if prefix:
                content = prefix + content
            return content

        reason = short_failure_reason(result)
        return f"Task failed: {reason}" if reason else "Task failed"
