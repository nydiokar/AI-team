"""OPT-3 — stop the stale/wedged-Case pileup.

Three defects, each pinned here against a real temp MeshDB:
  1. a pending approval whose ``expires_at`` has passed no longer blocks a close,
     and ``expire_stale_approvals`` flips it to 'expired' (the TTL the schema
     always carried but nothing enforced);
  2. an explicit close can cancel a Case's dangling pending approvals
     (``resolve_pending_approvals`` / ``force``) instead of refusing forever;
  3. a forced close waives the criteria/rework gates (orphan cleanup: no agent
     remains to meet them).
"""

from datetime import datetime, timedelta, timezone

import pytest

import asyncio
import types

from src.control.db import MeshDB, CaseCloseBlocked
from src.core import SessionStatus
from src.orchestrator import TaskOrchestrator


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _link_pending(db: MeshDB, fid: str, approval_id: str, *, expires_at=None) -> None:
    db.create_approval(approval_id, action="case_manager_respawn", expires_at=expires_at)
    db.create_flow_link(fid, "approval", approval_id, "approval", created_by="system")


def test_expired_approval_does_not_block_close(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    _link_pending(db, fid, "appr-old", expires_at=_past())
    # Past-due pending approval must NOT wedge the close.
    assert db._case_has_unresolved_approval(fid) is False
    assert db.close_case(fid) is True


def test_unexpired_and_null_expiry_still_block(tmp_path):
    db = _db(tmp_path)
    f1 = db.open_case("obj", "sess-1")
    _link_pending(db, f1, "appr-future", expires_at=_future())
    assert db._case_has_unresolved_approval(f1) is True
    with pytest.raises(CaseCloseBlocked):
        db.close_case(f1)

    f2 = db.open_case("obj", "sess-2")
    _link_pending(db, f2, "appr-null", expires_at=None)  # open-ended still blocks
    assert db._case_has_unresolved_approval(f2) is True


def test_expire_stale_approvals_flips_only_past_due(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    _link_pending(db, fid, "appr-old", expires_at=_past())
    _link_pending(db, fid, "appr-future", expires_at=_future())
    _link_pending(db, fid, "appr-null", expires_at=None)

    assert db.expire_stale_approvals() == 1
    assert db.get_approval("appr-old")["status"] == "expired"
    assert db.get_approval("appr-future")["status"] == "pending"
    assert db.get_approval("appr-null")["status"] == "pending"
    # Idempotent — nothing left past-due.
    assert db.expire_stale_approvals() == 0


def test_cancel_case_pending_approvals(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    _link_pending(db, fid, "appr-1", expires_at=_future())
    _link_pending(db, fid, "appr-2", expires_at=None)

    n = db.cancel_case_pending_approvals(fid, resolved_by="operator")
    assert n == 2
    assert db.get_approval("appr-1")["status"] == "cancelled"
    assert db.get_approval("appr-2")["status"] == "cancelled"
    assert db._case_has_unresolved_approval(fid) is False
    # Idempotent — nothing pending now.
    assert db.cancel_case_pending_approvals(fid) == 0


def test_resolve_pending_approvals_unwedges_close(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    _link_pending(db, fid, "appr-1", expires_at=None)  # open-ended: normally wedges

    with pytest.raises(CaseCloseBlocked):
        db.close_case(fid)  # default: still blocked

    assert db.close_case(fid, resolve_pending_approvals=True) is True
    assert db.get_approval("appr-1")["status"] == "cancelled"


def test_force_close_waives_criteria_and_approvals(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case(
        "obj", "sess-1", completion_criteria='["tests green", "deployed"]',
    )
    _link_pending(db, fid, "appr-1", expires_at=None)

    # Normal close: blocked by both the approval and the unmet criteria.
    with pytest.raises(CaseCloseBlocked):
        db.close_case(fid)

    # Forced orphan cleanup: approvals cancelled, criteria waived.
    assert db.close_case(fid, outcome="cancelled", force=True) is True
    assert db.get_flow_run(fid)["status"] == "cancelled"
    assert db.get_approval("appr-1")["status"] == "cancelled"


class _StubStore:
    def __init__(self, sessions):
        self._d = dict(sessions)

    def get(self, sid):
        return self._d.get(sid)


def test_sweep_disposition_terminal_vs_resumable(tmp_path, monkeypatch):
    """A CLOSED-manager Case is a terminal orphan (force_close); a
    pinned-node-offline one stays resumable (interrupt). dry_run exposes the
    decision without mutating state."""
    db = _db(tmp_path)
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    dead = db.open_case("obj", "mgr-dead", role="manager")
    offline = db.open_case("obj", "mgr-offline", role="manager")

    store = _StubStore({
        "mgr-dead": types.SimpleNamespace(status=SessionStatus.CLOSED),
        "mgr-offline": types.SimpleNamespace(status=SessionStatus.PINNED_NODE_OFFLINE),
    })
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    orch.session_store = store

    res = asyncio.run(orch.sweep_orphaned_cases(dry_run=True))
    disp = {c["case_id"]: c["disposition"] for c in res["candidates"]}
    assert disp[dead] == "force_close"
    assert disp[offline] == "interrupt"
    # dry_run must not change anything.
    assert db.get_flow_run(dead)["status"] in (None, "", "open")
