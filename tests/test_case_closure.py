"""
A37 — M2.5 Case continuity & closure semantics tests.

Makes ``Task finished != Case completed`` real:
  * a task's terminal outcome records `task.finished` and leaves the Case OPEN
    (covered in test_flow_substrate_hardening);
  * ``db.close_case`` is the ONLY status→terminal write path — an authoritative,
    guarded closer that refuses while an approval is pending, a child flow is
    open, or completion_criteria are unmet/unwaived;
  * pause/resume reuse the same flow_run_id (no replacement Case), which the A36
    admission already guarantees.

db-level primitives run on a real temp MeshDB; the orchestrator seam runs as a
real bound method on a bare orchestrator (``__new__``).
"""

import types

import pytest

from src.control.db import (
    MeshDB,
    CaseCloseBlocked,
    _parse_completion_criteria,
    _criterion_resolved,
    _unreconciled_criteria,
)
from src.orchestrator import TaskOrchestrator


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _orch() -> TaskOrchestrator:
    return TaskOrchestrator.__new__(TaskOrchestrator)


def _patch_db(monkeypatch, db):
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)


class _StubStore:
    def __init__(self):
        self._d = {}

    def get(self, sid):
        return self._d.get(sid)

    def save(self, session):
        self._d[session.session_id] = session


def _session(session_id, case_id=None, role=None):
    return types.SimpleNamespace(
        session_id=session_id, current_case_id=case_id, case_role=role,
    )


# ---------------------------------------------------------------------------
# Pure completion-criteria helpers
# ---------------------------------------------------------------------------

def test_parse_criteria_json_list():
    assert _parse_completion_criteria('["a", "b", " "]') == ["a", "b"]


def test_parse_criteria_plain_string():
    assert _parse_completion_criteria("ship it") == ["ship it"]


def test_parse_criteria_empty():
    assert _parse_completion_criteria(None) == []
    assert _parse_completion_criteria("   ") == []


def test_criterion_resolved_rules():
    assert _criterion_resolved({"criterion": "x", "status": "met"}) is True
    assert _criterion_resolved({"criterion": "x", "status": "waived", "reason": "n/a"}) is True
    assert _criterion_resolved({"criterion": "x", "status": "waived"}) is False  # no reason
    assert _criterion_resolved({"criterion": "x", "status": "pending"}) is False
    assert _criterion_resolved("not-a-dict") is False
    # [A39] boolean shorthand also honored (a Manager must not brick a Case on a format guess).
    assert _criterion_resolved({"criterion": "x", "met": True}) is True
    assert _criterion_resolved({"criterion": "x", "met": False}) is False   # explicit not-met
    assert _criterion_resolved({"criterion": "x", "waived": True, "reason": "oos"}) is True
    assert _criterion_resolved({"criterion": "x", "waived": True}) is False  # waiver still needs a reason


def test_unreconciled_criteria():
    raw = '["a", "b"]'
    # both met ⇒ none unresolved
    assert _unreconciled_criteria(raw, [
        {"criterion": "a", "status": "met"},
        {"criterion": "b", "status": "waived", "reason": "ok"},
    ]) == []
    # b missing ⇒ b unresolved
    assert _unreconciled_criteria(raw, [{"criterion": "a", "status": "met"}]) == ["b"]
    # no criteria ⇒ nothing to reconcile
    assert _unreconciled_criteria(None, None) == []


# ---------------------------------------------------------------------------
# db.close_case — happy path + idempotency + validation
# ---------------------------------------------------------------------------

def test_close_case_success(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    assert db.close_case(fid, actor="operator") is True

    row = db.get_flow_run(fid)
    assert row["status"] == "closed"
    closed = [e for e in db.list_flow_events(fid) if e["event_type"] == "flow.closed"]
    assert len(closed) == 1 and closed[0]["to_state"] == "closed"
    assert closed[0]["actor"] == "operator"


def test_close_case_idempotent(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    assert db.close_case(fid) is True
    # Second close is a no-op — returns False, no duplicate event.
    assert db.close_case(fid) is False
    closed = [e for e in db.list_flow_events(fid) if e["event_type"] == "flow.closed"]
    assert len(closed) == 1


def test_close_case_cancel_outcome(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    assert db.close_case(fid, outcome="cancelled") is True
    assert db.get_flow_run(fid)["status"] == "cancelled"
    changed = [e for e in db.list_flow_events(fid) if e["event_type"] == "flow.status_changed"]
    assert len(changed) == 1 and changed[0]["to_state"] == "cancelled"


def test_close_case_invalid_outcome(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    with pytest.raises(ValueError):
        db.close_case(fid, outcome="done")  # not a terminal status


def test_close_case_unknown_id(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        db.close_case("no-such-case")


# ---------------------------------------------------------------------------
# db.close_case — guards
# ---------------------------------------------------------------------------

def test_close_case_blocked_by_open_child(tmp_path):
    db = _db(tmp_path)
    parent = db.open_case("parent", "sess-1")
    child = db.create_flow_run("t-child", "intent", parent_flow_run_id=parent)
    db.create_flow_link(parent, "flow", child, "child_flow", created_by="system")

    with pytest.raises(CaseCloseBlocked):
        db.close_case(parent)

    # Close the child, then the parent closes.
    db.close_case(child)
    assert db.close_case(parent) is True


def test_close_case_blocked_by_pending_approval(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    db.create_approval("appr-1", action="delete", task_id="t-1")
    db.create_flow_link(fid, "approval", "appr-1", "approval", created_by="system")

    with pytest.raises(CaseCloseBlocked):
        db.close_case(fid)

    # Resolve the approval → the Case can close.
    db.resolve_approval("appr-1", "approved")
    assert db.close_case(fid) is True


def test_close_case_requires_criteria_reconciliation(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1", completion_criteria='["tests green", "deployed"]')

    # No reconciliation ⇒ refused.
    with pytest.raises(CaseCloseBlocked):
        db.close_case(fid)

    # Partial ⇒ still refused.
    with pytest.raises(CaseCloseBlocked):
        db.close_case(fid, criteria_reconciliation=[{"criterion": "tests green", "status": "met"}])

    # Full (one met, one waived-with-reason) ⇒ closes, reconciliation persisted.
    assert db.close_case(fid, criteria_reconciliation=[
        {"criterion": "tests green", "status": "met"},
        {"criterion": "deployed", "status": "waived", "reason": "staging only"},
    ]) is True
    assert db.get_flow_run(fid)["status"] == "closed"
    import json as _json
    ev = [e for e in db.list_flow_events(fid) if e["event_type"] == "flow.closed"][0]
    assert _json.loads(ev["payload_json"])["reconciliation"][0]["criterion"] == "tests green"


def test_close_case_waive_without_reason_refused(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1", completion_criteria="ship it")
    with pytest.raises(CaseCloseBlocked):
        db.close_case(fid, criteria_reconciliation=[{"criterion": "ship it", "status": "waived"}])


# ---------------------------------------------------------------------------
# orchestrator.close_case seam
# ---------------------------------------------------------------------------

def test_orch_close_case_ok_and_clears_affiliation(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()
    store = _StubStore()
    orch.session_store = store

    fid = db.open_case("obj", "sess-1", role="manager")
    store.save(_session("sess-1", case_id=fid, role="manager"))

    res = orch.close_case(fid, actor="operator")
    assert res == {"ok": True, "closed": True, "reason": None}
    assert db.get_flow_run(fid)["status"] == "closed"
    # Durable affiliation cleared on close.
    session = store.get("sess-1")
    assert session.current_case_id is None and session.case_role is None


def test_orch_close_case_blocked_returns_reason(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()
    orch.session_store = _StubStore()

    fid = db.open_case("obj", "sess-1", completion_criteria="ship it")
    res = orch.close_case(fid)
    assert res["ok"] is False and res["closed"] is False
    assert "completion_criteria" in res["reason"]
    assert db.get_flow_run(fid)["status"] is None  # still open


def test_orch_close_case_unknown_returns_reason(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()
    orch.session_store = _StubStore()

    res = orch.close_case("no-such-case")
    assert res["ok"] is False and "unknown case" in res["reason"]


def test_orch_close_case_does_not_clear_moved_on_session(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()
    store = _StubStore()
    orch.session_store = store

    fid = db.open_case("obj", "sess-1", role="manager")
    # Session already moved on to a different Case.
    store.save(_session("sess-1", case_id="other-case", role="worker"))

    orch.close_case(fid)
    session = store.get("sess-1")
    assert session.current_case_id == "other-case"  # untouched


# ---------------------------------------------------------------------------
# Pause/resume reuse the same flow_run_id (A36 admission guarantee)
# ---------------------------------------------------------------------------

def test_resume_reuses_same_case(tmp_path, monkeypatch):
    """A paused Case (status NULL/'blocked') is still OPEN, so a resuming turn
    re-attaches to the SAME flow_run_id — no replacement Case is minted."""
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    fid = db.open_case("obj", "sess-1")
    turn1 = types.SimpleNamespace(id="t-1", metadata={"session_id": "sess-1"})
    orch._record_flow_run_start(turn1)

    # "Pause": the Case is left open (no close). A later resume turn re-finds it.
    assert db.find_open_case_for_session("sess-1") == fid
    turn2 = types.SimpleNamespace(id="t-2", metadata={"session_id": "sess-1"})
    orch._record_flow_run_start(turn2)

    assert len(db.list_flow_runs()) == 1  # no replacement Case
    assert len(db.list_flow_links(flow_run_id=fid, entity_type="task", role="task")) == 2
