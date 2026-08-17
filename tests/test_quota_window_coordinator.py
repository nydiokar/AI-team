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
    store = _store(tmp_path)
    snap = _snapshot(used=100.0, reset=datetime(2026, 7, 29, 13, 0, tzinfo=timezone.utc))
    store.insert_snapshot(QuotaSnapshot(**{**snap.__dict__, "limit_reached": True}))
    coord = QuotaWindowCoordinator(store=store, adapters=[], enabled=True, observe_interval_sec=300)

    assert coord.next_observe_delay_sec() == 300


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
