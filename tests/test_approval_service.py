"""Move H Box 2 — ApprovalService gate logic. No network, no paid backend."""
import tempfile, os
import pytest

from src.control.db import MeshDB
from src.services.approval_service import ApprovalService


def _db():
    return MeshDB(db_path=os.path.join(tempfile.mkdtemp(), "h.db"))


class _SpyWorkflow:
    """Records emitted workflow calls instead of emitting real events."""
    def __init__(self):
        self.requested = []
        self.granted = []

    def approval_requested(self, **kw):
        self.requested.append(kw)

    def approval_granted(self, **kw):
        self.granted.append(kw)


def test_request_creates_pending_and_emits():
    db = _db()
    wf = _SpyWorkflow()
    svc = ApprovalService(db, workflow=wf)
    res = svc.request(action="deploy to prod", session_id="s1", risk="high", reversible=False)
    assert res.ok
    appr_id = res.reason  # documented: id rides on reason for request()
    assert appr_id.startswith("appr_")
    pending = svc.pending()
    assert len(pending) == 1 and pending[0]["id"] == appr_id
    assert pending[0]["status"] == "pending"
    assert len(wf.requested) == 1 and wf.requested[0]["session_id"] == "s1"


def test_request_missing_action_rejected():
    svc = ApprovalService(_db(), workflow=_SpyWorkflow())
    res = svc.request(action="", session_id="s1")
    assert not res.ok and res.reason == "missing_action"


@pytest.mark.asyncio
async def test_resolve_approved_flips_state_emits_and_fires_callback():
    db = _db()
    wf = _SpyWorkflow()
    fired = []
    svc = ApprovalService(db, workflow=wf, on_approve=lambda row: fired.append(row["id"]))
    appr_id = svc.request(action="x", session_id="s1").reason

    res = await svc.resolve(appr_id, "approved", resolved_by="me")
    assert res.ok
    assert db.get_approval(appr_id)["status"] == "approved"
    assert len(wf.granted) == 1 and wf.granted[0]["granted"] is True
    assert fired == [appr_id]  # callback fired on approve
    assert svc.pending() == []  # left the queue


@pytest.mark.asyncio
async def test_resolve_rejected_does_not_fire_callback_and_marks_granted_false():
    db = _db()
    wf = _SpyWorkflow()
    fired = []
    svc = ApprovalService(db, workflow=wf, on_approve=lambda row: fired.append(row["id"]))
    appr_id = svc.request(action="x", session_id="s1").reason

    res = await svc.resolve(appr_id, "rejected", resolved_by="me")
    assert res.ok
    assert db.get_approval(appr_id)["status"] == "rejected"
    assert wf.granted[0]["granted"] is False
    assert fired == []  # NOT fired on reject


@pytest.mark.asyncio
async def test_double_resolve_is_already_resolved():
    svc = ApprovalService(_db(), workflow=_SpyWorkflow())
    appr_id = svc.request(action="x", session_id="s1").reason
    assert (await svc.resolve(appr_id, "approved")).ok
    second = await svc.resolve(appr_id, "rejected")
    assert not second.ok and second.reason == "already_resolved"


@pytest.mark.asyncio
async def test_resolve_missing_is_not_found():
    svc = ApprovalService(_db(), workflow=_SpyWorkflow())
    res = await svc.resolve("nope", "approved")
    assert not res.ok and res.reason == "not_found"


@pytest.mark.asyncio
async def test_invalid_decision_rejected():
    svc = ApprovalService(_db(), workflow=_SpyWorkflow())
    appr_id = svc.request(action="x", session_id="s1").reason
    res = await svc.resolve(appr_id, "maybe")
    assert not res.ok and res.reason == "invalid_decision"


@pytest.mark.asyncio
async def test_expired_approval_cannot_be_resolved():
    db = _db()
    svc = ApprovalService(db, workflow=_SpyWorkflow())
    appr_id = svc.request(action="x", session_id="s1").reason
    # Simulate the reaper expiring it.
    assert db.resolve_approval(appr_id, "expired")
    res = await svc.resolve(appr_id, "approved")
    assert not res.ok and res.reason == "already_resolved"
