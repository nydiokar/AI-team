"""Carrier-independent Manager role — the dispatch-seam `case_role` propagation.

Root cause of the A43 finding (a node-pinned Manager boots bare/role-less): the
task dispatch payload dropped `session.case_role`, so the node reconstructed a
Session with `case_role=None` and the driver's `_role_boot` fell through to the
default path (no role prompt, no scoped manager tools).

These tests pin the seam BOTH ways — the producer (`_session_dispatch_payload`)
must carry it, and the consumer (`_make_session_from_payload`) must restore it —
plus a regression guard that a role-less session stays byte-identical (case_role
absent ⇒ None, the default path).
"""
from datetime import datetime, timezone

from src.core.interfaces import Session, SessionStatus


def _session(case_role=None, current_case_id=None) -> Session:
    now = datetime.now(tz=timezone.utc).isoformat()
    return Session(
        session_id="s-1",
        backend="claude",
        repo_path="/tmp/repo",
        status=SessionStatus.BUSY,
        created_at=now,
        updated_at=now,
        machine_id="kanebra-worker",
        last_user_message="You are being invoked as the Manager...",
        current_case_id=current_case_id,
        case_role=case_role,
    )


def test_dispatch_payload_carries_case_role():
    from src.orchestrator import _session_dispatch_payload

    payload = _session_dispatch_payload(_session(case_role="manager", current_case_id="case-abc"))
    assert payload["case_role"] == "manager"
    assert payload["current_case_id"] == "case-abc"


def test_dispatch_payload_role_less_session_is_none():
    """Regression: a standalone (role-less) session serializes case_role=None —
    the default path stays byte-identical for the 99% non-managed dispatch."""
    from src.orchestrator import _session_dispatch_payload

    payload = _session_dispatch_payload(_session())
    assert payload["case_role"] is None
    assert payload["current_case_id"] is None


def test_make_session_from_payload_restores_case_role():
    from src.worker.agent import _make_session_from_payload

    restored = _make_session_from_payload(
        {"session": {"session_id": "s-1", "backend": "claude", "case_role": "manager",
                     "current_case_id": "case-abc"}}
    )
    assert restored.case_role == "manager"
    assert restored.current_case_id == "case-abc"


def test_make_session_from_payload_absent_case_role_is_none():
    """Regression: a payload with no case_role restores to None (default),
    exactly as before this fix — no spurious role boot on ordinary workers."""
    from src.worker.agent import _make_session_from_payload

    restored = _make_session_from_payload(
        {"session": {"session_id": "s-1", "backend": "claude"}}
    )
    assert restored.case_role is None


def test_seam_round_trip_preserves_manager_role():
    """End-to-end of the dispatch seam: produce → consume must preserve the
    Manager role so the node's `_role_boot` sees case_role=='manager'."""
    from src.orchestrator import _session_dispatch_payload
    from src.worker.agent import _make_session_from_payload

    payload = _session_dispatch_payload(_session(case_role="manager", current_case_id="case-xyz"))
    restored = _make_session_from_payload({"session": payload})
    assert restored.case_role == "manager"
    assert restored.current_case_id == "case-xyz"
