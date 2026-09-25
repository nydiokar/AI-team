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


class TurnObsolete(Exception):
    """[A82 Stage 4c] Raised by ``prepare`` when activation-time revalidation
    finds a queued AUTOMATION turn obsolete (its Case blocked/closed, its
    continuation already reviewed, the Manager binding changed). The scheduler
    withdraws it with the reason instead of activating it (design §3.10/§7)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    slot_waiting: int = 0
    lineage_recovered: int = 0
    carrier_requeued: int = 0
    pending: int = 0
    # [A82 Stage 4b] withdrawn turns whose Case lineage is still `void`.
    lineage_voided: int = 0
    void_outstanding: int = 0


PrepareFn = Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[PreparedTurn]]
RecoverFn = Callable[[Dict[str, Any]], Awaitable[bool]]


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
        except TurnObsolete as ob:
            try:
                await asyncio.to_thread(
                    lambda: db.withdraw_turn(
                        task_id, int(row["revision"]),
                        actor=f"scheduler:obsolete:{ob.reason}"[:128],
                    )
                )
            except Exception:  # noqa: BLE001 — raced (edited/moved); next pass re-reads
                logger.debug("event=turn_obsolete_withdraw_race task_id=%s", task_id)
                return "ineligible"
            logger.info("event=turn_withdrawn_obsolete task_id=%s reason=%s", task_id, ob.reason)
            return "withdrawn"
        except Exception as e:  # noqa: BLE001 — leave queued with a reason + backoff
            changed = await asyncio.to_thread(
                db.mark_turn_blocked, task_id,
                f"prepare_failed: {type(e).__name__}: {str(e)[:200]}",
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
    recover_lineage: Optional[RecoverFn] = None,
    void_lineage: Optional[RecoverFn] = None,
) -> SchedulerPassResult:
    """One bounded activation pass. ``prepare(head, row)`` builds the
    execution payload for the full row (outside any transaction).
    ``recover_lineage(row)`` repairs rows left in the durable "lineage pending"
    state by a writer that died/stalled past its lease (bounded, first)."""
    result = SchedulerPassResult()
    requeue = getattr(db, "requeue_turns_on_dead_carriers", None)
    if callable(requeue):
        result.carrier_requeued = len(await asyncio.to_thread(requeue, limit))
    if recover_lineage is not None:
        for row in await asyncio.to_thread(db.list_lineage_recovery, limit):
            try:
                if await recover_lineage(row):
                    result.lineage_recovered += 1
            except Exception as e:  # noqa: BLE001 — stays pending; retried later
                logger.debug("event=turn_lineage_recovery_error task_id=%s err=%s", row.get("id"), e)
    if void_lineage is not None:
        # [A82 Stage 4b] Void the Case lineage of withdrawn turns (session
        # close) that the live closer could not finish (a writer lease still
        # live, a DB error). Bounded; the index holds only unfinished rows.
        for row in await asyncio.to_thread(db.list_void_lineage, limit):
            try:
                if await void_lineage(row):
                    result.lineage_voided += 1
                else:
                    result.void_outstanding += 1
            except Exception as e:  # noqa: BLE001 — stays void; retried later
                result.void_outstanding += 1
                logger.debug("event=turn_void_lineage_error task_id=%s err=%s", row.get("id"), e)
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
        elif outcome == "withdrawn":
            result.withdrawn += 1
        await asyncio.sleep(0)  # yield between small transactions
    # Keep the process-level enrollment presence honest (cleared when none).
    refresh = getattr(db, "refresh_enrollment_presence", None)
    if callable(refresh):
        await asyncio.to_thread(refresh)
    shared = allowance if allowance is not None else ALLOWANCE
    generation = shared.snapshot_generation()
    totals: Dict[str, int] = await asyncio.to_thread(db.managed_waiting_totals)
    shared.refresh_managed(totals["count"], generation)
    result.waiting = int(totals["queued"])
    result.pending = int(totals["count"]) - int(totals["queued"])
    if result.waiting:
        result.slot_waiting = int(await asyncio.to_thread(db.count_slot_waiting_sessions))
        wake = await asyncio.to_thread(db.next_turn_wake_at)
        if wake:
            try:
                delay = (datetime.fromisoformat(wake) - datetime.now(tz=timezone.utc)).total_seconds()
                result.next_wake_sec = max(0.05, delay)
            except ValueError:
                result.next_wake_sec = None
    return result


def _next_timeout(
    res: SchedulerPassResult, limit: int, safety_net_sec: float,
    slot_backoff_sec: Optional[float] = None,
) -> Optional[float]:
    """When to run the next pass without a hint: immediately if the pass hit
    its LIMIT (more eligible heads may remain); at the earliest time a delayed
    or backed-off head becomes eligible; for heads waiting on their own slot
    holder, on a bounded exponential backoff (a completion committed by an
    out-of-process task server cannot hint this loop); else a long lost-hint
    safety net while queued rows exist; else never (sleep until a hint)."""
    if res.activated >= limit:
        return 0
    if res.void_outstanding and not res.waiting:
        # [A82 Stage 4b] An unfinished lineage void (writer lease ≤30 s) needs a
        # bounded wake even when the fleet is otherwise idle.
        return FALLBACK_INTERVAL_SEC
    if not res.waiting:
        # Activated-but-unclaimed rows exist: keep the bounded safety-net wake so
        # a carrier that dies with the fleet otherwise idle is still detected
        # and its rows requeued (no wedge). Nothing pending ⇒ sleep until a hint.
        return safety_net_sec if res.pending else None
    candidates = [safety_net_sec]
    if res.next_wake_sec is not None:
        candidates.append(res.next_wake_sec)
    if res.slot_waiting and slot_backoff_sec is not None:
        candidates.append(slot_backoff_sec)
    return min(candidates)


SLOT_BACKOFF_BASE_SEC = 3.0
SLOT_BACKOFF_CAP_SEC = 30.0


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
        recover_lineage: Optional[RecoverFn] = None,
        void_lineage: Optional[RecoverFn] = None,
    ) -> None:
        self._db = db
        self._prepare = prepare
        self._recover_lineage = recover_lineage
        self._void_lineage = void_lineage
        self._fallback_sec = fallback_sec
        self._safety_net_sec = safety_net_sec
        self._slot_backoff_k = 0
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
                        allowance=self._allowance, recover_lineage=self._recover_lineage,
                        void_lineage=self._void_lineage,
                    )
                    self.passes += 1
                    if res.activated:
                        self._slot_backoff_k = 0
                    slot_backoff = min(
                        self._fallback_sec * (2 ** self._slot_backoff_k), SLOT_BACKOFF_CAP_SEC,
                    )
                    timeout = _next_timeout(
                        res, self._limit, self._safety_net_sec, slot_backoff,
                    )
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
                    self._slot_backoff_k = 0  # a hint: fresh, fast recheck
                except asyncio.TimeoutError:
                    self._slot_backoff_k = min(self._slot_backoff_k + 1, 16)
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
