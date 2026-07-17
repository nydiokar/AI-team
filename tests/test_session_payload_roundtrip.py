"""Round-trip test for the node-dispatch session payload.

Traces ``role_boot`` end-to-end through BOTH sides of the wire:
``_session_dispatch_payload`` (serialize, src/orchestrator.py) →
``_make_session_from_payload`` (deserialize, src/worker/agent.py). A node-pinned
role-ful worker must keep its Worker-role tier opt-in across the dispatch, or it
boots role-less (same carrier-coupling class as the A43 ``case_role`` defect).

The real functions are imported and exercised — nothing is reimplemented here.

Note: ``role_boot`` is set as an instance attribute rather than a constructor
kwarg because the ``Session`` dataclass field lands with the (still-unmerged)
Worker-role layer; the getattr-based carry under test behaves identically.
"""

from src.core.interfaces import Session, SessionStatus
from src.orchestrator import _session_dispatch_payload
from src.worker.agent import _make_session_from_payload


def _make_session(role_boot: object) -> Session:
    session = Session(
        session_id="s-1",
        backend="claude",
        repo_path="/tmp/repo",
        status=SessionStatus.BUSY,
        created_at="2026-07-17T00:00:00+00:00",
        updated_at="2026-07-17T00:00:00+00:00",
    )
    session.case_role = "worker"
    session.role_boot = role_boot
    return session


def test_role_boot_survives_full_roundtrip() -> None:
    sess = _make_session(role_boot="worker")

    payload = {"session": _session_dispatch_payload(sess)}
    reconstructed = _make_session_from_payload(payload)

    assert reconstructed.role_boot == "worker"


def test_role_boot_none_roundtrips_to_none() -> None:
    sess = _make_session(role_boot=None)

    payload = {"session": _session_dispatch_payload(sess)}
    reconstructed = _make_session_from_payload(payload)

    assert reconstructed.role_boot is None


def test_serialized_payload_contains_role_boot_key() -> None:
    sess = _make_session(role_boot="worker")

    serialized = _session_dispatch_payload(sess)

    assert "role_boot" in serialized
    assert serialized["role_boot"] == "worker"
