"""A82 Stage 4a — fair managed-turn scheduler (design §5).

One gateway loop activates eligible queued heads (``queued -> pending``); the
carriers then claim the pending rows (Stage 3). Rules honoured:

  * Queued rows are the authority; events are hints. A coalesced in-process
    event (admission / completion / claim / withdrawal ``notify``) wakes the
    loop; a bounded 3 s fallback runs ONLY while waiting rows exist, otherwise
    the loop sleeps until the next hint (no idle polling, no Case poller, no
    event-log scan).
  * Each pass reads the bounded waiting subset with eligibility applied BEFORE
    ``LIMIT 25`` (``MeshDB.select_eligible_turn_heads``), one head per session,
    and activates heads in small separate transactions, yielding between them.
  * Expensive preparation (context/role/attachment assembly) runs OUTSIDE the
    transaction against the head's revision + session config revision; the
    activation transaction re-checks both and re-preparation happens once on a
    stale revision.
  * Nothing is held per row after activation: no executor, coroutine or
    polling task per pending/running (local or remote) turn.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseModel

from .turn_admission import ALLOWANCE, SharedWaitingAllowance

logger = logging.getLogger(__name__)

ACTIVATION_LIMIT_PER_PASS = 25
# Retry clock after a FAILED pass (design §12: 3 s scheduler fallback).
FALLBACK_INTERVAL_SEC = 3.0
# Lost-hint safety net while queued rows exist but none is time-eligible
# (heads waiting on a slot holder / paused session progress only on hints).
SAFETY_NET_SEC = 60.0
_PREPARE_ATTEMPTS = 2


class PreparedTurn(BaseModel):
    """Immutable execution payload prepared outside the activation txn."""

    model_config = {"extra": "forbid", "frozen": True}

    action: str
    payload: Dict[str, Any]
    machine_id: Optional[str] = None


class SchedulerPassResult(BaseModel):
    model_config = {"extra": "forbid"}

    selected: int = 0
    activated: int = 0
    stale: int = 0
    ineligible: int = 0
    blocked: int = 0
    withdrawn: int = 0
    waiting: int = 0
    next_wake_sec: Optional[float] = None


PrepareFn = Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[PreparedTurn]]


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


async def _activate_head(
    db: Any, prepare: PrepareFn, head: Dict[str, Any], limit: int,
) -> str:
    """Prepare (outside the txn) + conditionally activate one head. Returns the
    activation outcome (``activated``/``stale``/``ineligible``/``gone``/
    ``oversize``/``blocked``)."""
    task_id = str(head["id"])
    current = head
    outcome = "stale"
    for _ in range(_PREPARE_ATTEMPTS):
        row = await asyncio.to_thread(db.get_task, task_id)
        if row is None or row.get("status") != "queued":
            return "gone"
        try:
            prepared = await prepare(current, row)
        except Exception as e:  # noqa: BLE001 — leave queued with a reason + backoff
            changed = await asyncio.to_thread(
                db.mark_turn_blocked, task_id, f"prepare_failed: {type(e).__name__}",
            )
            if changed:  # log on state change only, not every retry
                logger.warning("event=turn_prepare_failed task_id=%s err=%s", task_id, e)
            return "blocked"
        expected_config = int(current["config_revision"])
        outcome = await asyncio.to_thread(
            lambda: db.activate_prepared_turn(
                task_id,
                expected_revision=int(row["revision"]),
                expected_config_revision=expected_config,
                action=prepared.action,
                payload=prepared.payload,
                machine_id=prepared.machine_id,
            )
        )
        if outcome != "stale":
            return outcome
        # Revision/config moved under us: re-read the head and re-prepare.
        fresh = [
            h for h in await asyncio.to_thread(db.select_eligible_turn_heads, limit)
            if str(h["id"]) == task_id
        ]
        if not fresh:
            return "ineligible"
        current = fresh[0]
    return outcome


async def run_scheduler_pass(
    db: Any,
    prepare: PrepareFn,
    *,
    limit: int = ACTIVATION_LIMIT_PER_PASS,
    allowance: Optional[SharedWaitingAllowance] = None,
) -> SchedulerPassResult:
    """One bounded activation pass. ``prepare(head, row)`` builds the
    execution payload for the full row (outside any transaction)."""
    result = SchedulerPassResult()
    heads: List[Dict[str, Any]] = await asyncio.to_thread(
        db.select_eligible_turn_heads, limit,
    )
    result.selected = len(heads)
    now_iso = _utc_now_iso()
    for head in heads:
        task_id = str(head["id"])
        expires = head.get("expires_at")
        if expires and str(expires) <= now_iso and head.get("turn_source") != "human":
            # Obsolete optional automation (design §3.10): withdraw with reason.
            try:
                await asyncio.to_thread(
                    lambda: db.withdraw_turn(
                        task_id, int(head["revision"]), actor="scheduler:expired",
                    )
                )
                result.withdrawn += 1
            except Exception:  # noqa: BLE001 — raced; next pass re-reads
                logger.debug("event=turn_scheduler_expire_race task_id=%s", task_id)
            continue
        outcome = await _activate_head(db, prepare, head, limit)
        if outcome == "activated":
            result.activated += 1
        elif outcome == "stale":
            result.stale += 1
        elif outcome in ("oversize", "blocked"):
            result.blocked += 1
        elif outcome == "ineligible":
            result.ineligible += 1
        await asyncio.sleep(0)  # yield between small transactions
    shared = allowance if allowance is not None else ALLOWANCE
    generation = shared.snapshot_generation()
    totals: Dict[str, int] = await asyncio.to_thread(db.managed_waiting_totals)
    shared.refresh_managed(totals["count"], generation)
    result.waiting = int(totals["queued"])
    if result.waiting:
        wake = await asyncio.to_thread(db.next_turn_wake_at)
        if wake:
            try:
                delay = (datetime.fromisoformat(wake) - datetime.now(tz=timezone.utc)).total_seconds()
                result.next_wake_sec = max(0.05, delay)
            except ValueError:
                result.next_wake_sec = None
    return result


def _next_timeout(res: SchedulerPassResult, limit: int, safety_net_sec: float) -> Optional[float]:
    """When to run the next pass without a hint: immediately if the pass hit
    its LIMIT (more eligible heads may remain); at the earliest time a delayed
    or backed-off head becomes eligible; else a long lost-hint safety net while
    queued rows exist; else never (sleep until a hint)."""
    if res.activated >= limit:
        return 0
    if not res.waiting:
        return None
    if res.next_wake_sec is not None:
        return min(res.next_wake_sec, safety_net_sec)
    return safety_net_sec


class TurnScheduler:
    """The single gateway scheduler loop (event hint + bounded fallback)."""

    def __init__(
        self,
        db: Any,
        prepare: PrepareFn,
        *,
        fallback_sec: float = FALLBACK_INTERVAL_SEC,
        limit: int = ACTIVATION_LIMIT_PER_PASS,
        allowance: Optional[SharedWaitingAllowance] = None,
        safety_net_sec: float = SAFETY_NET_SEC,
    ) -> None:
        self._db = db
        self._prepare = prepare
        self._fallback_sec = fallback_sec
        self._safety_net_sec = safety_net_sec
        self._limit = limit
        self._allowance = allowance
        self._event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._last_error_log = 0.0
        self.passes = 0

    def hint(self) -> None:
        """Thread-safe, coalescing wake-up (callable from any loop/thread)."""
        loop, event = self._loop, self._event
        if loop is None or event is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass

    def stop(self) -> None:
        self._running = False
        self.hint()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        self._running = True
        _register(self)
        try:
            while self._running:
                self._event.clear()
                timeout: Optional[float] = None
                try:
                    res = await run_scheduler_pass(
                        self._db, self._prepare, limit=self._limit,
                        allowance=self._allowance,
                    )
                    self.passes += 1
                    timeout = _next_timeout(res, self._limit, self._safety_net_sec)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — keep the loop alive
                    now = time.monotonic()
                    if now - self._last_error_log > 60:
                        self._last_error_log = now
                        logger.warning("event=turn_scheduler_pass_failed err=%s", e)
                    timeout = self._fallback_sec  # retry on the fallback clock
                if not self._running:
                    break
                if timeout == 0:
                    continue
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
        finally:
            _unregister(self)


_ACTIVE_LOCK = threading.Lock()
_ACTIVE: Optional[TurnScheduler] = None


def _register(scheduler: TurnScheduler) -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = scheduler


def _unregister(scheduler: TurnScheduler) -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is scheduler:
            _ACTIVE = None


def notify_turn_queue_changed() -> None:
    """Hint the gateway scheduler that queue state changed (admission,
    completion, claim, withdrawal). No-op without a scheduler; the durable rows
    remain the authority either way."""
    with _ACTIVE_LOCK:
        scheduler = _ACTIVE
    if scheduler is not None:
        scheduler.hint()
