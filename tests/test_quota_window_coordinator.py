from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from src.services.quota_window_coordinator import (
    AdapterCapability,
    ClaudeGetUsageQuotaAdapter,
    FakeQuotaAdapter,
    QuotaBucket,
    QuotaAdapterError,
    QuotaPrincipal,
    QuotaSnapshot,
    QuotaWindowCoordinator,
    QuotaWindowStore,
    TelemetryQuality,
    UnsupportedQuotaAdapter,
    WindowSemantics,
    build_default_quota_adapters,
    utc_iso,
)
from src.services.quota_digest import QuotaTelegramDigestSubscriber


def _store(tmp_path):
    return QuotaWindowStore(tmp_path / "quota.db")


def _snapshot(*, provider="fake", bucket="five-hour", principal="fake-principal", observed=None, used=12.5, reset=None, quality=TelemetryQuality.AUTHORITATIVE):
    return QuotaSnapshot(
        provider=provider,
        bucket_id=bucket,
        principal_hash=principal,
        observed_at=observed or datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc),
        telemetry_quality=quality,
        used_percent=used,
        reset_at=reset or datetime(2026, 6, 23, 13, 0, tzinfo=timezone.utc),
        limit_reached=False,
        raw_status="observed",
    )


def _get_usage_response(
    *,
    five: float | None = 2.0,
    five_reset: str | None = "2026-08-17T02:20:00.119493+00:00",
    seven: float | None = 40.0,
    seven_reset: str | None = "2026-08-20T22:00:00.119518+00:00",
    available: bool = True,
) -> dict:
    return {
        "subscription_type": "max",
        "rate_limits_available": available,
        "rate_limits": {
            "five_hour": {"utilization": five, "resets_at": five_reset},
            "seven_day": {"utilization": seven, "resets_at": seven_reset},
            "limits": [
                {
                    "kind": "session",
                    "group": "session",
                    "percent": five,
                    "resets_at": five_reset,
                    "scope": None,
                    "is_active": False,
                },
                {
                    "kind": "weekly_all",
                    "group": "weekly",
                    "percent": seven,
                    "resets_at": seven_reset,
                    "scope": None,
                    "is_active": True,
                },
                {
                    "kind": "weekly_scoped",
                    "group": "weekly",
                    "percent": 5,
                    "resets_at": seven_reset,
                    "scope": {"model": {"id": None, "display_name": "Fable"}},
                    "is_active": False,
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_fake_adapter_observation_records_status(tmp_path):
    store = _store(tmp_path)
    adapter = FakeQuotaAdapter(snapshots={"five-hour": [_snapshot()]})
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True)

    await coord.observe_once()

    status = coord.read_status()
    assert status["mode"] == "observe_only"
    assert status["adapters"][0]["status"] == "ready"
    assert status["latest_snapshots"][0]["used_percent"] == 12.5


@pytest.mark.asyncio
async def test_duplicate_snapshot_handling_is_idempotent(tmp_path):
    store = _store(tmp_path)
    snap = _snapshot()
    adapter = FakeQuotaAdapter(snapshots={"five-hour": [snap, snap]})
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True)

    await coord.observe_once()
    await coord.observe_once()

    rows = store._conn().execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    events = [r[0] for r in store._conn().execute("SELECT event_name FROM coordinator_events").fetchall()]
    assert rows == 1
    assert "quota.duplicate_snapshot" in events


def test_concurrent_reads_and_writes(tmp_path):
    store = _store(tmp_path)
    base = datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc)
    snaps = [_snapshot(observed=base + timedelta(seconds=i), used=float(i)) for i in range(20)]
    adapter = FakeQuotaAdapter(snapshots={"five-hour": snaps})
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True)

    def writer():
        for _ in range(20):
            asyncio.run(coord.observe_once())

    def reader():
        for _ in range(20):
            coord.read_status()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(writer), pool.submit(reader), pool.submit(reader)]
        for future in futures:
            future.result(timeout=10)
    assert coord.read_status()["latest_snapshots"]


@pytest.mark.asyncio
async def test_restart_recovery_reads_persisted_state(tmp_path):
    db_path = tmp_path / "quota.db"
    store1 = QuotaWindowStore(db_path)
    coord1 = QuotaWindowCoordinator(
        store=store1,
        adapters=[FakeQuotaAdapter(snapshots={"five-hour": [_snapshot(used=41.0)]})],
        enabled=True,
    )
    await coord1.observe_once()
    store1.close()

    store2 = QuotaWindowStore(db_path)
    status = QuotaWindowCoordinator(store=store2, adapters=[], enabled=False).read_status()
    assert status["latest_snapshots"][0]["used_percent"] == 41.0


@pytest.mark.asyncio
async def test_unavailable_telemetry_is_explicit(tmp_path):
    store = _store(tmp_path)
    adapter = UnsupportedQuotaAdapter("opencode", "opencode_is_provider_router_no_phase1_quota_owner")
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True)

    await coord.observe_once()

    status = coord.read_status()
    assert status["adapters"][0]["status"] == "unavailable"
    assert status["latest_snapshots"][0]["telemetry_quality"] == "unsupported"
    assert status["latest_snapshots"][0]["unavailable_reason"]


@pytest.mark.asyncio
async def test_adapter_version_mismatch_disables_adapter(tmp_path):
    store = _store(tmp_path)
    cap = AdapterCapability(
        provider="fake",
        adapter_version="fake-2",
        schema_version="quota-v2",
        can_observe=True,
        telemetry_quality=TelemetryQuality.AUTHORITATIVE,
    )
    adapter = FakeQuotaAdapter(capability=cap, snapshots={"five-hour": [_snapshot()]})
    coord = QuotaWindowCoordinator(
        store=store,
        adapters=[adapter],
        enabled=True,
        expected_schema_versions={"fake": "quota-v1"},
    )

    await coord.observe_once()

    status = coord.read_status()
    assert status["adapters"][0]["enabled"] == 0
    assert status["adapters"][0]["reason"] == "version_mismatch"
    assert status["latest_snapshots"] == []


@pytest.mark.asyncio
async def test_malformed_provider_response_becomes_unavailable_snapshot(tmp_path):
    store = _store(tmp_path)
    adapter = FakeQuotaAdapter(malformed=True)
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True)

    await coord.observe_once()

    snapshot = coord.read_status()["latest_snapshots"][0]
    assert snapshot["telemetry_quality"] == "malformed"
    assert snapshot["unavailable_reason"] == "malformed_provider_response"


def test_timezone_conversion_stores_utc(tmp_path):
    store = _store(tmp_path)
    snap = _snapshot(
        observed=datetime(2026, 6, 23, 10, 0, tzinfo=timezone(timedelta(hours=2))),
        reset=datetime(2026, 6, 23, 15, 0, tzinfo=timezone(timedelta(hours=2))),
    )
    assert store.insert_snapshot(snap)
    row = store.status()["latest_snapshots"][0]
    assert row["observed_at"] == "2026-06-23T08:00:00Z"
    assert row["reset_at"] == "2026-06-23T13:00:00Z"
    assert utc_iso(datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc)) == row["observed_at"]


def test_status_returns_single_latest_snapshot_per_bucket_when_observed_at_ties(tmp_path):
    store = _store(tmp_path)
    observed = datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc)
    assert store.insert_snapshot(_snapshot(observed=observed, used=10.0, reset=datetime(2026, 6, 23, 13, 0, tzinfo=timezone.utc)))
    assert store.insert_snapshot(_snapshot(observed=observed, used=11.0, reset=datetime(2026, 6, 23, 14, 0, tzinfo=timezone.utc)))

    snapshots = store.status()["latest_snapshots"]

    assert len(snapshots) == 1
    assert snapshots[0]["used_percent"] == 11.0


@pytest.mark.asyncio
async def test_clock_rollback_records_event_without_inferring_reset(tmp_path):
    store = _store(tmp_path)
    clock_values = [
        datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 23, 7, 59, tzinfo=timezone.utc),
    ]

    def fake_now():
        if clock_values:
            return clock_values.pop(0)
        return datetime(2026, 6, 23, 7, 59, tzinfo=timezone.utc)

    adapter = FakeQuotaAdapter(snapshots={"five-hour": [_snapshot(), _snapshot(observed=datetime(2026, 6, 23, 8, 1, tzinfo=timezone.utc))]})
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True, now=fake_now)

    await coord.observe_once()
    await coord.observe_once()

    events = [r[0] for r in store._conn().execute("SELECT event_name FROM coordinator_events").fetchall()]
    assert "quota.clock_rollback" in events


@pytest.mark.asyncio
async def test_disabled_by_default_lifecycle_does_not_observe(tmp_path):
    store = _store(tmp_path)
    adapter = FakeQuotaAdapter(snapshots={"five-hour": [_snapshot()]})
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=False)

    await coord.start()
    await asyncio.sleep(0)
    await coord.stop()

    assert coord.read_status()["latest_snapshots"] == []


@pytest.mark.asyncio
async def test_observation_never_invokes_model(tmp_path):
    store = _store(tmp_path)
    adapter = FakeQuotaAdapter(snapshots={"five-hour": [_snapshot()]})
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True)

    await coord.observe_once()

    assert adapter.model_invocations == 0


@pytest.mark.asyncio
async def test_claude_get_usage_adapter_records_canonical_buckets_and_scoped_limits(tmp_path):
    now = datetime(2026, 8, 16, 22, 51, tzinfo=timezone.utc)
    calls = 0

    async def read_usage():
        nonlocal calls
        calls += 1
        return _get_usage_response()

    adapter = ClaudeGetUsageQuotaAdapter(
        principal_key="claude-max-nyd",
        read_usage=read_usage,
        claude_code_version_value="2.1.191 (Claude Code)",
        now=lambda: now,
    )
    store = _store(tmp_path)
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True, now=lambda: now)

    await coord.observe_once()

    snapshots = {row["bucket_id"]: row for row in coord.read_status()["latest_snapshots"]}
    assert snapshots["five_hour"]["used_percent"] == 2.0
    assert snapshots["five_hour"]["reset_at"] == "2026-08-17T02:20:00.119493Z"
    assert snapshots["five_hour"]["raw_status"].startswith("claude_get_usage sdk=")
    assert snapshots["seven_day"]["used_percent"] == 40.0
    assert snapshots["weekly_scoped:fable"]["used_percent"] == 5.0
    assert snapshots["weekly_scoped:fable"]["reset_at"] == "2026-08-20T22:00:00.119518Z"
    assert calls == 1


@pytest.mark.asyncio
async def test_claude_get_usage_adapter_preserves_zero_and_unknown_distinctly(tmp_path):
    now = datetime(2026, 8, 16, 22, 51, tzinfo=timezone.utc)

    async def read_usage():
        return _get_usage_response(five=0.0, five_reset=None, seven=None)

    adapter = ClaudeGetUsageQuotaAdapter(
        principal_key="claude-max-nyd",
        read_usage=read_usage,
        now=lambda: now,
    )
    snapshots = {}
    for bucket in await adapter.discover_buckets():
        snapshots[bucket.bucket_id] = await adapter.observe(bucket.bucket_id)

    assert snapshots["five_hour"].used_percent == 0.0
    assert snapshots["five_hour"].reset_at is None
    assert snapshots["five_hour"].telemetry_quality == TelemetryQuality.PARTIAL
    assert snapshots["seven_day"].used_percent is None
    assert snapshots["seven_day"].telemetry_quality == TelemetryQuality.PARTIAL


@pytest.mark.asyncio
async def test_claude_get_usage_adapter_failure_is_unavailable_not_zero(tmp_path):
    now = datetime(2026, 8, 16, 22, 51, tzinfo=timezone.utc)

    async def read_usage():
        raise RuntimeError("boom")

    adapter = ClaudeGetUsageQuotaAdapter(
        principal_key="claude-max-nyd",
        read_usage=read_usage,
        now=lambda: now,
    )

    snapshot = await adapter.observe("five_hour")

    assert snapshot.used_percent is None
    assert snapshot.reset_at is None
    assert snapshot.telemetry_quality == TelemetryQuality.UNAVAILABLE
    assert snapshot.unavailable_reason.startswith("claude_get_usage_failed:")


@pytest.mark.asyncio
async def test_claude_get_usage_adapter_returns_last_success_as_stale_after_failure(tmp_path):
    now = datetime(2026, 8, 16, 22, 51, tzinfo=timezone.utc)
    fail = False

    async def read_usage():
        if fail:
            raise RuntimeError("boom")
        return _get_usage_response(five=10.0)

    adapter = ClaudeGetUsageQuotaAdapter(
        principal_key="claude-max-nyd",
        read_usage=read_usage,
        now=lambda: now,
    )

    first = await adapter.observe("five_hour")
    fail = True
    adapter._cached_snapshot_at = 0.0
    second = await adapter.observe("five_hour")

    assert first.used_percent == 10.0
    assert second.used_percent == 10.0
    assert second.reset_at == first.reset_at
    assert second.telemetry_quality == TelemetryQuality.PARTIAL
    assert second.unavailable_reason.startswith("claude_get_usage_stale_after_failure:")


@pytest.mark.asyncio
async def test_claude_get_usage_adapter_returns_last_success_as_stale_after_empty_rate_limits(tmp_path):
    now = datetime(2026, 8, 16, 22, 51, tzinfo=timezone.utc)
    empty = False

    async def read_usage():
        if empty:
            return {
                "subscription_type": "max",
                "rate_limits_available": True,
                "rate_limits": None,
            }
        return _get_usage_response(five=10.0)

    adapter = ClaudeGetUsageQuotaAdapter(
        principal_key="claude-max-nyd",
        read_usage=read_usage,
        now=lambda: now,
    )

    first = await adapter.observe("five_hour")
    empty = True
    adapter._cached_snapshot_at = 0.0
    second = await adapter.observe("five_hour")

    assert first.used_percent == 10.0
    assert second.used_percent == 10.0
    assert second.reset_at == first.reset_at
    assert second.telemetry_quality == TelemetryQuality.PARTIAL
    assert second.unavailable_reason.startswith("claude_get_usage_stale_after_unavailable:")


@pytest.mark.asyncio
async def test_claude_get_usage_adapter_rate_limits_unavailable_is_not_zero(tmp_path):
    now = datetime(2026, 8, 16, 22, 51, tzinfo=timezone.utc)

    async def read_usage():
        return _get_usage_response(available=False)

    adapter = ClaudeGetUsageQuotaAdapter(
        principal_key="claude-max-nyd",
        read_usage=read_usage,
        now=lambda: now,
    )

    snapshot = await adapter.observe("five_hour")

    assert snapshot.used_percent is None
    assert snapshot.reset_at is None
    assert snapshot.telemetry_quality == TelemetryQuality.UNAVAILABLE
    assert snapshot.unavailable_reason == "claude_rate_limits_unavailable_or_empty"


def test_default_claude_quota_adapter_is_get_usage_not_statusline(monkeypatch):
    from config import config

    monkeypatch.setattr(config.quota, "claude_principal_key", "principal")
    adapters = build_default_quota_adapters()

    claude_adapters = [a for a in adapters if getattr(a, "provider", "") == "claude"]
    assert claude_adapters
    assert all(isinstance(adapter, ClaudeGetUsageQuotaAdapter) for adapter in claude_adapters)
    assert all("statusline" not in getattr(a, "adapter_version", "") for a in claude_adapters)


def test_old_statusline_snapshot_cannot_override_canonical_get_usage(tmp_path):
    now = datetime(2026, 8, 16, 22, 51, tzinfo=timezone.utc)
    store = _store(tmp_path)
    store.upsert_principal(QuotaPrincipal(provider="claude", principal_hash="principal", label="principal", authentication_mode="test"))
    store.upsert_bucket(
        QuotaBucket(
            provider="claude",
            bucket_id="five_hour",
            bucket_name="Claude 5-hour session limit",
            principal_hash="principal",
            window_semantics=WindowSemantics.ANCHORED,
            telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            window_duration_seconds=5 * 60 * 60,
        ),
        "principal",
    )
    store.insert_snapshot(
        QuotaSnapshot(
            provider="claude",
            principal_hash="principal",
            bucket_id="five_hour",
            observed_at=now - timedelta(minutes=2),
            telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            used_percent=88.0,
            reset_at=datetime(2026, 8, 17, 1, 20, tzinfo=timezone.utc),
            raw_status="claude_status_line",
        )
    )
    store.insert_snapshot(
        QuotaSnapshot(
            provider="claude",
            principal_hash="principal",
            bucket_id="five_hour",
            observed_at=now,
            telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            used_percent=2.0,
            reset_at=datetime(2026, 8, 17, 2, 20, tzinfo=timezone.utc),
            raw_status="claude_get_usage sdk=0.2.110 claude_code=2.1.191",
        )
    )

    latest = store.status(now=now)["latest_snapshots"][0]

    assert latest["used_percent"] == 2.0
    assert latest["reset_at"] == "2026-08-17T02:20:00Z"
    assert latest["raw_status"].startswith("claude_get_usage")


@pytest.mark.asyncio
async def test_coordinator_retains_persisted_get_usage_success_as_stale(tmp_path):
    now = datetime(2026, 8, 16, 22, 51, tzinfo=timezone.utc)
    store = _store(tmp_path)
    principal = QuotaPrincipal(provider="claude", principal_hash="principal", label="principal", authentication_mode="test")
    store.upsert_principal(principal)
    store.upsert_bucket(
        QuotaBucket(
            provider="claude",
            bucket_id="five_hour",
            bucket_name="Claude 5-hour session limit",
            principal_hash="principal",
            window_semantics=WindowSemantics.ANCHORED,
            telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            window_duration_seconds=5 * 60 * 60,
        ),
        "principal",
    )
    store.insert_snapshot(
        QuotaSnapshot(
            provider="claude",
            principal_hash="principal",
            bucket_id="five_hour",
            observed_at=now - timedelta(minutes=20),
            telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            used_percent=12.0,
            reset_at=datetime(2026, 8, 17, 2, 20, tzinfo=timezone.utc),
            raw_status="claude_get_usage sdk=0.2.110 claude_code=2.1.191",
        )
    )

    class EmptyGetUsageAdapter:
        async def capabilities(self):
            return AdapterCapability(
                provider="claude",
                adapter_version="claude-get-usage-v1",
                schema_version="claude-get-usage-rate-limits-v1",
                can_observe=True,
                telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            )

        async def identify_principal(self):
            return principal

        async def discover_buckets(self):
            return [
                QuotaBucket(
                    provider="claude",
                    bucket_id="five_hour",
                    bucket_name="Claude 5-hour session limit",
                    principal_hash="principal",
                    window_semantics=WindowSemantics.ANCHORED,
                    telemetry_quality=TelemetryQuality.AUTHORITATIVE,
                    window_duration_seconds=5 * 60 * 60,
                )
            ]

        async def observe(self, bucket_id):
            return QuotaSnapshot(
                provider="claude",
                principal_hash="principal",
                bucket_id=bucket_id,
                observed_at=now,
                telemetry_quality=TelemetryQuality.UNAVAILABLE,
                raw_status="claude_get_usage sdk=0.2.110 claude_code=2.1.191",
                unavailable_reason="claude_rate_limits_unavailable_or_empty",
            )

        async def detect_active_user_session(self):
            return None

    coord = QuotaWindowCoordinator(store=store, adapters=[EmptyGetUsageAdapter()], enabled=True, now=lambda: now)

    await coord.observe_once()

    latest = coord.read_status()["latest_snapshots"][0]
    state = coord.read_status()["window_states"][0]
    assert latest["used_percent"] == 12.0
    assert latest["reset_at"] == "2026-08-17T02:20:00Z"
    assert latest["telemetry_quality"] == "partial"
    assert latest["unavailable_reason"].startswith("claude_get_usage_stale_after_unavailable:")
    assert state["telemetry_state"] == "stale"


def test_adaptive_cadence_backs_off_until_reset_probe_window(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    store.insert_snapshot(
        _snapshot(
            observed=now,
            reset=datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc),
            used=12.0,
        )
    )
    coord = QuotaWindowCoordinator(
        store=store,
        adapters=[],
        enabled=True,
        observe_interval_sec=300,
        observe_max_interval_sec=21600,
        reset_probe_lead_sec=900,
        now=lambda: now,
    )

    assert coord.next_observe_delay_sec() == 17100


def test_adaptive_cadence_uses_tight_polling_when_limit_reached(tmp_path):
    """Spent, and the boundary it names has already passed: nothing tells us when
    the window reopens, so poll at the base interval."""
    store = _store(tmp_path)
    snap = _snapshot(used=100.0, reset=datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc))
    store.insert_snapshot(QuotaSnapshot(**{**snap.__dict__, "limit_reached": True}))
    coord = QuotaWindowCoordinator(store=store, adapters=[], enabled=True, observe_interval_sec=300)

    assert coord.next_observe_delay_sec() == 300


def test_spent_window_sleeps_to_its_own_reset_instead_of_re_asking(tmp_path):
    """THE waste this fixes. LIVE 2026-08-19: the five-hour bucket read
    `limit_reached=1, reset_at=17:20:00Z` and the observer then re-asked every
    60s — 16 consecutive probes, each spawning a Claude CLI subprocess — to
    re-learn an instant it already had. The provider named the boundary; sleep to
    it (plus a settle margin, since the read ON the boundary still shows the old
    window)."""
    now = datetime(2026, 8, 19, 17, 5, tzinfo=timezone.utc)
    store = _store(tmp_path)
    snap = _snapshot(
        observed=now, used=100.0,
        reset=datetime(2026, 8, 19, 17, 20, tzinfo=timezone.utc),
    )
    store.insert_snapshot(QuotaSnapshot(**{**snap.__dict__, "limit_reached": True}))
    coord = QuotaWindowCoordinator(
        store=store, adapters=[], enabled=True,
        observe_interval_sec=60, now=lambda: now,
    )

    assert coord.next_observe_delay_sec() == 15 * 60 + 30


def test_spent_window_with_no_known_reset_still_polls(tmp_path):
    """No boundary on record ⇒ the only way to learn the window reopened is to
    ask; the base interval stays."""
    store = _store(tmp_path)
    now = datetime(2026, 8, 19, 17, 5, tzinfo=timezone.utc)
    snap = _snapshot(observed=now, used=100.0)
    store.insert_snapshot(
        QuotaSnapshot(**{**snap.__dict__, "limit_reached": True, "reset_at": None})
    )
    coord = QuotaWindowCoordinator(
        store=store, adapters=[], enabled=True,
        observe_interval_sec=60, now=lambda: now,
    )

    assert coord.next_observe_delay_sec() == 60


@pytest.mark.asyncio
async def test_read_status_blocks_current_unclassified_window_from_automation(tmp_path):
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    adapter = FakeQuotaAdapter(
        provider="claude",
        principal_hash="principal",
        snapshots={
            "five-hour": [
                _snapshot(
                    provider="claude",
                    bucket="five-hour",
                    principal="principal",
                    observed=now,
                    used=12.0,
                    reset=datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc),
                )
            ]
        },
    )
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True, now=lambda: now)

    await coord.observe_once()

    state = coord.read_status()["window_states"][0]
    assert state["telemetry_state"] == "current"
    assert state["window_end_at"] == "2026-07-29T13:00:00Z"
    assert state["window_start_at"] is None
    assert state["window_known"] is False
    assert state["automation_ready"] is False
    assert "window_semantics_unclassified" in state["blockers"]


def test_read_status_marks_stale_reset_state_not_actionable(tmp_path):
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    adapter = FakeQuotaAdapter(
        provider="claude",
        principal_hash="principal",
        snapshots={
            "five-hour": [
                QuotaSnapshot(
                    provider="claude",
                    bucket_id="five-hour",
                    principal_hash="principal",
                    observed_at=now,
                    telemetry_quality=TelemetryQuality.PARTIAL,
                    used_percent=18.0,
                    reset_at=None,
                    raw_status="status_line",
                    unavailable_reason="reset_at_stale",
                )
            ]
        },
    )
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True, now=lambda: now)

    asyncio.run(coord.observe_once())

    state = coord.read_status()["window_states"][0]
    assert state["telemetry_state"] == "stale"
    assert state["window_end_at"] is None
    assert state["window_known"] is False
    assert state["automation_ready"] is False
    assert "reset_at_stale" in state["blockers"]


def test_read_status_marks_old_observation_stale_not_actionable(tmp_path):
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    store.upsert_principal(QuotaPrincipal(provider="claude", principal_hash="principal", label="principal", authentication_mode="test"))
    store.upsert_bucket(
        QuotaBucket(
            provider="claude",
            bucket_id="five_hour",
            bucket_name="Claude 5-hour session limit",
            principal_hash="principal",
            window_semantics=WindowSemantics.ANCHORED,
            telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            window_duration_seconds=5 * 60 * 60,
        ),
        "principal",
    )
    store.insert_snapshot(
        QuotaSnapshot(
            provider="claude",
            principal_hash="principal",
            bucket_id="five_hour",
            observed_at=now - timedelta(minutes=16),
            telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            used_percent=18.0,
            reset_at=datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc),
            raw_status="claude_get_usage sdk=0.2.110 claude_code=2.1.191",
        )
    )

    state = store.status(now=now)["window_states"][0]

    assert state["telemetry_state"] == "stale"
    assert state["window_known"] is False
    assert state["automation_ready"] is False
    assert "observation_stale" in state["blockers"]


def test_read_status_tracks_reset_boundary_history(tmp_path):
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    store.insert_snapshot(
        _snapshot(
            observed=datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc),
            reset=datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc),
            used=10.0,
        )
    )
    store.insert_snapshot(
        _snapshot(
            observed=datetime(2026, 7, 29, 7, 0, tzinfo=timezone.utc),
            reset=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
            used=1.0,
        )
    )
    store.insert_snapshot(
        _snapshot(
            observed=datetime(2026, 7, 29, 7, 30, tzinfo=timezone.utc),
            reset=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
            used=2.0,
        )
    )

    state = store.status(now=now)["window_states"][0]

    assert state["observed_reset_count"] == 2
    assert state["classification_status"] == "collecting"
    assert state["current_reset_observed_since"] == "2026-07-29T07:00:00Z"
    assert state["last_reset_change_at"] == "2026-07-29T07:00:00Z"
    assert state["reset_boundary_evidence"][0]["reset_at"] == "2026-07-29T12:00:00Z"
    assert state["reset_boundary_evidence"][0]["first_used_percent"] == 1.0
    assert state["reset_boundary_evidence"][0]["last_used_percent"] == 2.0


def test_single_reset_timestamp_cannot_classify_anchored(tmp_path):
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    store.insert_snapshot(
        _snapshot(
            observed=datetime(2026, 7, 29, 7, 0, tzinfo=timezone.utc),
            reset=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
            used=1.0,
        )
    )

    state = store.status(now=now)["window_states"][0]

    assert state["observed_reset_count"] == 1
    assert state["classification_status"] == "collecting"
    assert state["window_semantics"] == "unknown"
    assert state["automation_ready"] is False


@pytest.mark.asyncio
async def test_read_status_marks_anchored_current_window_as_preautomation_candidate(tmp_path):
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    adapter = FakeQuotaAdapter(
        provider="claude",
        principal_hash="principal",
        buckets=[
            QuotaBucket(
                provider="claude",
                bucket_id="five-hour",
                bucket_name="Five hour",
                principal_hash="principal",
                window_semantics=WindowSemantics.ANCHORED,
                telemetry_quality=TelemetryQuality.AUTHORITATIVE,
                window_duration_seconds=5 * 60 * 60,
            )
        ],
        snapshots={
            "five-hour": [
                _snapshot(
                    provider="claude",
                    bucket="five-hour",
                    principal="principal",
                    observed=now,
                    used=12.0,
                    reset=datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc),
                )
            ]
        },
    )
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True, now=lambda: now)

    await coord.observe_once()

    state = coord.read_status()["window_states"][0]
    assert state["telemetry_state"] == "current"
    assert state["window_start_at"] == "2026-07-29T08:00:00Z"
    assert state["window_start_inferred_at"] == "2026-07-29T08:00:00Z"
    assert state["window_start_source"] == "inferred_from_reset_duration"
    assert state["window_end_at"] == "2026-07-29T13:00:00Z"
    assert state["active_session_state"] == "unknown"
    assert state["window_known"] is True
    assert state["automation_ready"] is False
    assert state["blockers"] == ["active_session_state_unknown"]

@pytest.mark.asyncio
async def test_quota_digest_subscriber_aggregates_and_uses_notifier():
    class FakeNotifier:
        def __init__(self) -> None:
            self.messages = []

        async def notify_quota_digest(self, message, *, chat_id=None):
            self.messages.append((message, chat_id))

    notifier = FakeNotifier()
    digest = QuotaTelegramDigestSubscriber(notifier=notifier, chat_id=123, interval_sec=60)

    digest.handle_event(
        "quota.observed",
        {
            "provider": "claude",
            "bucket_id": "five_hour",
            "used_percent": 40.0,
            "reset_at": "2026-07-29T13:00:00Z",
            "telemetry_quality": "authoritative",
        },
    )
    await asyncio.sleep(0)

    assert notifier.messages
    message, chat_id = notifier.messages[0]
    assert chat_id == 123
    assert "claude/five_hour" in message
    assert "Claude tokens spent by observer: 0" in message


# ---------------------------------------------------------------------------
# A78 — quota telemetry truthfulness
# ---------------------------------------------------------------------------

def _anchored_bucket(provider="fake", principal="fake-principal", bucket_id="five-hour"):
    return QuotaBucket(
        provider=provider,
        bucket_id=bucket_id,
        bucket_name="Five hour",
        principal_hash=principal,
        window_semantics=WindowSemantics.ANCHORED,
        telemetry_quality=TelemetryQuality.AUTHORITATIVE,
        window_duration_seconds=5 * 60 * 60,
    )


@pytest.mark.asyncio
async def test_active_session_state_threaded_into_read_model(tmp_path):
    """A78 §1: the adapter's live active-session reading reaches the read model."""
    now = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)
    for active, expected in ((False, "false"), (True, "true"), (None, "unknown")):
        store = _store(tmp_path.joinpath(f"db-{expected}.sqlite"))
        adapter = FakeQuotaAdapter(
            provider="fake",
            principal_hash="principal",
            buckets=[_anchored_bucket()],
            snapshots={"five-hour": [_snapshot(principal="principal", observed=now)]},
            active_user_session=active,
        )
        coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True, now=lambda n=now: n)
        await coord.observe_once()
        state = coord.read_status()["window_states"][0]
        assert state["active_session_state"] == expected


@pytest.mark.asyncio
async def test_automation_ready_reachable_when_no_blockers(tmp_path):
    """A78 §1: automation_ready is a reachable state, not dead code."""
    now = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    adapter = FakeQuotaAdapter(
        provider="fake",
        principal_hash="principal",
        buckets=[_anchored_bucket(principal="principal")],
        snapshots={
            "five-hour": [
                _snapshot(
                    principal="principal",
                    observed=now,
                    used=12.0,
                    reset=datetime(2026, 8, 21, 13, 0, tzinfo=timezone.utc),
                )
            ]
        },
        active_user_session=False,
    )
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True, now=lambda: now)
    await coord.observe_once()
    state = coord.read_status()["window_states"][0]
    assert state["telemetry_state"] == "current"
    assert state["window_known"] is True
    assert state["automation_ready"] is True
    assert state["blockers"] == []


def test_schedule_aware_staleness_current_until_next_probe_overdue(tmp_path):
    """A78 §2: an hours-old reading is CURRENT while the next scheduled probe
    is still ahead of itself; only an overdue poll (+grace) reads stale."""
    from src.services.quota_window_coordinator import _STALE_SCHEDULE_GRACE

    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    observed = now - timedelta(hours=4)
    reset = now + timedelta(hours=1)
    store = _store(tmp_path)
    store.insert_snapshot(_snapshot(observed=observed, used=40.0, reset=reset))

    # Next poll due in 45 min → the 4h-old reading is on schedule, not blind.
    due = now + timedelta(minutes=45)
    states = store.status(now=now, next_observe_due_at=due)["window_states"]
    assert states[0]["telemetry_state"] == "current"
    assert states[0]["observed_age_sec"] == int((now - observed).total_seconds())
    assert states[0]["next_observe_due_at"] == utc_iso(due)

    # Poll overdue by more than the grace → genuinely stale.
    overdue = now - timedelta(minutes=1) - _STALE_SCHEDULE_GRACE
    states = store.status(now=now, next_observe_due_at=overdue)["window_states"]
    assert states[0]["telemetry_state"] == "stale"
    assert "observation_stale" in states[0]["blockers"]

    # No schedule supplied → legacy fixed-threshold behaviour (old reading).
    states = store.status(now=now)["window_states"]
    assert states[0]["telemetry_state"] == "stale"


def test_spent_reading_stays_current_until_its_reset_passes(tmp_path):
    """A78 §2: 'exhausted until T' does not decay into 'unknown' with age."""
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    spent = QuotaSnapshot(
        provider="fake",
        bucket_id="five-hour",
        principal_hash="fake-principal",
        observed_at=now - timedelta(hours=4),
        telemetry_quality=TelemetryQuality.AUTHORITATIVE,
        used_percent=100.0,
        reset_at=now + timedelta(hours=1),
        limit_reached=True,
        raw_status="observed",
    )
    store.insert_snapshot(spent)

    # Hours old and past any 15-minute rule — but its reset is still ahead.
    states = store.status(now=now)["window_states"]
    assert states[0]["telemetry_state"] == "current"

    # Once the boundary passes the reading belongs to a closed window.
    later = now + timedelta(hours=2)
    states = store.status(now=later)["window_states"]
    assert states[0]["telemetry_state"] == "stale"


def test_reset_history_clusters_jitter_and_bounds_evidence(tmp_path, monkeypatch):
    """A78 §4: sub-second reset jitter collapses into ONE window; evidence is
    bounded with truncation declared; counts stay honest."""
    import src.services.quota_window_coordinator as mod

    monkeypatch.setattr(mod, "_EVIDENCE_LIMIT", 3)
    base = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)

    def insert_boundary(index: int, used: float, jitter: timedelta):
        store.insert_snapshot(
            _snapshot(
                observed=base + timedelta(days=index),
                used=used,
                reset=datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc) + jitter,
            )
        )

    # One weekly boundary reported with sub-second drift across many polls.
    insert_boundary(0, 10.0, timedelta(seconds=-0.4))
    insert_boundary(1, 20.0, timedelta(seconds=0.1))
    # A distinct later boundary (well beyond jitter).
    insert_boundary(2, 30.0, timedelta(days=7))
    insert_boundary(3, 40.0, timedelta(days=7, seconds=0.3))

    history = mod._reset_history(
        store._conn(), provider="fake", principal_hash="fake-principal",
        bucket_id="five-hour",
        reset_at=utc_iso(datetime(2026, 8, 27, 22, 0, tzinfo=timezone.utc) + timedelta(seconds=0.3)),
    )
    assert history["observed_reset_count"] == 2  # jitter pairs collapsed
    assert len(history["reset_boundary_evidence"]) <= 3
    assert history["evidence_truncated"] is False
    # Current boundary matched to its cluster despite string inequality.
    assert history["current_reset_observed_since"] is not None


def test_v1_database_migration_adds_active_session_column(tmp_path):
    """A78 §1: an existing v1 store upgrades in place, data intact."""
    import sqlite3

    db_path = tmp_path / "v1.db"
    conn = sqlite3.connect(str(db_path))
    v1_ddl = """
    CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS snapshots (
        snapshot_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        principal_hash TEXT NOT NULL,
        bucket_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        telemetry_quality TEXT NOT NULL,
        used_percent REAL,
        reset_at TEXT,
        limit_reached INTEGER,
        window_duration_seconds INTEGER,
        raw_status TEXT NOT NULL DEFAULT '',
        unavailable_reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    """
    conn.executescript(v1_ddl)
    conn.execute("INSERT INTO schema_version(version, applied_at) VALUES (1, '2026-08-01T00:00:00Z')")
    conn.execute(
        "INSERT INTO snapshots(snapshot_id, provider, principal_hash, bucket_id, observed_at,"
        " telemetry_quality, created_at) VALUES ('legacy', 'claude', 'p', 'five_hour',"
        " '2026-08-01T00:00:00Z', 'authoritative', '2026-08-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    store = QuotaWindowStore(db_path)
    cols = {row[1] for row in store._conn().execute("PRAGMA table_info(snapshots)").fetchall()}
    assert "active_session_state" in cols
    version = store._conn().execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version >= 2
    legacy = store.latest_snapshot("claude", "p", "five_hour")
    assert legacy is not None and legacy["active_session_state"] is None


@pytest.mark.asyncio
async def test_record_refusal_snapshot_attributed_idempotent_and_schedules(tmp_path):
    """A78 §3: a refusal teaches the store the exact boundary once; repeats are
    idempotent per boundary; the observer then sleeps to that boundary."""
    now = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    adapter = FakeQuotaAdapter(
        provider="claude",
        principal_hash="principal",
        buckets=[_anchored_bucket(bucket_id="five_hour")],
        snapshots={
            "five_hour": [
                _snapshot(
                    provider="claude",
                    bucket="five_hour",
                    principal="principal",
                    observed=now,
                    used=95.0,
                    reset=now + timedelta(hours=2),
                )
            ]
        },
    )
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True, now=lambda: now)
    await coord.observe_once()

    reset_at = datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc)
    assert store.record_refusal_snapshot(provider="claude", reset_at=reset_at, observed_at=now) is True
    # Same boundary again → no duplicate row.
    assert store.record_refusal_snapshot(provider="claude", reset_at=reset_at, observed_at=now + timedelta(seconds=5)) is False

    snap = store.latest_snapshot("claude", "principal", "five_hour")
    assert snap["telemetry_quality"] == TelemetryQuality.EVENT_DERIVED.value
    assert snap["limit_reached"] == 1
    assert snap["used_percent"] is None

    # The observer stops re-asking: it sleeps to the refused boundary (+settle).
    delay = coord.next_observe_delay_sec()
    until_reset = int((reset_at - now).total_seconds())
    assert delay == min(until_reset + 30, coord.observe_max_interval_sec)


@pytest.mark.asyncio
async def test_refusal_without_known_principal_records_event_only(tmp_path):
    store = _store(tmp_path)
    stored = store.record_refusal_snapshot(
        provider="codex",
        reset_at=datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc),
    )
    assert stored is False
    events = store._conn().execute("SELECT event_name FROM coordinator_events").fetchall()
    assert any(row["event_name"] == "quota.refusal_unattributed" for row in events)


def test_prune_keeps_first_sightings_recent_rows_and_honours_zero(tmp_path):
    """A78 §4 anti-goal: retention must never delete boundary evidence."""
    now = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)
    old = now - timedelta(days=30)
    store = _store(tmp_path)

    def snap(at, used, sid):
        return QuotaSnapshot(
            provider="fake", bucket_id="five-hour", principal_hash="fake-principal",
            observed_at=at, telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            used_percent=used, reset_at=datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc),
            limit_reached=False, raw_status=sid,
        )

    store.insert_snapshot(snap(old, 10.0, "first-sighting-old"))
    store.insert_snapshot(snap(old + timedelta(hours=1), 11.0, "confirmation-old"))
    store.insert_snapshot(snap(now - timedelta(hours=1), 12.0, "recent"))

    pruned = store.prune_snapshots(retention_days=14, now=now)
    assert pruned == 1  # only the redundant old confirmation dies
    remaining = {row["raw_status"] for row in store._conn().execute("SELECT raw_status FROM snapshots")}
    assert {"first-sighting-old", "recent"} == remaining

    # 0 disables pruning entirely.
    total_before = store._conn().execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert store.prune_snapshots(retention_days=0, now=now) == 0
    total_after = store._conn().execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert total_before == total_after


def test_prune_treats_jittered_reset_at_as_one_boundary(tmp_path):
    """The provider reports reset_at with sub-second drift (live: 11:19:59.6 ..
    11:20:00.5 for one window). Retention must cluster within _BOUNDARY_JITTER
    like the evidence reader does — exact-string anchors kept ~98% of old rows
    on the live controller — while boundaries farther apart stay distinct."""
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(days=30)
    store = _store(tmp_path)
    boundary_a = datetime(2026, 9, 22, 11, 20, tzinfo=timezone.utc)
    boundary_b = boundary_a + timedelta(seconds=3)   # > jitter: a different window
    drifts = (0.106364, -0.373754, 0.375913, -0.169618, 0.48447, -0.405295)

    def snap(i, reset, tag):
        return QuotaSnapshot(
            provider="fake", bucket_id="five-hour", principal_hash="fake-principal",
            observed_at=old + timedelta(minutes=2 * i), telemetry_quality=TelemetryQuality.AUTHORITATIVE,
            used_percent=10.0 + i, reset_at=reset, limit_reached=False, raw_status=tag,
        )

    for i, d in enumerate(drifts):
        store.insert_snapshot(snap(i, boundary_a + timedelta(seconds=d), f"a{i}"))
    for i, d in enumerate(drifts[:3]):
        store.insert_snapshot(snap(10 + i, boundary_b + timedelta(seconds=d), f"b{i}"))

    assert store.prune_snapshots(retention_days=14, now=now) == 7
    remaining = {row["raw_status"] for row in store._conn().execute("SELECT raw_status FROM snapshots")}
    assert remaining == {"a0", "b0"}


def test_latest_snapshots_stays_indexed_on_volume(tmp_path):
    """A78 §5 regression guard: the bounded read stays fast and index-backed on
    a seeded-volume store; the correlated-subquery disaster cannot return."""
    import sqlite3
    import time

    store = _store(tmp_path)
    conn = sqlite3.connect(str(store._path))
    rows = []
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for bucket in ("a", "b", "c", "d"):
        for i in range(5000):
            at = utc_iso(base + timedelta(seconds=i * 30))
            rows.append((f"{bucket}-{i}", "fake", f"p-{bucket}", bucket, at, "authoritative", 50.0, None, None, None, "observed", "", at))
    conn.executemany(
        "INSERT OR IGNORE INTO snapshots(snapshot_id, provider, principal_hash, bucket_id,"
        " observed_at, telemetry_quality, used_percent, reset_at, limit_reached,"
        " window_duration_seconds, raw_status, unavailable_reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()

    started = time.perf_counter()
    latest = store.latest_snapshots()
    elapsed = time.perf_counter() - started
    assert len(latest) == 4
    assert elapsed < 0.5, f"latest_snapshots took {elapsed:.3f}s — regression"

    plan = " ".join(
        str(row[-1])
        for row in store._conn().execute(
            "EXPLAIN QUERY PLAN SELECT * FROM snapshots WHERE provider = ? AND principal_hash = ?"
            " AND bucket_id = ? ORDER BY observed_at DESC, created_at DESC LIMIT 1",
            ("fake", "p-a", "a"),
        ).fetchall()
    )
    assert "USING INDEX" in plan and "SCAN" not in plan.replace("SEARCH", "").replace("USING INDEX", "")


def test_prune_snapshots_stays_fast_on_volume(tmp_path):
    """Regression guard for the 2026-08-21 live incident: prune_snapshots used a
    per-row correlated EXISTS subquery that effectively re-scanned each
    candidate row's whole bucket per row. Against the live 54k-row table it ran
    synchronously on the gateway's event loop for 4+ minutes (never finished),
    freezing health checks / worker heartbeats / fetch_pending the whole time.
    Seeded here at a comparable scale+shape (many buckets, mostly-old rows, one
    reset_at anchor + many redundant confirmations per bucket — the case the
    correlated subquery handled worst): must stay well under a second and keep
    exactly the same keep/delete semantics as the correctness test above."""
    import sqlite3
    import time

    store = _store(tmp_path)
    conn = sqlite3.connect(str(store._path))
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    old_base = now - timedelta(days=30)
    rows = []
    for bucket in range(10):
        reset_at = utc_iso(datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc))
        for i in range(2000):  # first sighting (i=0) + 1999 redundant old confirmations
            at = utc_iso(old_base + timedelta(minutes=i))
            rows.append((
                f"b{bucket}-{i}", "fake", f"p-{bucket}", "five-hour", at,
                "authoritative", 50.0, reset_at, 0, None, "observed", "", at,
            ))
        # one recent row per bucket — must always survive pruning.
        recent_at = utc_iso(now - timedelta(hours=1))
        rows.append((
            f"b{bucket}-recent", "fake", f"p-{bucket}", "five-hour", recent_at,
            "authoritative", 50.0, reset_at, 0, None, "observed", "", recent_at,
        ))
    conn.executemany(
        "INSERT OR IGNORE INTO snapshots(snapshot_id, provider, principal_hash, bucket_id,"
        " observed_at, telemetry_quality, used_percent, reset_at, limit_reached,"
        " window_duration_seconds, raw_status, unavailable_reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    total_before = len(rows)

    started = time.perf_counter()
    pruned = store.prune_snapshots(retention_days=14, now=now)
    elapsed = time.perf_counter() - started

    # The incident was O(n^2) and ran 4+ minutes on 54k rows; a healthy linear
    # prune here is sub-second. Use a generous ceiling so CI parallelism (xdist
    # CPU contention) cannot flake it, while still catching a minutes-long
    # algorithmic regression by a wide margin.
    assert elapsed < 30.0, f"prune_snapshots took {elapsed:.3f}s — regression"
    # Per bucket: keep the first sighting (i=0) + the recent row; delete the rest.
    assert pruned == total_before - (10 * 2)
    remaining = store._conn().execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert remaining == 10 * 2


# --------------------------------------------------------------------------- #
# A93 — bounded quota store: events + snapshots, batched, worker-path, reclaim #
# --------------------------------------------------------------------------- #

def _seed_events(store, stamps):
    import sqlite3

    conn = sqlite3.connect(str(store._path))
    conn.executemany(
        "INSERT INTO coordinator_events(event_name, reason, created_at) VALUES (?, ?, ?)",
        [("quota.duplicate_snapshot", tag, at) for tag, at in stamps],
    )
    conn.commit()
    conn.close()


def _seed_old_snapshots(store, *, now, count, buckets=1):
    """``count`` old rows per bucket on ONE boundary (row 0 = its first
    sighting) plus one recent row per bucket."""
    import sqlite3

    conn = sqlite3.connect(str(store._path))
    reset_at = utc_iso(now - timedelta(days=40))
    rows = []
    for b in range(buckets):
        for i in range(count):
            at = utc_iso(now - timedelta(days=30) + timedelta(minutes=i))
            rows.append((f"b{b}-{i}", "fake", f"p-{b}", "five-hour", at, "authoritative", 50.0, reset_at, 0, None, "observed", "", at))
        recent = utc_iso(now - timedelta(hours=1))
        rows.append((f"b{b}-recent", "fake", f"p-{b}", "five-hour", recent, "authoritative", 50.0, reset_at, 0, None, "observed", "", recent))
    conn.executemany(
        "INSERT INTO snapshots(snapshot_id, provider, principal_hash, bucket_id,"
        " observed_at, telemetry_quality, used_percent, reset_at, limit_reached,"
        " window_duration_seconds, raw_status, unavailable_reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _count(store, table):
    return store._conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_prune_events_removes_old_keeps_fresh_skewed_and_honours_zero(tmp_path):
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    _seed_events(store, [
        ("old-z", utc_iso(now - timedelta(days=30))),
        ("old-naive", "2026-08-01T00:00:00"),       # naive, pre-Z writer
        ("old-space", "2026-08-01 00:00:00"),       # naive, sqlite datetime() shape
        ("fresh", utc_iso(now - timedelta(days=1))),
        ("future-skew", utc_iso(now + timedelta(days=3))),  # clock ran ahead
    ])

    assert store.prune_events(retention_days=0, now=now) == 0
    assert _count(store, "coordinator_events") == 5

    assert store.prune_events(retention_days=14, now=now) == 3
    remaining = {r[0] for r in store._conn().execute("SELECT reason FROM coordinator_events")}
    assert remaining == {"fresh", "future-skew"}


def test_prune_batches_bound_rows_per_write_transaction(tmp_path):
    """A93 adversarial: a first prune of a large backlog must hold the write
    lock only for bounded batches, never one giant DELETE."""
    from contextlib import contextmanager

    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    _seed_events(store, [(f"e{i}", utc_iso(now - timedelta(days=20, seconds=i))) for i in range(1050)])
    _seed_old_snapshots(store, now=now, count=530, buckets=2)

    per_tx: list[int] = []
    real_write = store._write

    @contextmanager
    def counting_write():
        with real_write() as conn:
            before = conn.total_changes
            yield conn
            per_tx.append(conn.total_changes - before)

    store._write = counting_write
    assert store.prune_events(retention_days=14, now=now, batch_rows=100) == 1050
    assert max(per_tx) <= 100 and len(per_tx) == 11  # 10 full batches + the short one
    assert _count(store, "coordinator_events") == 0

    per_tx.clear()
    # Each bucket keeps its first sighting + its recent row across every batch.
    assert store.prune_snapshots(retention_days=14, now=now, batch_rows=100) == 2 * 529
    assert max(per_tx) <= 100 and len(per_tx) >= 11
    kept = {r[0] for r in store._conn().execute("SELECT snapshot_id FROM snapshots")}
    assert kept == {"b0-0", "b0-recent", "b1-0", "b1-recent"}


def test_first_run_backlog_prune_keeps_each_batch_short(tmp_path):
    """The live worker backlog (~225k events) clears in short transactions."""
    import time
    from contextlib import contextmanager

    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    _seed_events(store, [(f"e{i}", utc_iso(now - timedelta(days=60) + timedelta(seconds=i * 10))) for i in range(225_000)])
    durations: list[float] = []
    real_write = store._write

    @contextmanager
    def timed_write():
        started = time.perf_counter()
        with real_write() as conn:
            yield conn
        durations.append(time.perf_counter() - started)

    store._write = timed_write
    removed = store.prune_events(retention_days=14, now=now)
    assert removed > 200_000
    assert _count(store, "coordinator_events") == 225_000 - removed
    assert len(durations) >= removed // 5000
    assert max(durations) < 1.0, f"a prune batch held the write lock {max(durations):.3f}s"


def test_concurrent_pruners_on_one_file_converge(tmp_path):
    """Two controller processes (gateway + task-server) pruning the same file:
    no error, no row deleted twice, and the result equals a single prune."""
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "quota.db"
    a, b = QuotaWindowStore(path), QuotaWindowStore(path)
    _seed_events(a, [(f"e{i}", utc_iso(now - timedelta(days=20))) for i in range(3000)]
                 + [("fresh", utc_iso(now))])
    _seed_old_snapshots(a, now=now, count=1500)

    def prune(store):
        return (
            store.prune_events(retention_days=14, now=now, batch_rows=200),
            store.prune_snapshots(retention_days=14, now=now, batch_rows=200),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prune, [a, b]))
    assert sum(r[0] for r in results) == 3000
    assert sum(r[1] for r in results) == 1499
    assert _count(a, "coordinator_events") == 1
    assert {r[0] for r in a._conn().execute("SELECT snapshot_id FROM snapshots")} == {"b0-0", "b0-recent"}


@pytest.mark.asyncio
async def test_prune_once_daily_prunes_both_tables_once_per_utc_day(tmp_path):
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    clock = {"now": now}
    store = _store(tmp_path)
    _seed_events(store, [("old", utc_iso(now - timedelta(days=20)))])
    _seed_old_snapshots(store, now=now, count=5)
    coord = QuotaWindowCoordinator(store=store, adapters=[], enabled=True, now=lambda: clock["now"])

    await coord.prune_once_daily()
    assert _count(store, "coordinator_events") == 0
    assert _count(store, "snapshots") == 2

    _seed_events(store, [("old-2", utc_iso(now - timedelta(days=20)))])
    await coord.prune_once_daily()              # same UTC day: no second pass
    assert _count(store, "coordinator_events") == 1
    clock["now"] = now + timedelta(days=1)
    await coord.prune_once_daily()              # next day: prunes again
    assert _count(store, "coordinator_events") == 0


@pytest.mark.asyncio
async def test_prune_failure_never_escapes_or_stops_the_observe_loop(tmp_path, monkeypatch):
    store = _store(tmp_path)
    coord = QuotaWindowCoordinator(store=store, adapters=[], enabled=True)

    def boom(**_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "prune_events", boom)
    await coord.prune_once_daily()              # swallowed, not raised
    assert coord._last_prune_day is None        # retried on a later tick

    observed = {"n": 0}

    async def observe_once():
        observed["n"] += 1
        if observed["n"] >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(coord, "observe_once", observe_once)
    monkeypatch.setattr(coord, "next_observe_delay_sec", lambda **_kw: 0)
    with pytest.raises(asyncio.CancelledError):
        await coord._observe_loop()
    assert observed["n"] == 3                   # loop kept observing past failed prunes


@pytest.mark.asyncio
async def test_retention_runs_from_the_worker_prewarmer_path(tmp_path):
    """A93: the worker never starts the coordinator loop; its prewarmer tick
    must drive the same daily retention (and reclaim space on its own cache)."""
    from src.services.quota_window_prewarmer import QuotaWindowPrewarmer

    now = datetime.now(timezone.utc)
    store = _store(tmp_path)
    _seed_events(store, [(f"e{i}", utc_iso(now - timedelta(days=20))) for i in range(2000)]
                 + [("fresh", utc_iso(now))])
    _seed_old_snapshots(store, now=now, count=2000)
    coord = QuotaWindowCoordinator(store=store, adapters=[], enabled=True, reclaim_space=True)
    warmer = QuotaWindowPrewarmer(coordinator=coord, enabled=True)
    store._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    size_before = store._path.stat().st_size

    decision = await warmer.tick_once()

    assert decision.action != "activate"
    assert _count(store, "coordinator_events") == 1
    assert {r[0] for r in store._conn().execute("SELECT snapshot_id FROM snapshots")} == {"b0-0", "b0-recent"}
    conn = store._conn()
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2      # INCREMENTAL
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert store._path.stat().st_size < size_before


def test_reclaim_space_converts_once_then_goes_incremental(tmp_path):
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    _seed_events(store, [(f"e{i}", utc_iso(now - timedelta(days=20))) for i in range(3000)])
    store.prune_events(retention_days=14, now=now)
    assert store.reclaim_space() == "vacuum"
    _seed_events(store, [(f"e{i}", utc_iso(now - timedelta(days=20))) for i in range(3000)])
    store.prune_events(retention_days=14, now=now)
    before = store._conn().execute("PRAGMA freelist_count").fetchone()[0]
    assert before > 0
    assert store.reclaim_space() == "incremental"
    # Every freed page is returned. (Stepping the pragma via execute().fetchall()
    # freed ONE page per call on py3.11/SQLite 3.40.1; `after < before` hid that.)
    assert store._conn().execute("PRAGMA freelist_count").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_shared_controller_store_is_never_vacuumed(tmp_path):
    """reclaim_space defaults OFF: the controller store (gateway + task-server)
    is pruned but never rewritten."""
    now = datetime.now(timezone.utc)
    store = _store(tmp_path)
    _seed_events(store, [(f"e{i}", utc_iso(now - timedelta(days=20))) for i in range(500)])
    coord = QuotaWindowCoordinator(store=store, adapters=[], enabled=True)

    await coord.prune_once_daily()

    assert _count(store, "coordinator_events") == 0
    assert store._conn().execute("PRAGMA auto_vacuum").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# A93 amendment — archive before delete (quota_archive.db)                     #
# --------------------------------------------------------------------------- #

def _seed_event_rows(store, rows):
    """rows: (event_name, reason, payload, created_at) in id order."""
    import sqlite3

    conn = sqlite3.connect(str(store._path))
    conn.executemany(
        "INSERT INTO coordinator_events(event_name, provider, reason, payload, created_at) VALUES (?, 'claude', ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _eighty_days(store, now):
    """~80 days of history shaped like the live worker: long noise runs with
    rare refusals between them, and snapshots on several boundaries per bucket."""
    import sqlite3

    events = []
    start = now - timedelta(days=80)
    for day in range(80):
        base = start + timedelta(days=day)
        for i in range(40):
            events.append(("quota.adapter_unavailable", "timeout", "{}", utc_iso(base + timedelta(minutes=i))))
        if day % 9 == 0:
            events.append(("quota.refusal_recorded", "rate_limit_event", f'{{"reset_at": "day-{day}"}}', utc_iso(base + timedelta(hours=2))))
        for i in range(25):
            events.append(("quota.duplicate_snapshot", "", "{}", utc_iso(base + timedelta(hours=3, minutes=i))))
    _seed_event_rows(store, events)

    snaps = []
    for bucket in ("five_hour", "seven_day"):
        for hour in range(80 * 24):
            at = start + timedelta(hours=hour)
            reset = utc_iso(start + timedelta(hours=(hour // 5 + 1) * 5))
            snaps.append((f"{bucket}-{hour}", "claude", "p", bucket, utc_iso(at), "authoritative",
                          float(hour % 5) * 10, reset, 0, None, "observed", "", None, utc_iso(at)))
    conn = sqlite3.connect(str(store._path))
    conn.executemany(
        "INSERT INTO snapshots(snapshot_id, provider, principal_hash, bucket_id, observed_at,"
        " telemetry_quality, used_percent, reset_at, limit_reached, window_duration_seconds,"
        " raw_status, unavailable_reason, active_session_state, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        snaps,
    )
    conn.commit()
    conn.close()
    return events


def _archive_conn(store):
    import sqlite3

    conn = sqlite3.connect(str(store.archive_path))
    conn.row_factory = sqlite3.Row
    return conn


@pytest.mark.asyncio
async def test_first_pass_archives_the_whole_backlog_before_deleting_it(tmp_path):
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    events = _eighty_days(store, now)
    cols = "snapshot_id, provider, principal_hash, bucket_id, observed_at, telemetry_quality, used_percent, reset_at, limit_reached, raw_status, created_at"
    before = {r[0]: tuple(r) for r in store._conn().execute(f"SELECT {cols} FROM snapshots")}
    cutoff = utc_iso(now - timedelta(days=14))
    old_events = [e for e in events if e[3] < cutoff]
    coord = QuotaWindowCoordinator(store=store, adapters=[], enabled=True, now=lambda: now)
    assert coord.archive_enabled is True                       # default ON

    await coord.prune_once_daily()

    # Snapshots: every pruned row is in the archive, column for column.
    after = {r[0] for r in store._conn().execute("SELECT snapshot_id FROM snapshots")}
    pruned = set(before) - after
    assert len(pruned) > 2000
    archived = {r[0]: tuple(r) for r in _archive_conn(store).execute(f"SELECT {cols} FROM snapshots_archive")}
    assert set(archived) == pruned
    assert all(archived[sid] == before[sid] for sid in pruned)

    # Events: deleted, compacted, counts per name preserved exactly.
    assert _count(store, "coordinator_events") == len(events) - len(old_events)
    arch = _archive_conn(store)
    per_name = {r[0]: r[1] for r in arch.execute("SELECT event_name, SUM(count) FROM event_runs GROUP BY event_name")}
    expected: dict = {}
    for e in old_events:
        expected[e[0]] = expected.get(e[0], 0) + 1
    assert per_name == expected
    runs = arch.execute("SELECT COUNT(*) FROM event_runs").fetchone()[0]
    assert runs < len(old_events) / 15                         # noise collapses
    refusals = [r["first_payload"] for r in arch.execute(
        "SELECT first_payload FROM event_runs WHERE event_name = 'quota.refusal_recorded' ORDER BY first_id")]
    assert refusals == [e[2] for e in old_events if e[0] == "quota.refusal_recorded"]
    # Runs tile the pruned id range with no gap or overlap.
    spans = arch.execute("SELECT first_id, last_id, count FROM event_runs ORDER BY first_id").fetchall()
    assert spans[0][0] == 1 and spans[-1][1] == len(old_events)
    assert all(a[1] + 1 == b[0] for a, b in zip(spans, spans[1:]))
    assert all(last - first + 1 == n for first, last, n in spans)


def test_event_runs_are_identical_however_the_backlog_is_batched(tmp_path):
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    a, b = QuotaWindowStore(tmp_path / "a" / "quota.db"), QuotaWindowStore(tmp_path / "b" / "quota.db")
    for store in (a, b):
        _eighty_days(store, now)
    a.prune_events(retention_days=14, now=now, archive=True)
    b.prune_events(retention_days=14, now=now, archive=True, batch_rows=37)   # runs split across batches
    q = "SELECT first_id, last_id, event_name, count, first_at, last_at, first_payload, last_payload FROM event_runs ORDER BY first_id"
    assert [tuple(r) for r in _archive_conn(a).execute(q)] == [tuple(r) for r in _archive_conn(b).execute(q)]


def test_archive_is_idempotent_when_a_delete_fails_and_the_pass_retries(tmp_path, monkeypatch):
    import sqlite3

    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    events = _eighty_days(store, now)
    real_write = store._write
    calls = {"n": 0}

    def flaky_write():
        calls["n"] += 1
        if calls["n"] == 2:                                    # the 2nd batch's DELETE, after its archive
            raise sqlite3.OperationalError("database is locked")
        return real_write()

    monkeypatch.setattr(store, "_write", flaky_write)
    with pytest.raises(sqlite3.OperationalError):
        store.prune_events(retention_days=14, now=now, archive=True, batch_rows=100)
    monkeypatch.setattr(store, "_write", real_write)
    store.prune_events(retention_days=14, now=now, archive=True, batch_rows=100)

    cutoff = utc_iso(now - timedelta(days=14))
    total = _archive_conn(store).execute("SELECT SUM(count) FROM event_runs").fetchone()[0]
    assert total == sum(1 for e in events if e[3] < cutoff)    # nothing counted twice


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["archive_snapshots", "archive_events"])
async def test_archive_failure_deletes_nothing_and_retries(tmp_path, monkeypatch, method):
    from src.services import quota_window_coordinator as qwc

    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    _eighty_days(store, now)
    coord = QuotaWindowCoordinator(store=store, adapters=[], enabled=True, now=lambda: now, reclaim_space=True)
    if method == "archive_events":
        await asyncio.to_thread(store.prune_snapshots, retention_days=14, now=now, archive=True)
    snaps, evs = _count(store, "snapshots"), _count(store, "coordinator_events")
    warnings: list[str] = []
    monkeypatch.setattr(qwc.logger, "warning", lambda msg, *a: warnings.append(msg % a))

    def broken(self, rows):
        raise OSError("disk full")

    monkeypatch.setattr(qwc.QuotaArchive, method, broken)
    await coord.prune_once_daily()

    assert any(w.startswith("event=quota_archive_failed") for w in warnings)
    assert (_count(store, "snapshots"), _count(store, "coordinator_events")) == (snaps, evs)
    assert coord._last_prune_day is None                       # next tick retries
    assert store._conn().execute("PRAGMA auto_vacuum").fetchone()[0] == 0   # no reclaim either

    monkeypatch.undo()
    await coord.prune_once_daily()                             # archive healthy again
    assert coord._last_prune_day == "2026-10-22"
    cutoff = utc_iso(now - timedelta(days=14))
    assert store._conn().execute("SELECT COUNT(*) FROM coordinator_events WHERE created_at < ?", (cutoff,)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_archive_flag_off_is_this_prs_prior_behaviour(tmp_path):
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    _eighty_days(store, now)
    coord = QuotaWindowCoordinator(store=store, adapters=[], enabled=True, now=lambda: now, archive_enabled=False)

    await coord.prune_once_daily()

    assert not store.archive_path.exists()
    cutoff = utc_iso(now - timedelta(days=14))
    assert store._conn().execute("SELECT COUNT(*) FROM coordinator_events WHERE created_at < ?", (cutoff,)).fetchone()[0] == 0


def test_concurrent_archiving_pruners_count_each_event_once(tmp_path):
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "quota.db"
    a, b = QuotaWindowStore(path), QuotaWindowStore(path)
    events = _eighty_days(a, now)

    def prune(store):
        store.prune_events(retention_days=14, now=now, archive=True, batch_rows=150)
        store.prune_snapshots(retention_days=14, now=now, archive=True, batch_rows=500)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(prune, [a, b]))
    cutoff = utc_iso(now - timedelta(days=14))
    arch = _archive_conn(a)
    assert arch.execute("SELECT SUM(count) FROM event_runs").fetchone()[0] == sum(1 for e in events if e[3] < cutoff)
    assert _count(a, "coordinator_events") == sum(1 for e in events if e[3] >= cutoff)
    archived = arch.execute("SELECT COUNT(*) FROM snapshots_archive").fetchone()[0]
    assert archived + _count(a, "snapshots") == 2 * 80 * 24


def _old_events(store, n, *, name, now, days=20):
    _seed_event_rows(store, [(name, "", f'{{"n": {i}}}', utc_iso(now - timedelta(days=days, seconds=-i))) for i in range(n)])


def _archived_total(store, name=None):
    sql = "SELECT COALESCE(SUM(count), 0) FROM event_runs" + (" WHERE event_name = ?" if name else "")
    return _archive_conn(store).execute(sql, (name,) if name else ()).fetchone()[0]


def test_recreated_store_under_a_surviving_archive_loses_nothing(tmp_path):
    """Round-2 blocker: a rebuilt store restarts its ids at 1; they must not be
    mistaken for ids the archive already holds for the OLD store."""
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "quota.db"
    old = QuotaWindowStore(path)
    _old_events(old, 100, name="quota.adapter_unavailable", now=now)
    assert old.prune_events(retention_days=14, now=now, archive=True) == 100
    old.close()
    for f in tmp_path.glob("quota.db*"):
        f.unlink()                                              # archive survives

    new = QuotaWindowStore(path)
    _old_events(new, 50, name="quota.refusal_recorded", now=now)
    assert new.prune_events(retention_days=14, now=now, archive=True) == 50

    assert _archived_total(new, "quota.refusal_recorded") == 50
    assert _archived_total(new) == 150
    stores = _archive_conn(new).execute("SELECT COUNT(DISTINCT store_id) FROM event_runs").fetchone()[0]
    assert stores == 2


def test_restored_older_store_copy_rotates_identity_instead_of_losing_rows(tmp_path):
    """Same store identity, ids reused: a backup restored after the archive
    moved on. Rows the archive never took are archived under a new identity."""
    import shutil
    import sqlite3

    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "quota.db"
    store = QuotaWindowStore(path)
    _old_events(store, 100, name="quota.adapter_unavailable", now=now, days=30)
    store.store_id()
    backup = tmp_path / "backup.db"
    src = sqlite3.connect(str(path)); dst = sqlite3.connect(str(backup))
    src.backup(dst); src.close(); dst.close()

    store.prune_events(retention_days=14, now=now, archive=True)            # archives ids 1..100
    _old_events(store, 60, name="quota.duplicate_snapshot", now=now, days=25)
    store.prune_events(retention_days=14, now=now, archive=True)            # archives ids 101..160
    store.close()
    for f in tmp_path.glob("quota.db*"):
        f.unlink()
    shutil.copy(backup, path)                                               # ids 1..100 again, seq 100

    restored = QuotaWindowStore(path)
    _old_events(restored, 30, name="quota.refusal_recorded", now=now)        # ids 101..130 reused
    restored.prune_events(retention_days=14, now=now, archive=True)

    assert _count(restored, "coordinator_events") == 0
    assert _archived_total(restored, "quota.refusal_recorded") == 30        # nothing lost
    assert _archived_total(restored, "quota.duplicate_snapshot") == 60


def test_event_inserted_mid_pass_survives(tmp_path, monkeypatch):
    from src.services import quota_window_coordinator as qwc

    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    _old_events(store, 4, name="quota.adapter_unavailable", now=now)
    real = qwc.QuotaArchive.archive_events

    def archive_then_new_event(self, rows, **kw):
        real(self, rows, **kw)
        if not store._conn().execute("SELECT 1 FROM coordinator_events WHERE event_name = 'fresh'").fetchone():
            store.add_event("fresh")

    monkeypatch.setattr(qwc.QuotaArchive, "archive_events", archive_then_new_event)
    assert store.prune_events(retention_days=14, now=now, archive=True, batch_rows=4) == 4
    left = [r[0] for r in store._conn().execute("SELECT event_name FROM coordinator_events")]
    assert left == ["fresh"]


def test_singleton_event_run_stores_its_payload_once(tmp_path):
    """Live events interleave, so ~95-99% of runs are singletons; storing the
    payload as both first and last doubled the archive. A singleton keeps it in
    first_payload only; a run that grows (even across passes) gets last_payload."""
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    store = _store(tmp_path)
    old = now - timedelta(days=30)
    for i, (name, payload) in enumerate([("quota.a", '{"n": 1}'), ("quota.b", '{"n": 2}'), ("quota.b", '{"n": 3}')]):
        store._conn().execute(
            "INSERT INTO coordinator_events(event_name, reason, payload, created_at) VALUES (?, '', ?, ?)",
            (name, payload, utc_iso(old + timedelta(minutes=i))),
        )
    store._conn().commit()
    store.prune_events(retention_days=14, now=now, archive=True, batch_rows=2)   # run b spans two batches
    rows = [tuple(r) for r in _archive_conn(store).execute(
        "SELECT event_name, count, first_payload, last_payload FROM event_runs ORDER BY first_id")]
    assert rows == [("quota.a", 1, '{"n": 1}', ""), ("quota.b", 2, '{"n": 2}', '{"n": 3}')]
