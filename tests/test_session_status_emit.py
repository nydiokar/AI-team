"""SessionStore pushes a `session_status_changed` event on (and only on) a real
status transition — the missing half of the push contract that left the UI
frozen until a poll/remount. Centralized in save() so it can never drift.

No paid CLI, no network.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from src.core.interfaces import SessionStatus
from src.core import observability
from src.services.session_store import SessionStore


def _capture() -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    observability.register_event_forwarder(lambda e: events.append(e))
    return events


def _sess(sid: str, status: SessionStatus) -> SimpleNamespace:
    return SimpleNamespace(session_id=sid, status=status)


def test_emits_on_transition_and_is_idempotent_on_no_change() -> None:
    events = _capture()
    try:
        store = SessionStore()
        store._last_status.clear()

        # First observation of a session => one event (prev None).
        store._emit_status_change(_sess("s1", SessionStatus.BUSY))
        # Same status again => NO event (gated on real change).
        store._emit_status_change(_sess("s1", SessionStatus.BUSY))
        # A genuine transition => one more event carrying previous + new.
        store._emit_status_change(_sess("s1", SessionStatus.IDLE))

        mine = [e for e in events
                if e.get("event") == "session_status_changed" and e.get("session_id") == "s1"]
        assert len(mine) == 2
        assert mine[0]["status"] == "busy"
        assert mine[1]["status"] == "idle"
        assert mine[1]["previous_status"] == "busy"
    finally:
        observability.register_event_forwarder(None)


def test_event_carries_session_id_for_live_invalidation() -> None:
    # The frontend keys its live refresh off any event with a session_id; assert
    # it's always present so the durable-state panel actually gets invalidated.
    events = _capture()
    try:
        store = SessionStore()
        store._last_status.clear()
        store._emit_status_change(_sess("s2", SessionStatus.AWAITING_INPUT))
        mine = [e for e in events if e.get("event") == "session_status_changed"]
        assert mine and all(e.get("session_id") for e in mine)
    finally:
        observability.register_event_forwarder(None)
