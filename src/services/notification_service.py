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
import re
from typing import Any, Optional

from src.services.result_text import session_reply_text, short_failure_reason, format_file_change_lines, trim_reply_for_chat

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

        session_id = getattr(session, "session_id", None) if session else None

        emit_event(
            "task_notification",
            task_id=task_id,
            session_id=session_id,
            status="success" if success else "failed",
        )

        # Web Push fan-out — a SECOND channel, independent of Telegram. It runs on
        # this unconditional path (NOT under the chat_id/telegram guard below) so
        # Web-only sessions, which have no chat_id, still get notified. Fire-and-
        # forget so a slow/absent push service never blocks task completion.
        self._maybe_push_outcome(task_id, session_id, result, session, success=success)

        tg = self._telegram
        if chat_id and tg:
            try:
                await tg.notify_completion(
                    task_id, text, success=success, chat_id=chat_id,
                )
            except Exception as e:
                logger.warning("notify_task_outcome failed task=%s err=%s", task_id, e)

    # ------------------------------------------------------------------
    # Web Push (#21) — best-effort second channel
    # ------------------------------------------------------------------

    def _maybe_push_outcome(
        self,
        task_id: str,
        session_id: Optional[str],
        result: Any,
        session: Optional[Any],
        *,
        success: bool,
    ) -> None:
        """Schedule a best-effort Web Push for a terminal outcome.

        Never raises, never blocks: the actual fan-out runs as a detached task
        bounded by concurrency + per-send timeout inside PushService.
        """
        import asyncio

        try:
            from config import config as _cfg
            from src.control.db import get_db
            from src.services.push_service import PushService, build_task_payload

            db = get_db()
            svc = PushService(_cfg, db)
            ok, _reason = svc.available()
            if not ok:
                return

            repo_path = getattr(session, "repo_path", None) if session else None
            project = self._project_name(repo_path)
            emoji = "✅" if success else "❌"
            title = f"{emoji} {project}" if project else ("Task complete" if success else "Task failed")

            if success:
                snippet = session_reply_text(result)
            else:
                snippet = short_failure_reason(result) or "Task failed"
            snippet = " ".join(snippet.split())  # chat-preview style: single line, no markdown/newlines

            machine_id = getattr(session, "machine_id", None) if session else None
            model = getattr(session, "model", None) if session else None
            node_model = "/".join(part for part in (machine_id, model) if part)
            body = f"[{node_model}] {snippet}" if node_model else snippet

            url = f"/sessions/{session_id}" if session_id else "/"
            payload = build_task_payload(
                title=title,
                body=body,
                task_id=task_id,
                session_id=session_id,
                url=url,
            )

            async def _run() -> None:
                try:
                    await svc.fanout(payload)
                except Exception as e:  # defensive: fanout already swallows
                    logger.debug("push fanout task=%s err=%s", task_id, e)

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_run())
            except RuntimeError:
                # No running loop (e.g. sync test context) — skip; push is best-effort.
                logger.debug("no running loop for push fanout task=%s", task_id)
        except Exception as e:
            logger.debug("maybe_push_outcome failed task=%s err=%s", task_id, e)

    @staticmethod
    def _project_name(repo_path: Optional[str]) -> Optional[str]:
        """Last path segment, splitting on both ``/`` and ``\\`` — worker nodes may
        report Windows paths (e.g. ``C:\\Users\\...\\AI-team``) to this Linux
        gateway, where ``os.path.basename`` only recognizes ``/``."""
        if not repo_path:
            return None
        parts = [p for p in re.split(r"[/\\]", str(repo_path)) if p]
        return parts[-1] if parts else None

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

    async def notify_case_resume_proposal(
        self,
        *,
        case_id: str,
        mode: str,
        estimate_usd: Optional[float],
        estimate_known: bool,
        reset_at: Optional[str] = None,
        objective: str = "",
        auto: bool = False,
        chat_id: Optional[int] = None,
    ) -> None:
        """[quota-resume] Tell the operator their quota came back and a Case is
        waiting on a decision — on BOTH channels, because this is the one moment
        the harness genuinely needs a human and it usually arrives hours later,
        when nobody is looking at the dashboard.

        Channel split is deliberate:
          * **Web Push** — a real browser notification (works with the tab
            closed), deep-linking to the Case where the decision lives.
          * **Telegram** — notification ONLY. It carries no approve/decline
            affordance on purpose: approving spends real money and the decision
            surface stays the authenticated Web UI, which is where the estimate,
            the mode choice and the Case evidence are.

        Best-effort on both: a notification failure must never block or alter the
        proposal that was already recorded.
        """
        from src.core.observability import emit_event

        emit_event("case_resume_notification", session_id=None)

        cost = (
            f"~${estimate_usd:.2f}" if estimate_known and isinstance(estimate_usd, (int, float))
            else "cost unknown"
        )
        short_case = case_id[:8]
        # AUTO mode still notifies — louder, if anything: money was spent without
        # being asked, so the operator learns about it at the moment it happens
        # rather than from a bill.
        title = (
            "▶ Quota restored — Case resumed automatically" if auto
            else "⏸ Quota restored — resume this Case?"
        )
        summary = " ".join((objective or "").split())[:120]
        body = f"[{short_case}] {mode} · {cost}" + (f" · {summary}" if summary else "")

        self._maybe_push_case_resume(case_id=case_id, title=title, body=body)

        tg = self._telegram
        target = chat_id
        if target is None:
            try:
                from config import config as _cfg
                target = getattr(getattr(_cfg, "telegram", None), "notification_chat_id", None)
            except Exception:
                target = None
        if target and tg:
            head = (
                f"▶ *Quota restored* — Case `{short_case}` was resumed automatically."
                if auto else
                f"⏸ *Quota restored* — Case `{short_case}` is paused and waiting."
            )
            tail = (
                "\n\nCASE_QUOTA_RESUME_AUTO is ON — no approval was asked. Open the "
                "Web UI to watch it → /work/"
                if auto else
                "\n\nApprove or decline in the Web UI → /work/"
            )
            text = (
                head + "\n"
                + (f"Mode: `{mode}` · estimated {cost}" if auto
                   else f"Recommended: `{mode}` · estimated {cost}")
                + (f"\nReset: {reset_at}" if reset_at else "")
                + (f"\n{summary}" if summary else "")
                + tail + case_id
            )
            try:
                await tg.notify_completion(
                    f"case-resume-{short_case}", text, success=True, chat_id=target,
                )
            except Exception as e:
                logger.warning("notify_case_resume_proposal telegram failed case=%s err=%s", case_id, e)

    def _maybe_push_case_resume(self, *, case_id: str, title: str, body: str) -> None:
        """Detached Web Push for a resume proposal. Mirrors ``_maybe_push_outcome``
        (same bounded fan-out, same never-raise contract); only the deep link and
        the payload identity differ — the Case, not a task."""
        import asyncio

        try:
            from config import config as _cfg
            from src.control.db import get_db
            from src.services.push_service import PushService, build_task_payload

            svc = PushService(_cfg, get_db())
            ok, _reason = svc.available()
            if not ok:
                return
            payload = build_task_payload(
                title=title, body=body, task_id=None, session_id=None,
                url=f"/work/{case_id}",
            )

            async def _run() -> None:
                try:
                    await svc.fanout(payload)
                except Exception as e:
                    logger.debug("push fanout case_resume case=%s err=%s", case_id, e)

            try:
                asyncio.get_running_loop().create_task(_run())
            except RuntimeError:
                logger.debug("no running loop for case_resume push case=%s", case_id)
        except Exception as e:
            logger.debug("maybe_push_case_resume failed case=%s err=%s", case_id, e)

    async def notify_quota_digest(self, message: str, *, chat_id: Optional[int] = None) -> None:
        """Deliver a temporary quota coordinator digest through notification seams."""
        from src.core.observability import emit_event

        emit_event("quota_digest_notification")
        tg = self._telegram
        if chat_id and tg:
            try:
                await tg.notify_completion("quota-coordinator", message, success=True, chat_id=chat_id)
            except Exception as e:
                logger.warning("notify_quota_digest failed err=%s", e)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_outcome_text(result: Any, *, success: bool, prefix: str = "") -> str:
        """Produce user-facing text from a TaskResult-like object.

        Assembly order matters:
          1. Extract and trim the reply body (the only thing that gets long).
          2. Append the file-change summary — always in full; it's small and
             high-signal. Including it inside the trim would silently cut it when
             the reply is large.
          3. Prepend the prefix last so it is never consumed by the cap.
        """
        if success:
            reply = session_reply_text(result)

            # Trim the reply body only — read the cap from config so it's
            # tunable without a deploy (TG_REPLY_MAX_CHARS env var; 0 = off).
            try:
                from config import config as _cfg
                max_chars = getattr(_cfg.system, "telegram_reply_max_chars", 0)
            except Exception:
                max_chars = 0
            reply = trim_reply_for_chat(reply, max_chars)

            # File summary always appended after the trim so it is never eaten.
            files = getattr(result, "files_modified", None) or []
            if files:
                lines = format_file_change_lines(result, limit=20)
                reply = reply + "\n\n**Changed files:**\n" + "\n".join(lines)

            return (prefix + reply) if prefix else reply

        reason = short_failure_reason(result)
        return f"Task failed: {reason}" if reason else "Task failed"
