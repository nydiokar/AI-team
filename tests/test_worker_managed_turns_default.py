"""[A82 Stage 8a cutover] WORKER_MANAGED_TURNS defaults ON.

Both sites that read the flag must agree: empty/unset enables managed turns;
only an explicit false value (0/false/no/off) opts a worker back out. A split
between the two readers would leave a worker's config-level ``managed_turns``
and its driver-level echo-replay disagreeing, so they are asserted together.
"""
from __future__ import annotations

import pytest

from src.backends.claude_driver import _replay_user_messages_enabled
from src.worker.config import WorkerConfig


def _min_env(monkeypatch) -> None:
    """The required (non-flag) env WorkerConfig.from_env reads."""
    monkeypatch.setenv("WORKER_NODE_ID", "test-node")
    monkeypatch.setenv("WORKER_TOKEN", "test-token")
    monkeypatch.setenv("WORKER_TAILSCALE_IP", "100.0.0.1")
    monkeypatch.setenv("CONTROLLER_URL", "http://127.0.0.1:9003")
    monkeypatch.setenv("WORKER_BACKENDS", "claude")


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),      # unset  -> ON (cutover default)
        ("", True),        # empty  -> ON
        ("1", True),       # explicit truthy -> ON
        ("true", True),
        ("0", False),      # explicit false -> OFF
        ("false", False),
        ("no", False),
        ("off", False),
    ],
)
def test_managed_turns_default_on(monkeypatch, value, expected):
    _min_env(monkeypatch)
    if value is None:
        monkeypatch.delenv("WORKER_MANAGED_TURNS", raising=False)
    else:
        monkeypatch.setenv("WORKER_MANAGED_TURNS", value)

    # Config site (src/worker/config.py)
    assert WorkerConfig.from_env().managed_turns is expected
    # Driver site (src/backends/claude_driver.py) — must mirror, no split-brain
    assert _replay_user_messages_enabled() is expected
