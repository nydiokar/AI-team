"""A83 — SessionView wiring is ADDITIVE: `needs_input`/`is_active` stay
byte-identical and the reason serializes as a sibling field (or null)."""
from __future__ import annotations

from src.core.interfaces import Session, SessionStatus
from src.core.view_models import SessionView
from src.core.session_reason import SessionReason


def _session(status: SessionStatus) -> Session:
    return Session(
        session_id="s1",
        backend="claude",
        repo_path="/tmp/repo",
        status=status,
        created_at="2026-09-25T00:00:00Z",
        updated_at="2026-09-25T00:00:00Z",
        machine_id="node_a",
    )


def test_from_session_leaves_reason_none_and_needs_input_is_active_unchanged():
    for status in SessionStatus:
        v = SessionView.from_session(_session(status))
        # These formulas must be byte-identical to pre-A83.
        assert v.needs_input == (status == SessionStatus.AWAITING_INPUT)
        assert v.is_active == (
            status
            not in (
                SessionStatus.CLOSED,
                SessionStatus.ERROR,
                SessionStatus.PINNED_NODE_OFFLINE,
            )
        )
        # Plain from_session never derives a reason.
        assert v.reason is None
        assert v.to_dict()["reason"] is None


def test_with_reason_attaches_and_serializes_nested_shape():
    v = SessionView.from_session(_session(SessionStatus.AWAITING_INPUT))
    r = SessionReason(kind="waiting_workers", confidence="high")
    v2 = v.with_reason(r)
    # Original is untouched (frozen DTO, `replace` returns a copy).
    assert v.reason is None
    assert v2.reason == r
    # needs_input/is_active unchanged by attaching a reason.
    assert v2.needs_input == v.needs_input
    assert v2.is_active == v.is_active
    d = v2.to_dict()
    assert d["reason"] == {"kind": "waiting_workers", "confidence": "high", "detail": None}
    # Every other key is identical to the no-reason dict.
    base = v.to_dict()
    for k in base:
        if k == "reason":
            continue
        assert d[k] == base[k]


def test_with_reason_none_serializes_null():
    v = SessionView.from_session(_session(SessionStatus.BUSY)).with_reason(None)
    assert v.to_dict()["reason"] is None
