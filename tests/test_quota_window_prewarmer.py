"""Keeping the 5-hour window ticking — decision, verification and bounds.

The value being protected: when the operator starts work, the window should
ALREADY be running, so their session spends the tail of one window and gets a
fresh one soon after, instead of anchoring a brand-new five hours at 08:30.

What is asserted here:
  * the trigger is telemetry, not a clock — "no reset_at in the future" is the
    only thing that means "no window is running";
  * an open window is never re-activated (that is the operator's own window);
  * every activation is VERIFIED by re-observing the provider, and an activation
    that does not produce a window counts as a failure;
  * the bounds hold: one per window, daily budget, minimum interval, and a
    circuit breaker that stops the mechanism rather than retrying into it.

No provider contact: the adapter is a fake, so nothing here can spend a token.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from src.services.quota_window_prewarmer import QuotaWindowPrewarmer


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def latest_snapshots(self):
        return self.rows


class _FakeAdapter:
    """Activation that opens a window in the fake store, exactly as the real one
    would be observed to."""

    provider = "claude"

    def __init__(self, store, *, ok=True, opens_window=True, window_hours=5):
        self.store = store
        self.ok = ok
        self.opens_window = opens_window
        self.window_hours = window_hours
        self.calls = 0

    async def activate(self, bucket_id="five_hour"):
        self.calls += 1
        if self.opens_window:
            self.store.rows = [_snapshot(reset_in_hours=self.window_hours)]
        return {"ok": self.ok, "usd": 0.001, "error": "" if self.ok else "TimeoutError"}


class _FakeCoordinator:
    def __init__(self, rows, **adapter_kw):
        self.store = _FakeStore(rows)
        self.adapters = [_FakeAdapter(self.store, **adapter_kw)]
        self.observations = 0

    async def observe_once(self):
        self.observations += 1


def _snapshot(*, reset_in_hours=None, used_percent=0.0, provider="claude",
              bucket_id="five_hour"):
    row = {
        "provider": provider, "bucket_id": bucket_id, "used_percent": used_percent,
        "limit_reached": 0, "observed_at": _iso(_now()),
        "telemetry_quality": "authoritative", "reset_at": None,
    }
    if reset_in_hours is not None:
        row["reset_at"] = _iso(_now() + timedelta(hours=reset_in_hours))
    return row


def _closed_window():
    """The live shape of "no window is running": the provider reports the bucket
    with no reset instant at all (observed on this host at 04:25Z 2026-08-19)."""
    return [_snapshot(reset_in_hours=None)]


def _mk(rows, **kw):
    coordinator = _FakeCoordinator(rows, **kw.pop("adapter", {}))
    return QuotaWindowPrewarmer(coordinator=coordinator, enabled=True, **kw), coordinator


# --------------------------------------------------------------------------- #
# 1. The decision                                                              #
# --------------------------------------------------------------------------- #

def test_no_open_window_is_the_only_activation_trigger():
    warmer, _ = _mk(_closed_window())
    assert warmer.decide(_closed_window()[0]).action == "activate"


def test_an_open_window_is_never_re_activated():
    """The operator's own usage already opened it — spending a turn buys nothing
    and the next decision point is just after it closes."""
    warmer, _ = _mk([_snapshot(reset_in_hours=2)])
    decision = warmer.decide(_snapshot(reset_in_hours=2))

    assert decision.action == "skip_window_open"
    assert decision.next_check_at is not None
    assert decision.next_check_at > _now() + timedelta(hours=1, minutes=55)


def test_an_elapsed_reset_counts_as_closed():
    """A reset instant in the past is a window that already ended, not an open
    one — this is what the tick right after a boundary sees."""
    warmer, _ = _mk([])
    assert warmer.decide(_snapshot(reset_in_hours=-1)).action == "activate"


def test_no_telemetry_never_activates_blind():
    warmer, _ = _mk([])
    assert warmer.decide(None).action == "skip_no_telemetry"


# --------------------------------------------------------------------------- #
# 2. Verification against the provider                                         #
# --------------------------------------------------------------------------- #

def test_activation_is_verified_by_re_observing():
    warmer, coord = _mk(_closed_window())

    decision = asyncio.run(warmer.tick_once())

    assert coord.adapters[0].calls == 1
    assert coord.observations == 2            # before deciding AND after acting
    assert decision.reason == "opened"
    assert warmer.state.consecutive_failures == 0
    assert warmer.read_status()["window_open"] is True


def test_a_turn_that_opens_no_window_is_a_failure_not_a_success():
    """The anchored-window premise is not assumed. If the provider took the turn
    and no window appeared, the mechanism is wrong about this account and must
    not keep firing on schedule."""
    warmer, coord = _mk(_closed_window(), adapter={"opens_window": False})

    asyncio.run(warmer.tick_once())

    assert warmer.state.last_outcome == "no_window_observed"
    assert warmer.state.consecutive_failures == 1


def test_a_failed_turn_counts_as_a_failure():
    warmer, _ = _mk(_closed_window(), adapter={"ok": False, "opens_window": False})

    asyncio.run(warmer.tick_once())

    assert warmer.state.last_outcome == "failed"
    assert warmer.state.consecutive_failures == 1


def test_repeated_failure_opens_the_circuit_and_stops_spending():
    warmer, coord = _mk(_closed_window(),
                        adapter={"ok": False, "opens_window": False},
                        min_interval_sec=0, max_consecutive_failures=2)

    asyncio.run(warmer.tick_once())
    asyncio.run(warmer.tick_once())
    calls_at_circuit = coord.adapters[0].calls
    asyncio.run(warmer.tick_once())            # must NOT spend again

    assert warmer.state.circuit_open is True
    assert coord.adapters[0].calls == calls_at_circuit
    assert warmer.decide(_closed_window()[0]).action == "skip_circuit_open"


# --------------------------------------------------------------------------- #
# 3. Bounds                                                                    #
# --------------------------------------------------------------------------- #

def test_min_interval_holds_back_a_second_activation():
    warmer, _ = _mk(_closed_window(), min_interval_sec=3600)
    warmer.state.last_activation_at = _now() - timedelta(minutes=5)

    assert warmer.decide(_closed_window()[0]).action == "skip_min_interval"


def test_daily_budget_is_enforced():
    warmer, _ = _mk(_closed_window(), max_per_day=2, min_interval_sec=0)
    warmer.state.activation_day = _now().date().isoformat()
    warmer.state.activations_today = 2

    assert warmer.decide(_closed_window()[0]).action == "skip_budget"


def test_the_daily_counter_rolls_over():
    warmer, _ = _mk(_closed_window(), max_per_day=1, min_interval_sec=0)
    warmer.state.activation_day = "2020-01-01"
    warmer.state.activations_today = 99

    assert warmer.decide(_closed_window()[0]).action == "activate"
    assert warmer.state.activations_today == 0


def test_disabled_prewarmer_starts_no_task_and_spends_nothing():
    coord = _FakeCoordinator(_closed_window())
    warmer = QuotaWindowPrewarmer(coordinator=coord, enabled=False)

    asyncio.run(warmer.start())

    assert warmer._task is None
    assert coord.adapters[0].calls == 0


def test_only_the_five_hour_bucket_is_considered():
    """The seven-day bucket is a budget, not a window that can be started early."""
    warmer, _ = _mk([_snapshot(reset_in_hours=40, bucket_id="seven_day")])
    assert warmer._latest_five_hour() is None


def test_sleep_delay_is_clamped_to_a_sane_range():
    warmer, _ = _mk(_closed_window())
    assert warmer._delay_until(_now() + timedelta(seconds=5)) == 60
    assert warmer._delay_until(_now() + timedelta(days=3)) == 6 * 60 * 60
    assert warmer._delay_until(None) == warmer.poll_interval_sec
