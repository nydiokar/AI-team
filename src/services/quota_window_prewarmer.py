"""Keep the Claude 5-hour subscription window TICKING, around the clock.

WHY THIS EXISTS
---------------
A Claude subscription window is anchored by first use: the five-hour clock starts
on the first chargeable interaction and reopens five hours later. So the moment
the operator starts serious work decides how much of that work fits before the
wall. Starting at 08:30 into a cold account means one window covers 08:30-13:30
and nothing resets until 13:30. If a window had already been opened at ~04:00,
the same 08:30 start lands 30 minutes before a reset — the operator spends the
tail of one window and gets a whole fresh one at 09:00. Two windows of headroom
for the same morning, at the cost of one near-zero-token turn.

That value only exists BEFORE work starts, which is why this runs unattended and
without quiet hours: a warm-up that waits for the operator to be awake is just
the operator pressing a button, and buys nothing.

WHAT IT IS NOT
--------------
Not a clock. ``docs/SESSION_WINDOW_WARMING_SPEC.md`` §2 is explicit that a
five-hour reset timestamp does NOT prove a five-hour anchored window, and
Anthropic has re-anchored limits before. So every decision here is made from a
FRESH observation of the provider's own telemetry, and every activation is
VERIFIED by re-observing afterwards:

    observe → is a window open? → (no) activate → observe again → did one open?

If the verification says no window opened, the activation is recorded as
ineffective and the prewarmer backs off instead of firing again on schedule. A
broken assumption shows up as a logged fact, not as silent token burn.

COST SHAPE
----------
One activation per window, at most: the schedule is derived from the provider's
own ``reset_at``, and an already-open window is skipped (the operator's own
usage opened it — nothing to do). That is ≤ ~5 turns/day of a haiku "Return only:
0" with no tools, no MCP, no project settings (see
``claude_usage_control.open_claude_window_with_minimal_turn``). Bounded twice
more by ``max_per_day`` and ``min_interval_sec``, and a consecutive-failure
circuit breaker stops it entirely rather than retrying into a provider incident.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from src.services.quota_window_coordinator import normalize_utc, utc_iso, utc_now

logger = logging.getLogger(__name__)

#: The bucket whose rhythm we keep. The seven-day bucket is a budget, not a
#: window you can start early, and the weekly scoped buckets are per-model.
FIVE_HOUR_BUCKET: str = "five_hour"


@dataclass
class PrewarmDecision:
    """One evaluation of "should a window be opened right now?" — the whole
    decision surface, so it can be asserted in tests without a provider."""

    action: str                      # activate | skip_window_open | skip_budget
                                     # | skip_min_interval | skip_circuit_open
                                     # | skip_no_telemetry
    reason: str = ""
    reset_at: Optional[datetime] = None
    next_check_at: Optional[datetime] = None


@dataclass
class PrewarmState:
    """Everything the scheduler remembers between ticks. Deliberately in-memory:
    a restart re-derives the truth from the provider on the next observation,
    which is strictly more trustworthy than anything we could persist."""

    activations_today: int = 0
    activation_day: str = ""
    last_activation_at: Optional[datetime] = None
    consecutive_failures: int = 0
    cost_spikes: int = 0
    circuit_open: bool = False
    circuit_reason: str = ""
    last_outcome: str = ""
    last_cost_percent: Optional[float] = None
    #: Newest ``reset_at`` seen for a window believed to still be running — the
    #: baseline for anchor-drift detection (spec §7's mandatory second reading).
    seen_reset_at: Optional[datetime] = None
    semantics_suspect: bool = False
    history: List[Dict[str, Any]] = field(default_factory=list)


class QuotaWindowPrewarmer:
    """Telemetry-driven, verify-every-time window keeper for ONE provider.

    Reads through the coordinator (its store for the latest observation, its
    adapter for a fresh one and for activation), so it owns no provider contact
    of its own — spec §15: the scheduler never parses provider output.
    """

    def __init__(
        self,
        *,
        coordinator: Any,
        enabled: bool = False,
        provider: str = "claude",
        min_interval_sec: int = 3600,
        max_per_day: int = 8,
        delay_after_reset_sec: int = 120,
        poll_interval_sec: int = 300,
        verify_delay_sec: int = 90,
        max_consecutive_failures: int = 3,
        max_activation_percent: float = 2.0,
        now: Callable[[], datetime] = utc_now,
        event_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.coordinator = coordinator
        self.enabled = enabled
        self.provider = provider
        self.min_interval_sec = max(0, int(min_interval_sec))
        self.max_per_day = max(0, int(max_per_day))
        self.delay_after_reset_sec = max(0, int(delay_after_reset_sec))
        self.poll_interval_sec = max(60, int(poll_interval_sec))
        #: Wait before the SECOND verification read when the first sees no window
        #: yet — the provider's telemetry trails an activation (see tick_once).
        self.verify_delay_sec = max(0, int(verify_delay_sec))
        self.max_consecutive_failures = max(1, int(max_consecutive_failures))
        #: Spec §13: prompt length is NOT a cost guarantee — an agent turn can
        #: silently load settings, MCP schemas and project rules. The only honest
        #: measure is the provider's own utilization delta across the activation.
        self.max_activation_percent = max(0.0, float(max_activation_percent))
        self._now = now
        self._event_sink = event_sink
        self.state = PrewarmState()
        self._task: Optional[asyncio.Task] = None

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled:
            logger.info("event=quota_prewarmer_disabled")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info(
                "event=quota_prewarmer_started min_interval=%ds max_per_day=%d",
                self.min_interval_sec, self.max_per_day,
            )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                delay = self.poll_interval_sec
                try:
                    decision = await self.tick_once()
                    delay = self._delay_until(decision.next_check_at)
                except Exception as e:                       # never kill the loop
                    logger.warning("event=quota_prewarmer_tick_failed err=%s", e)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("event=quota_prewarmer_stopped")
            raise

    def _delay_until(self, when: Optional[datetime]) -> int:
        """Sleep until the next decision point, clamped so a bad timestamp can
        never park the loop forever (or spin it)."""
        if when is None:
            return self.poll_interval_sec
        seconds = int((when - self._now()).total_seconds())
        return max(60, min(seconds, 6 * 60 * 60))

    # -- decision -----------------------------------------------------------
    def _latest_five_hour(self) -> Optional[Dict[str, Any]]:
        store = getattr(self.coordinator, "store", None)
        if store is None:
            return None
        try:
            reader = getattr(store, "latest_snapshots", None)
            rows = reader() if callable(reader) else (
                store.status().get("latest_snapshots") or []
            )
        except Exception:
            return None
        for row in rows:
            if (row.get("provider") == self.provider
                    and row.get("bucket_id") == FIVE_HOUR_BUCKET):
                return row
        return None

    def note_anchor_drift(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        """Spec §7/§16: falsify the anchored-window premise continuously.

        An ANCHORED window's boundary is fixed once the window starts — later
        interactions must not move it. A SLIDING one moves its boundary as usage
        continues, and prewarming a sliding window is worthless. The operator's
        own traffic supplies the "second request" the spec requires, so all we
        have to do is notice: a ``reset_at`` that moves FORWARD while the
        previously-seen boundary had not yet elapsed is drift.

        Returns True when drift was observed on this reading. Drift stops
        activation (circuit) rather than degrading it — "any ambiguity disables
        automation" (spec §18).
        """
        if snapshot is None:
            return False
        reset_at = normalize_utc(snapshot.get("reset_at"))
        if reset_at is None:
            self.state.seen_reset_at = None       # window closed — no baseline
            return False
        previous = self.state.seen_reset_at
        self.state.seen_reset_at = reset_at
        if previous is None:
            return False
        now = self._now()
        moved = (reset_at - previous).total_seconds()
        # Tolerance: the provider re-reports the same boundary with sub-second
        # jitter (observed: 12:30:00.057Z vs 12:30:00.157Z).
        if moved <= 60:
            return False
        if previous <= now:
            return False                          # the old window simply ended
        self.state.semantics_suspect = True
        self.state.circuit_open = True
        self.state.circuit_reason = "anchor_drift"
        self._emit("prewarm.anchor_drift", {
            "previous_reset_at": utc_iso(previous), "reset_at": utc_iso(reset_at),
            "moved_seconds": int(moved),
        })
        logger.warning(
            "event=quota_prewarm_anchor_drift previous=%s now=%s moved=%ds "
            "(window may not be first-use anchored; activation disabled)",
            utc_iso(previous), utc_iso(reset_at), int(moved),
        )
        return True

    def decide(self, snapshot: Optional[Dict[str, Any]]) -> PrewarmDecision:
        """Pure decision from ONE observation. No I/O — this is the part worth
        asserting exhaustively.

        A window is OPEN iff the provider reports a ``reset_at`` still in the
        future. The absence of a reset instant is exactly what "no window is
        running" looks like on the wire, and it is the only trigger to activate.
        """
        now = self._now()
        self._roll_day(now)

        if self.state.circuit_open:
            return PrewarmDecision(
                action="skip_circuit_open",
                reason=self.state.circuit_reason or "circuit_open",
                next_check_at=now + timedelta(seconds=self.poll_interval_sec),
            )
        if snapshot is None:
            return PrewarmDecision(
                action="skip_no_telemetry", reason="no_five_hour_snapshot",
                next_check_at=now + timedelta(seconds=self.poll_interval_sec),
            )

        reset_at = normalize_utc(snapshot.get("reset_at"))
        if reset_at is not None and reset_at > now:
            # Someone (usually the operator) is already inside a window. Nothing
            # to buy: come back just after it closes, which is the ONLY moment
            # opening a new one is both possible and worth a turn.
            return PrewarmDecision(
                action="skip_window_open", reason="window_already_open",
                reset_at=reset_at,
                next_check_at=reset_at + timedelta(seconds=self.delay_after_reset_sec),
            )

        if self.max_per_day and self.state.activations_today >= self.max_per_day:
            return PrewarmDecision(
                action="skip_budget",
                reason=f"daily_budget_spent:{self.state.activations_today}",
                next_check_at=self._next_utc_midnight(now),
            )
        last = self.state.last_activation_at
        if last is not None:
            earliest = last + timedelta(seconds=self.min_interval_sec)
            if earliest > now:
                return PrewarmDecision(
                    action="skip_min_interval", reason="min_interval_not_elapsed",
                    next_check_at=earliest,
                )
        return PrewarmDecision(action="activate", reason="no_open_window")

    def _roll_day(self, now: datetime) -> None:
        day = now.date().isoformat()
        if self.state.activation_day != day:
            self.state.activation_day = day
            self.state.activations_today = 0

    def _next_utc_midnight(self, now: datetime) -> datetime:
        return datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc,
        )

    # -- one full cycle -----------------------------------------------------
    async def tick_once(self) -> PrewarmDecision:
        """observe → decide → (activate → observe again → verify).

        The re-observation is the point of this method: it is what makes the
        prewarmer adapt to a provider that changed its window behaviour instead
        of firing on a clock that used to be right.
        """
        await self._observe()
        before = self._latest_five_hour()
        self.note_anchor_drift(before)
        decision = self.decide(before)
        if decision.action != "activate":
            self._emit(f"prewarm.{decision.action}", {
                "reason": decision.reason, "reset_at": utc_iso(decision.reset_at),
            })
            return decision

        adapter = self._adapter()
        if adapter is None:
            return PrewarmDecision(
                action="skip_no_telemetry", reason="no_activatable_adapter",
                next_check_at=self._now() + timedelta(seconds=self.poll_interval_sec),
            )
        if not await self._principal_known(adapter):
            # Spec §9A: without a known quota-owning identity we cannot tell
            # whose window we would be starting — observe only.
            self._emit("prewarm.skip_principal_unknown", {})
            return PrewarmDecision(
                action="skip_no_telemetry", reason="principal_unknown",
                next_check_at=self._now() + timedelta(seconds=self.poll_interval_sec),
            )

        percent_before = _percent(before)
        self._emit("prewarm.activation_started", {})
        result: Dict[str, Any] = await adapter.activate(FIVE_HOUR_BUCKET)
        now = self._now()
        self.state.last_activation_at = now
        self.state.activations_today += 1

        # VERIFY. The provider's own telemetry, read after the fact, is the only
        # acceptable evidence that a window actually opened.
        await self._observe()
        verified = self._latest_five_hour()
        opened_reset_at = normalize_utc((verified or {}).get("reset_at"))
        opened = opened_reset_at is not None and opened_reset_at > now
        if bool(result.get("ok")) and not opened:
            # The turn succeeded but no window is visible YET. Live on
            # 2026-08-19 that was pure telemetry lag — the boundary the read at
            # +4s could not see was there 15 minutes later — and scoring it as a
            # failure walks a healthy prewarmer toward a circuit breaker that
            # requires manual revalidation. Re-read once, after a bounded wait,
            # before judging. Free: `get_usage` is a control request, not a turn.
            await asyncio.sleep(self.verify_delay_sec)
            await self._observe()
            verified = self._latest_five_hour()
            opened_reset_at = normalize_utc((verified or {}).get("reset_at"))
            opened = opened_reset_at is not None and opened_reset_at > now
        self.state.seen_reset_at = opened_reset_at   # new baseline for drift

        # COST, measured the only honest way (spec §13): the provider's own
        # utilization delta across the activation, not the prompt we sent.
        cost_percent = _delta(percent_before, _percent(verified))
        self.state.last_cost_percent = cost_percent
        result = {**result, "cost_percent": cost_percent}
        if (cost_percent is not None and self.max_activation_percent > 0
                and cost_percent > self.max_activation_percent):
            self.state.cost_spikes += 1
            self._emit("prewarm.cost_exceeded", {
                "cost_percent": cost_percent, "limit": self.max_activation_percent,
            })
            logger.warning(
                "event=quota_prewarm_cost_exceeded delta_percent=%.2f limit=%.2f spikes=%d",
                cost_percent, self.max_activation_percent, self.state.cost_spikes,
            )
            if self.state.cost_spikes >= 2:
                # Spec §13: two unexplained spikes ⇒ open the circuit and require
                # manual revalidation. A "minimal" turn that is not minimal means
                # the activation environment is not what we think it is.
                self.state.circuit_open = True
                self.state.circuit_reason = "cost_exceeded"
                self._emit("prewarm.circuit_opened", {"reason": "cost_exceeded"})

        if bool(result.get("ok")) and opened:
            self.state.consecutive_failures = 0
            self.state.last_outcome = "opened"
            self._record(now, "opened", result, opened_reset_at)
            self._emit("prewarm.activation_succeeded", {
                "usd": result.get("usd"), "reset_at": utc_iso(opened_reset_at),
            })
            logger.info(
                "event=quota_prewarm_window_opened reset_at=%s usd=%s",
                utc_iso(opened_reset_at), result.get("usd"),
            )
            return PrewarmDecision(
                action="activate", reason="opened", reset_at=opened_reset_at,
                next_check_at=opened_reset_at + timedelta(seconds=self.delay_after_reset_sec),
            )

        # Either the turn failed, or it succeeded and NO window appeared — which
        # would mean the anchored-window premise no longer holds. Both are
        # failures of this mechanism and must not be retried on schedule.
        self.state.consecutive_failures += 1
        self.state.last_outcome = "failed" if not result.get("ok") else "no_window_observed"
        self._record(now, self.state.last_outcome, result, opened_reset_at)
        if self.state.consecutive_failures >= self.max_consecutive_failures:
            self.state.circuit_open = True
            self.state.circuit_reason = "consecutive_failures"
            self._emit("prewarm.circuit_opened", {
                "failures": self.state.consecutive_failures,
                "last_outcome": self.state.last_outcome,
            })
            logger.warning(
                "event=quota_prewarm_circuit_opened failures=%d outcome=%s",
                self.state.consecutive_failures, self.state.last_outcome,
            )
        else:
            self._emit("prewarm.activation_failed", {
                "outcome": self.state.last_outcome, "error": result.get("error"),
            })
        return PrewarmDecision(
            action="activate", reason=self.state.last_outcome,
            next_check_at=now + timedelta(seconds=max(self.poll_interval_sec, 900)),
        )

    async def _observe(self) -> None:
        """Refresh telemetry through the coordinator. Free: ``get_usage`` is a
        control request, not a model turn."""
        observe = getattr(self.coordinator, "observe_once", None)
        if observe is None:
            return
        try:
            await observe()
        except Exception as e:
            logger.debug("event=quota_prewarm_observe_failed err=%s", e)

    async def _principal_known(self, adapter: Any) -> bool:
        """Spec §9A: activation requires a known quota-owning identity. The read
        is cached inside the adapter, so this costs nothing."""
        identify = getattr(adapter, "identify_principal", None)
        if identify is None:
            return True
        try:
            principal = await identify()
        except Exception:
            return False
        label = str(getattr(principal, "label", "") or "")
        return bool(label) and label != "principal_unknown"

    def _adapter(self) -> Optional[Any]:
        for adapter in getattr(self.coordinator, "adapters", []) or []:
            if getattr(adapter, "provider", "") != self.provider:
                continue
            if callable(getattr(adapter, "activate", None)):
                return adapter
        return None

    # -- observability ------------------------------------------------------
    def _record(
        self, when: datetime, outcome: str, result: Dict[str, Any],
        reset_at: Optional[datetime],
    ) -> None:
        self.state.history.append({
            "at": utc_iso(when), "outcome": outcome, "usd": result.get("usd"),
            "cost_percent": result.get("cost_percent"),
            "reset_at": utc_iso(reset_at), "error": result.get("error") or "",
        })
        del self.state.history[:-50]

    def _emit(self, name: str, payload: Dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(name, {"provider": self.provider, **payload})
        except Exception:
            pass

    def read_status(self) -> Dict[str, Any]:
        """Read-only status for the operator (Control API / diagnostics)."""
        snapshot = self._latest_five_hour()
        reset_at = normalize_utc((snapshot or {}).get("reset_at"))
        now = self._now()
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "window_open": bool(reset_at is not None and reset_at > now),
            "reset_at": utc_iso(reset_at),
            "observed_at": (snapshot or {}).get("observed_at"),
            "used_percent": (snapshot or {}).get("used_percent"),
            "activations_today": self.state.activations_today,
            "last_activation_at": utc_iso(self.state.last_activation_at),
            "last_outcome": self.state.last_outcome,
            "last_cost_percent": self.state.last_cost_percent,
            "consecutive_failures": self.state.consecutive_failures,
            "cost_spikes": self.state.cost_spikes,
            "circuit_open": self.state.circuit_open,
            "circuit_reason": self.state.circuit_reason,
            # False until something falsifies the anchored-window premise; the
            # spec's classification question, answered continuously.
            "semantics_suspect": self.state.semantics_suspect,
            "history": list(self.state.history[-10:]),
        }


def _percent(snapshot: Optional[Dict[str, Any]]) -> Optional[float]:
    value = (snapshot or {}).get("used_percent")
    return float(value) if isinstance(value, (int, float)) else None


def _delta(before: Optional[float], after: Optional[float]) -> Optional[float]:
    """Utilization consumed by the activation. A window that did not exist before
    reads as 0%, so a missing ``before`` is treated as zero — but a missing
    ``after`` yields None rather than a fabricated number."""
    if after is None:
        return None
    return round(after - (before if before is not None else 0.0), 3)


def build_prewarmer_from_config(
    *, coordinator: Any, enabled: bool,
    event_sink: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> QuotaWindowPrewarmer:
    from config.settings import config

    quota = getattr(config, "quota", None)
    return QuotaWindowPrewarmer(
        coordinator=coordinator,
        enabled=enabled,
        min_interval_sec=int(getattr(quota, "prewarm_min_interval_sec", 3600)),
        max_per_day=int(getattr(quota, "prewarm_max_per_day", 8)),
        delay_after_reset_sec=int(getattr(quota, "prewarm_delay_after_reset_sec", 120)),
        max_activation_percent=float(getattr(quota, "prewarm_max_activation_percent", 2.0)),
        poll_interval_sec=int(getattr(quota, "observe_interval_sec", 300)),
        event_sink=event_sink,
    )
