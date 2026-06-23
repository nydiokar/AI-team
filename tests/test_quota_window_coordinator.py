from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.services.quota_window_coordinator import (
    AdapterCapability,
    FakeQuotaAdapter,
    QuotaAdapterError,
    QuotaSnapshot,
    QuotaWindowCoordinator,
    QuotaWindowStore,
    TelemetryQuality,
    UnsupportedQuotaAdapter,
    utc_iso,
)


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


@pytest.mark.asyncio
async def test_concurrent_reads_and_writes(tmp_path):
    store = _store(tmp_path)
    base = datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc)
    snaps = [_snapshot(observed=base + timedelta(seconds=i), used=float(i)) for i in range(20)]
    adapter = FakeQuotaAdapter(snapshots={"five-hour": snaps})
    coord = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True)

    async def writer():
        for _ in range(20):
            await coord.observe_once()

    async def reader():
        for _ in range(20):
            await asyncio.to_thread(coord.read_status)

    await asyncio.gather(writer(), reader(), reader())
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

