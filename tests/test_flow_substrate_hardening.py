"""
A29 — Work substrate hardening: deferred write-path seams (flag-guarded, shadow).

Covers the seams A26 deferred to A29:
  * session attachment  — the case's worker session is linked + `session.attached`
    is appended when a flow is created for a task that runs in a session.
  * terminal OUTCOME    — task success closes the case (`flow.closed` + status
    'closed'); a failure blocks it (`flow.status_changed`→'blocked' + status
    'blocked'). `closure` is only a STAGE; this records the real result.
  * approval lifecycle  — an approval on a task with a flow links approval→flow
    and appends `approval.requested` / `approval.resolved`.

Invariants (same as A26): flag OFF ⇒ byte-identical (zero substrate writes); every
write is best-effort/isolated ⇒ a forced failure can NEVER raise into task/approval
execution. Helpers run as real bound methods on a bare orchestrator (``__new__``).
"""
import types

import pytest

from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator
from src.services.approval_service import ApprovalService


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _orch() -> TaskOrchestrator:
    return TaskOrchestrator.__new__(TaskOrchestrator)


def _task(task_id, metadata=None):
    return types.SimpleNamespace(id=task_id, metadata=metadata)


# [A36] Under flag-ON admission an ordinary turn no longer births a Case — only a
# dispatched/managed task does. These A29 seam tests drive the birth machinery,
# so the task is flagged an explicit managed-Case root.
def _managed_task(task_id, metadata=None):
    meta = dict(metadata or {})
    meta[TaskOrchestrator._MANAGED_CASE_META_KEY] = True
    return _task(task_id, meta)


def _result(success=True, error_class=""):
    return types.SimpleNamespace(success=success, error_class=error_class)


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("HARNESS_FLOW_DRIVE", raising=False)


def _patch_db(monkeypatch, db):
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)


# ---------------------------------------------------------------------------
# Session attachment
# ---------------------------------------------------------------------------

def test_on_session_attached_link_and_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    fid = orch._record_flow_run_start(_managed_task("t-1", {"session_id": "sess-abc"}))

    sess_links = db.list_flow_links(flow_run_id=fid, role="worker")
    assert len(sess_links) == 1
    assert (sess_links[0]["entity_type"], sess_links[0]["entity_id"]) == (
        "session", "sess-abc",
    )
    attached = [e for e in db.list_flow_events(fid) if e["event_type"] == "session.attached"]
    assert len(attached) == 1
    assert attached[0]["entity_id"] == "sess-abc"


def test_no_session_id_means_no_session_link(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    fid = orch._record_flow_run_start(_managed_task("t-oneoff", {}))
    assert db.list_flow_links(flow_run_id=fid, entity_type="session") == []
    assert [e for e in db.list_flow_events(fid) if e["event_type"] == "session.attached"] == []


def test_off_writes_no_session_link(tmp_path, monkeypatch):
    db = _db(tmp_path)  # flag unset
    _patch_db(monkeypatch, db)
    orch = _orch()

    fid = orch._record_flow_run_start(_task("t-1", {"session_id": "sess-abc"}))
    assert db.list_flow_links(flow_run_id=fid) == []
    assert db.list_flow_events(fid) == []


# ---------------------------------------------------------------------------
# Terminal OUTCOME
# ---------------------------------------------------------------------------

def test_terminal_success_closes_case(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    task = _managed_task("t-ok", {"session_id": "s"})
    orch._record_flow_run_start(task)
    orch._flow_terminal_outcome(task, success=True)

    fid = task.metadata[TaskOrchestrator._FLOW_RUN_META_KEY]
    assert db.get_flow_run(fid)["status"] == "closed"
    closed = [e for e in db.list_flow_events(fid) if e["event_type"] == "flow.closed"]
    assert len(closed) == 1 and closed[0]["to_state"] == "closed"


def test_terminal_failure_blocks_case(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    task = _managed_task("t-bad", {})
    orch._record_flow_run_start(task)
    orch._flow_terminal_outcome(task, success=False, error_class="timeout")

    fid = task.metadata[TaskOrchestrator._FLOW_RUN_META_KEY]
    assert db.get_flow_run(fid)["status"] == "blocked"
    changed = [e for e in db.list_flow_events(fid) if e["event_type"] == "flow.status_changed"]
    assert len(changed) == 1 and changed[0]["to_state"] == "blocked"


def test_terminal_off_is_noop(tmp_path, monkeypatch):
    db = _db(tmp_path)  # flag unset
    _patch_db(monkeypatch, db)
    orch = _orch()

    fid = db.create_flow_run("t-x", "closure")
    orch._flow_terminal_outcome(
        _task("t-x", {TaskOrchestrator._FLOW_RUN_META_KEY: fid}), success=True,
    )
    # No status write, no events.
    assert db.get_flow_run(fid)["status"] is None
    assert db.list_flow_events(fid) == []


def test_terminal_outcome_write_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    fid = db.create_flow_run("t-boom", "closure")

    def boom(*a, **k):
        raise RuntimeError("outcome boom")

    monkeypatch.setattr(db, "update_flow_run", boom)
    monkeypatch.setattr(db, "append_flow_event", boom)
    _patch_db(monkeypatch, db)
    orch = _orch()

    # Must not raise despite both substrate writes exploding.
    orch._flow_terminal_outcome(
        _task("t-boom", {TaskOrchestrator._FLOW_RUN_META_KEY: fid}), success=True,
    )


# ---------------------------------------------------------------------------
# Approval lifecycle → substrate
# ---------------------------------------------------------------------------

class _SpyWorkflow:
    def approval_requested(self, **kw):
        pass

    def approval_granted(self, **kw):
        pass


def test_approval_request_and_resolve_link_and_events(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    fid = db.create_flow_run("t-appr", "execution")
    svc = ApprovalService(db, workflow=_SpyWorkflow())

    res = svc.request(action="deploy", session_id="s1", task_id="t-appr")
    appr_id = res.reason

    links = db.list_flow_links(flow_run_id=fid, entity_type="approval")
    assert len(links) == 1 and links[0]["entity_id"] == appr_id and links[0]["role"] == "approval"
    types_after_request = [e["event_type"] for e in db.list_flow_events(fid)]
    assert types_after_request == ["approval.requested"]


@pytest.mark.asyncio
async def test_approval_resolve_appends_resolved_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    fid = db.create_flow_run("t-appr", "execution")
    svc = ApprovalService(db, workflow=_SpyWorkflow())

    appr_id = svc.request(action="deploy", session_id="s1", task_id="t-appr").reason
    await svc.resolve(appr_id, "approved", resolved_by="operator1")

    resolved = [e for e in db.list_flow_events(fid) if e["event_type"] == "approval.resolved"]
    assert len(resolved) == 1
    assert resolved[0]["to_state"] == "approved"
    assert resolved[0]["actor"] == "operator"
    # Link is idempotent — still exactly one across request+resolve.
    assert len(db.list_flow_links(flow_run_id=fid, entity_type="approval")) == 1


def test_approval_no_flow_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)  # NO flow_run for the task
    svc = ApprovalService(db, workflow=_SpyWorkflow())

    res = svc.request(action="deploy", session_id="s1", task_id="t-unknown")
    assert res.ok
    # No flow ⇒ nothing linked anywhere; never inferred.
    assert db.list_flow_links(entity_type="approval", entity_id=res.reason) == []


def test_approval_off_is_noop(tmp_path, monkeypatch):
    db = _db(tmp_path)  # flag unset
    fid = db.create_flow_run("t-appr", "execution")
    svc = ApprovalService(db, workflow=_SpyWorkflow())

    svc.request(action="deploy", session_id="s1", task_id="t-appr")
    assert db.list_flow_links(flow_run_id=fid) == []
    assert db.list_flow_events(fid) == []


def test_approval_substrate_failure_does_not_break_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    db.create_flow_run("t-appr", "execution")

    def boom(*a, **k):
        raise RuntimeError("appr boom")

    monkeypatch.setattr(db, "list_flow_runs", boom)
    svc = ApprovalService(db, workflow=_SpyWorkflow())

    # Gate still records the approval despite the substrate write blowing up.
    res = svc.request(action="deploy", session_id="s1", task_id="t-appr")
    assert res.ok
    assert svc.get(res.reason) is not None
