"""Unit tests for MeshHealth sliding-window degradation detection.

Covers the core contract: single failures don't trigger degradation,
N consecutive failures do, and recovery is immediate on success.
"""
from src.control.mesh_health import MeshHealth


def test_initial_state_is_healthy():
    h = MeshHealth()
    assert h.is_healthy() is True
    assert h.is_degraded() is False


def test_single_failure_does_not_degrade():
    h = MeshHealth(window_size=6, failure_threshold=3)
    h.record_check(False)
    assert h.is_healthy() is True
    assert h.is_degraded() is False


def test_two_failures_still_healthy():
    h = MeshHealth(window_size=6, failure_threshold=3)
    h.record_check(False)
    h.record_check(False)
    assert h.is_healthy() is True
    assert h.is_degraded() is False


def test_three_consecutive_failures_triggers_degradation():
    h = MeshHealth(window_size=6, failure_threshold=3)
    for _ in range(3):
        h.record_check(False)
    assert h.is_healthy() is False
    assert h.is_degraded() is True


def test_four_consecutive_failures_stays_degraded():
    h = MeshHealth(window_size=6, failure_threshold=3)
    for _ in range(4):
        h.record_check(False)
    assert h.is_degraded() is True


def test_single_success_clears_degradation():
    h = MeshHealth(window_size=6, failure_threshold=3)
    for _ in range(3):
        h.record_check(False)
    assert h.is_degraded() is True
    h.record_check(True)
    assert h.is_healthy() is True
    assert h.is_degraded() is False


def test_transition_events_emit_once(monkeypatch):
    events = []

    def fake_emit_event(name, **fields):
        events.append((name, fields))

    monkeypatch.setattr("src.core.observability.emit_event", fake_emit_event)

    h = MeshHealth(window_size=6, failure_threshold=3)
    h.record_check(False)
    h.record_check(False)
    assert events == []

    h.record_check(False)
    h.record_check(False)
    assert [name for name, _fields in events] == ["mesh_degraded"]

    h.record_check(True)
    h.record_check(True)
    assert [name for name, _fields in events] == ["mesh_degraded", "mesh_restored"]
    assert events[-1][1]["consecutive_failures"] == 0


def test_mixed_pattern_does_not_degrade():
    """Alternating success/failure never reaches consecutive threshold."""
    h = MeshHealth(window_size=6, failure_threshold=3)
    for _ in range(10):
        h.record_check(True)
        h.record_check(False)
    assert h.is_healthy() is True


def test_reset_clears_state():
    h = MeshHealth(window_size=6, failure_threshold=3)
    for _ in range(3):
        h.record_check(False)
    assert h.is_degraded() is True
    h.reset()
    assert h.is_healthy() is True
    assert h.is_degraded() is False
    assert h.stats()["consecutive_failures"] == 0


def test_stats_shape():
    h = MeshHealth(window_size=3, failure_threshold=2)
    h.record_check(True)
    h.record_check(False)
    stats = h.stats()
    assert stats["window_size"] == 3
    assert stats["failure_threshold"] == 2
    assert stats["consecutive_failures"] == 1
    assert stats["degraded"] is False
    assert len(stats["recent_checks"]) == 2
    assert stats["recent_checks"] == [True, False]


def test_window_evicts_oldest():
    h = MeshHealth(window_size=3, failure_threshold=3)
    for _ in range(3):
        h.record_check(True)
    assert h.is_healthy() is True
    # Fill window with failures - oldest successes evicted
    for _ in range(3):
        h.record_check(False)
    assert h.is_degraded() is True
    # Window now contains [False, False, False]
    assert list(h._results) == [False, False, False]


def test_custom_threshold():
    h = MeshHealth(window_size=10, failure_threshold=1)
    h.record_check(False)
    assert h.is_degraded() is True
    h.record_check(True)
    assert h.is_degraded() is False


def test_get_mesh_health_singleton(monkeypatch):
    """Module-level singleton respects config, returns same instance."""
    import config.settings as _s
    monkeypatch.setattr(_s.config.mesh, "mesh_health_window_size", 10)
    monkeypatch.setattr(_s.config.mesh, "mesh_health_failure_threshold", 5)

    from src.control.mesh_health import get_mesh_health, _health

    _health = None  # force re-creation from config
    import importlib
    mod = importlib.import_module("src.control.mesh_health")
    mod._health = None

    h1 = get_mesh_health()
    h2 = get_mesh_health()
    assert h1 is h2  # same instance
