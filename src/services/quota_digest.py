"""Temporary Telegram digest subscriber for quota coordinator events."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class QuotaTelegramDigestSubscriber:
    """Aggregate quota events and send bounded operator digests.

    The coordinator stays notification-agnostic: it calls this object as an
    event subscriber, and the subscriber uses NotificationService for delivery.
    """

    def __init__(
        self,
        *,
        notifier: Any,
        chat_id: Optional[int],
        interval_sec: int = 3600,
        now: Any = None,
    ) -> None:
        self.notifier = notifier
        self.chat_id = chat_id
        self.interval_sec = max(60, int(interval_sec))
        self._now = now or (lambda: datetime.now(tz=timezone.utc))
        self._pending: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._last_sent_at: Optional[datetime] = None
        self._send_task: Optional[asyncio.Task] = None

    def handle_event(self, name: str, payload: Dict[str, Any]) -> None:
        if not name.startswith("quota."):
            return
        key = (str(payload.get("provider") or ""), str(payload.get("bucket_id") or ""))
        self._pending[key] = {"event": name, **payload}
        if self._should_send_now(name):
            self._schedule_send()

    def _should_send_now(self, name: str) -> bool:
        if name in ("quota.adapter_unavailable", "quota.observed", "limit.reached", "window.reset_detected"):
            if self._last_sent_at is None:
                return True
        if self._last_sent_at is None:
            return False
        return (self._now() - self._last_sent_at).total_seconds() >= self.interval_sec

    def _schedule_send(self) -> None:
        if self._send_task is not None and not self._send_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._send_task = loop.create_task(self.flush())

    async def flush(self) -> None:
        if not self._pending:
            return
        events = list(self._pending.values())
        self._pending.clear()
        self._last_sent_at = self._now()
        text = self._format(events)
        try:
            await self.notifier.notify_quota_digest(text, chat_id=self.chat_id)
        except Exception as e:
            logger.debug("quota digest send failed err=%s", e)

    def _format(self, events: list[Dict[str, Any]]) -> str:
        lines = ["Quota coordinator digest", "Claude tokens spent by observer: 0"]
        for item in events[:10]:
            provider = item.get("provider") or "unknown"
            bucket = item.get("bucket_id") or "unknown"
            used = item.get("used_percent")
            reset = item.get("reset_at") or "unknown"
            quality = item.get("telemetry_quality") or "unknown"
            reason = item.get("reason") or ""
            try:
                used_text = "unknown" if used is None else f"{float(used):.1f}%"
            except (TypeError, ValueError):
                used_text = "unknown"
            suffix = f" reason={reason}" if reason else ""
            lines.append(f"- {provider}/{bucket}: used={used_text} reset={reset} quality={quality}{suffix}")
        if len(events) > 10:
            lines.append(f"- ... {len(events) - 10} more")
        return "\n".join(lines)
