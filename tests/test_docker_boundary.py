"""Controller/worker boundary tests for the finished Docker migration.

These lock the architectural invariant that the earlier partially-shared-host
setup violated:

  * the CONTROLLER never needs the Claude binary or ``~/.claude`` credentials —
    quota is observed harness-side (worker) and INGESTED over an explicit
    interface, then read back through the existing store/API;
  * activity transport is EXPLICIT, never inferred from network identity —
    same-host / loopback / Tailscale addressing must NOT disable forwarding, and
    forwarding failures are OBSERVABLE, not silently swallowed.

All offline: no real HTTP, no Claude binary, no gateway, no shared-storage
topology. The quota-ingest tests deliberately exercise the pure mapping path with
an injected usage payload, proving the controller path runs with no harness.
"""
from __future__ import annotations

import threading
import time
import types
from typing import Any, Dict, List, Optional, Tuple

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class _FakeHTTP:
    """Records POSTs; ``always_fail`` raises on every post."""

    def __init__(self, *, always_fail: bool = False) -> None:
        self.calls: List[Tuple[str, Any, int]] = []
        self._lock = threading.Lock()
        self.always_fail = always_fail

    def post(self, path: str, body: Any = None, timeout: int = 10) -> Any:
        if self.always_fail:
            raise RuntimeError("simulated transport failure")
        with self._lock:
            self.calls.append((path, body, timeout))
        return {"ok": True}

    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)


def _valid_usage() -> Dict[str, Any]:
    """A raw Claude ``get_usage`` response with an open 5h + 7d window."""
    return {
        "rate_limits_available": True,
        "subscription_type": "pro",
        "rate_limits": {
            "five_hour": {"utilization": 42.5, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"utilization": 12.0, "resets_at": "2030-01-07T00:00:00Z"},
        },
    }


def _base_worker_env(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_NODE_ID", "kanebra-worker")
    monkeypatch.setenv("WORKER_TOKEN", "t0ken")
    monkeypatch.setenv("WORKER_TAILSCALE_IP", "100.1.1.1")
    # Same-host addressing on purpose: controller URL == our own tailscale IP.
    monkeypatch.setenv("CONTROLLER_URL", "http://100.1.1.1:9002")
    monkeypatch.setenv("WORKER_BACKENDS", "claude,codex")
    for k in ("WORKER_SHARES_CONTROLLER_FS", "WORKER_QUOTA_OBSERVE", "QUOTA_COORDINATOR_ENABLED"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# Phase 3 — quota observation crosses the boundary with NO harness controller-side
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_quota_observation_ingests_without_harness(tmp_path):
    """A controller with no Claude binary/credentials ingests a worker-supplied
    usage payload and serves it through the SAME store the API reads — proving the
    observe→persist→read path needs no local harness."""
    from src.services.quota_window_coordinator import (
        QuotaWindowStore,
        build_quota_coordinator_from_config,
        ingest_worker_quota_observation,
    )
    from config import config

    db_path = str(tmp_path / "quota.db")
    monkey_db = config.quota.db_path
    config.quota.db_path = db_path
    try:
        result = await ingest_worker_quota_observation(
            provider="claude",
            node_id="kanebra-worker",
            usage=_valid_usage(),
            observed_at="2029-12-31T23:00:00Z",
            db_path=db_path,
        )
        assert result["accepted"] is True

        # A DISTINCT store handle (simulating the separate gateway process reading
        # the shared controller/state volume) sees the ingested rows.
        store = QuotaWindowStore(db_path)
        latest = {row["bucket_id"]: row for row in store.latest_snapshots()}
        assert "five_hour" in latest
        assert latest["five_hour"]["used_percent"] == 42.5
        assert latest["five_hour"]["telemetry_quality"] == "authoritative"

        # The gateway's ingest-only coordinator (no spawning adapters) reads it back
        # for /api/quota-windows.
        coord = build_quota_coordinator_from_config(enabled=True, observe_locally=False)
        status = coord.read_status()
        five = [w for w in status["window_states"] if w.get("bucket_id") == "five_hour"]
        assert five, "five_hour window not surfaced through read_status"
    finally:
        config.quota.db_path = monkey_db


@pytest.mark.asyncio
async def test_worker_harness_error_is_distinct_from_empty_window(tmp_path):
    """A harness read failure reported by the worker records the adapter as
    UNAVAILABLE (with reason) — never collapses into a legitimate empty window —
    and does NOT overwrite a prior good snapshot (no flapping)."""
    from src.services.quota_window_coordinator import (
        QuotaWindowStore,
        ingest_worker_quota_observation,
    )

    db_path = str(tmp_path / "quota.db")

    # First a good observation, then a harness error.
    await ingest_worker_quota_observation(
        provider="claude", node_id="w1", usage=_valid_usage(),
        observed_at="2029-12-31T23:00:00Z", db_path=db_path,
    )
    err = await ingest_worker_quota_observation(
        provider="claude", node_id="w1", usage=None, error="ConnectError", db_path=db_path,
    )
    assert err["accepted"] is False
    assert err["reason"] == "harness_error"

    store = QuotaWindowStore(db_path)
    adapters = {a["provider"]: a for a in store.status()["adapters"]}
    assert adapters["claude"]["status"] == "unavailable"
    assert "worker_harness_unavailable:ConnectError" in adapters["claude"]["reason"]

    # The prior good snapshot survives — the error did not flap the window to empty.
    latest = {row["bucket_id"]: row for row in store.latest_snapshots()}
    assert latest["five_hour"]["used_percent"] == 42.5


def test_ingest_only_coordinator_has_no_spawning_adapter(tmp_path):
    """observe_locally=False (controller-only host) builds an ingest-only
    coordinator — it must not carry the Claude-spawning adapter."""
    from config import config
    from src.services.quota_window_coordinator import (
        ClaudeGetUsageQuotaAdapter,
        build_quota_coordinator_from_config,
    )

    saved = config.quota.db_path
    config.quota.db_path = str(tmp_path / "quota.db")
    try:
        ingest_only = build_quota_coordinator_from_config(enabled=True, observe_locally=False)
        assert ingest_only.adapters == []

        local = build_quota_coordinator_from_config(enabled=True, observe_locally=True)
        assert any(isinstance(a, ClaudeGetUsageQuotaAdapter) for a in local.adapters)
    finally:
        config.quota.db_path = saved


# ---------------------------------------------------------------------------
# Phase 4/5 — activity transport is explicit + failures are observable
# ---------------------------------------------------------------------------

def test_activity_forward_failure_is_observable_not_swallowed():
    """A transport failure must be counted/visible (stats), and the daemon must
    survive — the old bare ``except: pass`` hid a broken control-plane path."""
    from src.worker.agent import _ActivityForwarder

    http = _FakeHTTP(always_fail=True)
    fwd = _ActivityForwarder(http, node_id="w1")
    fwd.offer({
        "event": "task_activity", "session_id": "s" * 12,
        "task_id": "task_1", "label": "Using Bash",
    })
    assert _wait_for(lambda: fwd.stats()["failed"] >= 1), "failure was not observable"
    # thread still alive: a second offer is still processed (fails again, counted).
    fwd.offer({
        "event": "task_activity", "session_id": "s" * 12,
        "task_id": "task_1", "label": "Thinking",
    })
    assert _wait_for(lambda: fwd.stats()["failed"] >= 2)


def test_activity_forward_success_counts():
    from src.worker.agent import _ActivityForwarder

    http = _FakeHTTP()
    fwd = _ActivityForwarder(http, node_id="w1")
    fwd.offer({
        "event": "task_activity", "session_id": "s" * 12,
        "task_id": "task_1", "label": "Using Bash",
    })
    assert _wait_for(lambda: fwd.stats()["sent"] == 1)
    assert http.calls[0][0] == "/events/activity"


def test_setup_forwarding_enabled_despite_local_controller_url():
    """Loopback / same-host controller URL must NOT disable forwarding — under the
    container split the worker never shares the controller's events.ndjson."""
    from src.core.observability import register_event_forwarder
    from src.worker.agent import WorkerAgent

    stub = types.SimpleNamespace(
        cfg=types.SimpleNamespace(
            shares_controller_fs=False, node_id="w1",
            controller_url="http://127.0.0.1:9002",
        ),
        _http=_FakeHTTP(),
        _activity_forwarder=None,
    )
    try:
        WorkerAgent._setup_activity_forwarding(stub)
        assert stub._activity_forwarder is not None
    finally:
        register_event_forwarder(None)


def test_setup_forwarding_disabled_only_with_explicit_shared_fs():
    """The single legacy escape hatch is the EXPLICIT flag, not any heuristic."""
    from src.core.observability import register_event_forwarder
    from src.worker.agent import WorkerAgent

    stub = types.SimpleNamespace(
        cfg=types.SimpleNamespace(
            shares_controller_fs=True, node_id="w1",
            controller_url="http://10.9.9.9:9002",  # remote-looking, still skipped
        ),
        _http=_FakeHTTP(),
        _activity_forwarder=None,
    )
    try:
        WorkerAgent._setup_activity_forwarding(stub)
        assert stub._activity_forwarder is None
    finally:
        register_event_forwarder(None)


# ---------------------------------------------------------------------------
# Worker config — addressing must not imply shared FS; explicit knobs
# ---------------------------------------------------------------------------

def test_worker_config_addressing_does_not_imply_shared_fs(monkeypatch):
    from src.worker.config import WorkerConfig

    _base_worker_env(monkeypatch)
    cfg = WorkerConfig.from_env()
    # Same-host tailscale addressing, yet forwarding is NOT disabled.
    assert cfg.shares_controller_fs is False


def test_worker_config_shares_controller_fs_explicit_optin(monkeypatch):
    from src.worker.config import WorkerConfig

    _base_worker_env(monkeypatch)
    monkeypatch.setenv("WORKER_SHARES_CONTROLLER_FS", "1")
    assert WorkerConfig.from_env().shares_controller_fs is True


def test_worker_quota_observe_defaults_on_for_claude_when_coordinator_enabled(monkeypatch):
    from src.worker.config import WorkerConfig

    _base_worker_env(monkeypatch)
    monkeypatch.setenv("QUOTA_COORDINATOR_ENABLED", "1")
    assert WorkerConfig.from_env().quota_observe_enabled is True


def test_worker_quota_observe_off_when_coordinator_disabled(monkeypatch):
    from src.worker.config import WorkerConfig

    _base_worker_env(monkeypatch)  # QUOTA_COORDINATOR_ENABLED unset
    assert WorkerConfig.from_env().quota_observe_enabled is False


def test_worker_quota_observe_explicit_override_wins(monkeypatch):
    from src.worker.config import WorkerConfig

    _base_worker_env(monkeypatch)
    monkeypatch.setenv("QUOTA_COORDINATOR_ENABLED", "1")
    monkeypatch.setenv("WORKER_QUOTA_OBSERVE", "0")
    assert WorkerConfig.from_env().quota_observe_enabled is False


# ---------------------------------------------------------------------------
# Window warming runs where the harness lives (regression: warming went inert
# under the Docker controller/worker split — controller has adapters == []).
# ---------------------------------------------------------------------------

def test_worker_prewarm_enabled_for_claude_when_flag_on(monkeypatch):
    from src.worker.config import WorkerConfig

    _base_worker_env(monkeypatch)
    monkeypatch.setenv("QUOTA_PREWARM_ENABLED", "1")
    assert WorkerConfig.from_env().quota_prewarm_enabled is True


def test_worker_prewarm_off_when_flag_unset(monkeypatch):
    from src.worker.config import WorkerConfig

    _base_worker_env(monkeypatch)  # QUOTA_PREWARM_ENABLED unset
    monkeypatch.delenv("QUOTA_PREWARM_ENABLED", raising=False)
    assert WorkerConfig.from_env().quota_prewarm_enabled is False


def test_worker_prewarm_off_without_claude_backend(monkeypatch):
    from src.worker.config import WorkerConfig

    _base_worker_env(monkeypatch)
    monkeypatch.setenv("WORKER_BACKENDS", "codex")
    monkeypatch.setenv("QUOTA_PREWARM_ENABLED", "1")
    # Warming needs a claude harness to spend the activation turn.
    assert WorkerConfig.from_env().quota_prewarm_enabled is False


def test_controller_can_activate_reflects_adapter_presence():
    """The controller only stands up a prewarmer when it can actually fire one.
    An ingest-only coordinator (adapters == []) must report can-activate False,
    so orchestrator._build_quota_prewarmer skips the inert loop."""
    from src.orchestrator import TaskOrchestrator

    class _Coord:
        def __init__(self, adapters):
            self.adapters = adapters

    class _Activatable:
        async def activate(self, bucket_id="five_hour"):
            return {"ok": True}

    class _Observer:  # no activate() — telemetry-only
        pass

    orch = TaskOrchestrator.__new__(TaskOrchestrator)

    orch.quota_coordinator = _Coord([])                       # ingest_only
    assert orch._coordinator_can_activate() is False

    orch.quota_coordinator = _Coord([_Observer()])            # no activate
    assert orch._coordinator_can_activate() is False

    orch.quota_coordinator = _Coord([_Activatable()])         # real adapter
    assert orch._coordinator_can_activate() is True
