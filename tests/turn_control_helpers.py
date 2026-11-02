"""Carrier inputs for backend behavioral fixtures; no live backend invocation."""
from __future__ import annotations

from src.core.turn_liveness import turn_control


def session_turn(backend, session, message, ownership, *, on_process=None, **kwargs):
    turn = turn_control(
        ownership.turn_uuid, ownership=ownership, on_process=on_process,
        stall_override=getattr(backend, "_test_stall_sec", None),
    )
    return backend.resume_session(session, message, turn=turn, **kwargs)


def session_compaction(backend, session, ownership, *, on_process=None, **kwargs):
    turn = turn_control(
        ownership.turn_uuid, ownership=ownership, on_process=on_process,
        stall_override=getattr(backend, "_test_stall_sec", None),
    )
    return backend.compact_session(session, turn=turn, **kwargs)
