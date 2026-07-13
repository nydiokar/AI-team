"""[M3.3] Session↔Case affiliation persistence — the clobber-proof contract.

Regression for the live bug found while driving the F1 Manager loop: closing a
Case left ``sessions.current_case_id`` still pointing at the (now closed) Case,
because a generic ``upsert_session`` of a stale in-memory session object (the
Manager's own turn-end persist) overwrote the clear.

The fix: ``current_case_id`` / ``case_role`` are owned EXCLUSIVELY by the targeted
``set_session_case`` writer; ``upsert_session`` no longer touches them on conflict.
"""
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus


def _session(session_id: str, current_case_id=None, case_role=None) -> Session:
    return Session(
        session_id=session_id,
        backend="claude",
        repo_path="/tmp/repo",
        status=SessionStatus.IDLE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        machine_id="host",
        current_case_id=current_case_id,
        case_role=case_role,
    )


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def test_set_session_case_sets_and_clears(tmp_path):
    db = _db(tmp_path)
    db.upsert_session(_session("s1"))

    db.set_session_case("s1", "case-A", "manager")
    row = db.get_session("s1")
    assert row["current_case_id"] == "case-A"
    assert row["case_role"] == "manager"

    db.set_session_case("s1", None, None)
    row = db.get_session("s1")
    assert (row["current_case_id"] or None) is None
    assert (row["case_role"] or None) is None


def test_upsert_session_does_not_clobber_affiliation(tmp_path):
    """The core fix: a full-session save of a STALE object must not overwrite the
    Case affiliation that set_session_case established."""
    db = _db(tmp_path)
    db.upsert_session(_session("s1"))
    db.set_session_case("s1", "case-A", "manager")

    # A stale in-memory object (current_case_id=None, as it was before the attach)
    # gets persisted via the generic path — as the Manager's turn-end save did.
    stale = _session("s1", current_case_id=None, case_role=None)
    stale.last_summary = "turn ended"
    db.upsert_session(stale)

    row = db.get_session("s1")
    # Affiliation SURVIVES the clobbering save…
    assert row["current_case_id"] == "case-A"
    assert row["case_role"] == "manager"
    # …while other fields from the generic save still land.
    assert row["last_summary"] == "turn ended"


def test_set_session_case_authoritative_clear_survives_stale_resave(tmp_path):
    """After a clear, a stale object still carrying the old Case must not re-attach."""
    db = _db(tmp_path)
    db.upsert_session(_session("s1"))
    db.set_session_case("s1", "case-A", "manager")
    db.set_session_case("s1", None, None)  # Case closed

    stale = _session("s1", current_case_id="case-A", case_role="manager")
    db.upsert_session(stale)  # the racy turn-end persist

    row = db.get_session("s1")
    assert (row["current_case_id"] or None) is None
    assert (row["case_role"] or None) is None


def test_set_session_case_unknown_session_is_noop(tmp_path):
    db = _db(tmp_path)
    db.set_session_case("ghost", "case-A", "manager")  # must not raise
    assert db.get_session("ghost") is None
