"""Web Push (#21) delivery helper.

Best-effort browser push for sanitized task/session terminal outcomes. This is a
*second* notification channel alongside Telegram (see NotificationService) and
follows the same rules:

- never raise into the caller
- best-effort per subscription
- fan-out is bounded (concurrency + per-send timeout) and MUST NOT block task
  completion — callers fire it via ``asyncio.create_task`` (see
  ``NotificationService.notify_task_outcome``).

Privacy: only sanitized facts are ever sent — title, short body, task/session IDs,
and a session URL. Never prompts, assistant output, file contents, command lines,
or raw errors.

Transport: RFC 8291 encryption + VAPID signing is delegated to ``pywebpush``,
imported lazily so a missing package (or missing VAPID config) simply means push
is disabled — the gateway keeps running and Telegram delivery is unaffected.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# HTTP status codes from a push service that mean the subscription is permanently
# gone and should be disabled rather than retried.
_GONE_STATUS = {404, 410}


class _MalformedSubscription(Exception):
    """A stored subscription row is missing required fields — permanent, disable it
    rather than retrying it on every outcome."""


def push_available(cfg: Any, db: Any) -> tuple[bool, Optional[str]]:
    """Return (available, reason). Available iff VAPID configured, pywebpush
    importable, and a DB is present. ``reason`` explains why not, for the UI."""
    push_cfg = getattr(cfg, "push", None)
    if push_cfg is None or not getattr(push_cfg, "configured", False):
        return False, "vapid_not_configured"
    if db is None:
        return False, "db_unavailable"
    try:
        import pywebpush  # noqa: F401
    except Exception:
        return False, "pywebpush_not_installed"
    return True, None


def build_task_payload(
    *,
    title: str,
    body: str,
    task_id: Optional[str],
    session_id: Optional[str],
    url: Optional[str],
) -> dict:
    """Assemble the sanitized push payload consumed by the service worker.

    Caller is responsible for ensuring ``title``/``body`` contain no sensitive
    content; this helper only bounds their length.
    """
    return {
        "title": (title or "AI-Team")[:120],
        "body": (body or "")[:240],
        "task_id": task_id,
        "session_id": session_id,
        "url": url or "/",
    }


class PushService:
    """Bounded, best-effort Web Push fan-out."""

    def __init__(self, cfg: Any, db: Any):
        self._cfg = cfg
        self._db = db

    def available(self) -> tuple[bool, Optional[str]]:
        return push_available(self._cfg, self._db)

    def _enabled_sub_count(self) -> int:
        try:
            return len(self._db.list_push_subscriptions(enabled_only=True))
        except Exception:
            return 0

    async def fanout(self, payload: dict) -> None:
        """Send ``payload`` to every enabled subscription, bounded by concurrency
        and per-send timeout. Never raises. Expired subscriptions are disabled;
        transient errors are recorded but the subscription is kept."""
        ok, reason = self.available()
        if not ok:
            # If subscribers exist but we're skipping for a config reason, the
            # operator is EXPECTING notifications and getting silence — make that
            # visible (WARNING), naming the missing env vars. Otherwise stay quiet.
            sub_count = self._enabled_sub_count()
            if sub_count > 0:
                missing = ""
                try:
                    push_cfg = getattr(self._cfg, "push", None)
                    if push_cfg is not None and hasattr(push_cfg, "missing_config"):
                        missing = ",".join(push_cfg.missing_config())
                except Exception:
                    pass
                logger.warning(
                    "event=push_fanout_skipped reason=%s subscribers=%d missing_env=%s",
                    reason, sub_count, missing or "-",
                )
            else:
                logger.debug("event=push_fanout_skipped reason=%s subscribers=0", reason)
            return

        try:
            subs = self._db.list_push_subscriptions(enabled_only=True)
        except Exception as e:
            logger.warning("event=push_fanout_list_failed err=%s", e)
            return
        if not subs:
            logger.debug("event=push_fanout_no_subscribers")
            return
        logger.info("event=push_fanout_start subscribers=%d", len(subs))
        if not subs:
            return

        push_cfg = self._cfg.push
        sem = asyncio.Semaphore(max(1, int(getattr(push_cfg, "fanout_concurrency", 8))))
        data = json.dumps(payload)

        async def _one(sub: dict) -> None:
            async with sem:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._send_blocking, sub, data),
                        timeout=float(getattr(push_cfg, "send_timeout_sec", 5.0)),
                    )
                except asyncio.TimeoutError:
                    self._safe(self._db.mark_push_error, sub.get("endpoint"), "timeout")
                except Exception as e:
                    self._handle_send_error(sub, e)

        # Isolate every send; a single failure must not sink the batch.
        await asyncio.gather(*(_one(s) for s in subs), return_exceptions=True)

    # ------------------------------------------------------------------

    def _send_blocking(self, sub: dict, data: str) -> None:
        """Blocking pywebpush send. Runs in a thread; raises on failure so the
        async wrapper can classify the error."""
        push_cfg = self._cfg.push
        # Validate the row BEFORE importing/calling the transport: a junk row
        # should be classified permanent, not hidden behind an ImportError.
        try:
            subscription_info = {
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh_key"], "auth": sub["auth_key"]},
            }
        except KeyError as e:
            raise _MalformedSubscription(str(e)) from e

        from pywebpush import webpush  # lazy

        # vapid_claims MUST be a fresh dict per call: pywebpush mutates it (injects
        # `aud` and an `exp` expiry). Reusing one dict across sends would carry an
        # expired `exp` into the 2nd send and raise VapidException. Never hoist this.
        vapid_claims = {"sub": push_cfg.vapid_subject}
        webpush(
            subscription_info=subscription_info,
            data=data,
            vapid_private_key=push_cfg.vapid_private_key,
            vapid_claims=vapid_claims,
            timeout=float(getattr(push_cfg, "send_timeout_sec", 5.0)),
        )

    def _handle_send_error(self, sub: dict, err: Exception) -> None:
        endpoint = sub.get("endpoint")
        # Malformed row = permanent; disable so it stops retrying every outcome.
        if isinstance(err, _MalformedSubscription):
            logger.warning("event=push_subscription_malformed endpoint=%s err=%s", endpoint, err)
            self._safe(self._db.disable_push_subscription, endpoint)
            return
        status = _status_of(err)
        if status in _GONE_STATUS:
            logger.info("event=push_subscription_gone endpoint=%s status=%s", endpoint, status)
            self._safe(self._db.disable_push_subscription, endpoint)
        else:
            self._safe(self._db.mark_push_error, endpoint, f"{type(err).__name__}:{status or ''}")

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            logger.debug("event=push_db_side_effect_failed err=%s", e)


def _status_of(err: Exception) -> Optional[int]:
    """Best-effort extraction of an HTTP status from a pywebpush error."""
    resp = getattr(err, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    return None
