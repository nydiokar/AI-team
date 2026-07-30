"""Characterization tests for `src.worker.agent._ActivityForwarder` (A59).

`_ActivityForwarder` is live remote-worker code that arrived undispatched via the
A17 WIP snapshot (`d1556ad`) with zero tests. Decision: keep-with-tests (it is live
working code — reverting is the riskier move). These tests lock its contract:

  - it forwards only `task_activity` events, and only well-formed ones;
  - a transport failure is swallowed and the forwarder keeps running (a telemetry
    hiccup must never crash or stall the worker's SDK stream thread);
  - it is non-blocking under backpressure — a full queue drops the live signal
    rather than blocking the caller (§7 service-boundary answer: bounded queue,
    single daemon thread, best-effort drop, 3s post timeout).

All offline — no real HTTP, no WorkerAgent, no gateway.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.worker.agent import _ActivityForwarder


class _FakeHTTP:
    """Records POSTs; can fail once, or gate the consumer thread on demand."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Any, int]] = []
        self._lock = threading.Lock()
        self.fail_once = False
        self._failed = False
        self.gate: Optional[threading.Event] = None  # if set, post() blocks until released

    def post(self, path: str, body: Any = None, timeout: int = 10) -> Any:
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.fail_once and not self._failed:
            self._failed = True
            raise RuntimeError("simulated transport failure")
        with self._lock:
            self.calls.append((path, body, timeout))
        return {"ok": True}

    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)


def _activity(**over: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "event": "task_activity",
        "session_id": "s" * 12,
        "task_id": "task_abc",
        "turn_id": "turn_1",
        "label": "Using Bash",
    }
    payload.update(over)
    return payload


def _wait_for(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_forwards_wellformed_task_activity():
    http = _FakeHTTP()
    fwd = _ActivityForwarder(http, node_id="kanebra")
    fwd.offer(_activity())
    assert _wait_for(lambda: http.call_count() == 1), "activity was not forwarded"
    path, body, timeout = http.calls[0]
    assert path == "/events/activity"
    assert body["node_id"] == "kanebra"
    assert body["session_id"] == "s" * 12
    assert body["task_id"] == "task_abc"
    assert body["label"] == "Using Bash"
    assert timeout == 3  # bounded post timeout — never hangs the sender


@pytest.mark.parametrize(
    "payload",
    [
        {"event": "something_else", "session_id": "s", "label": "x"},  # wrong event type
        _activity(session_id=None, task_id=None),                      # no id at all
        _activity(label=None),                                         # no label
        _activity(label=""),                                           # empty label
    ],
)
def test_ignores_non_activity_and_malformed(payload):
    http = _FakeHTTP()
    fwd = _ActivityForwarder(http, node_id="kanebra")
    fwd.offer(payload)
    # then a valid one — only the valid one must be delivered
    fwd.offer(_activity(label="Thinking…"))
    assert _wait_for(lambda: http.call_count() == 1)
    assert http.calls[0][1]["label"] == "Thinking…"


def test_transport_failure_is_swallowed_and_forwarder_survives():
    http = _FakeHTTP()
    http.fail_once = True
    fwd = _ActivityForwarder(http, node_id="kanebra")
    # first offer's POST raises inside the daemon thread and must be swallowed
    fwd.offer(_activity(label="first"))
    # a subsequent event must still be delivered — the thread did not die
    fwd.offer(_activity(label="second"))
    assert _wait_for(lambda: http.call_count() == 1), "forwarder died on a transport error"
    assert http.calls[0][1]["label"] == "second"


def test_offer_is_non_blocking_under_backpressure():
    # Hold the consumer inside post(); with max_queue=1 the queue fills immediately.
    http = _FakeHTTP()
    http.gate = threading.Event()
    fwd = _ActivityForwarder(http, node_id="kanebra", max_queue=1)
    try:
        # Offer far more than the queue can hold; offer() must never block or raise.
        start = time.monotonic()
        for i in range(200):
            fwd.offer(_activity(label=f"evt-{i}"))
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, "offer() blocked under backpressure instead of dropping"
    finally:
        http.gate.set()  # release the consumer so the daemon can drain/exit cleanly


def test_offer_before_any_consumer_progress_does_not_raise():
    # Pure enqueue path: even with the consumer gated, a well-formed offer returns.
    http = _FakeHTTP()
    http.gate = threading.Event()
    fwd = _ActivityForwarder(http, node_id="kanebra", max_queue=8)
    try:
        fwd.offer(_activity())  # should not raise
    finally:
        http.gate.set()
