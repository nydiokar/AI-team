"""
A47 — Case observability: the worker SESSION as a first-class Case-graph node.

When a dispatched worker task JOINs a Manager's Case (admission branch J, under
``HARNESS_FLOW_DRIVE`` ON) the orchestrator now ALSO writes a durable
``flow_links(entity_type='session', role='worker')`` row — mirroring the manager
session link ``open_case`` writes — and marks the worker's TASK link
``created_by='manager'`` so the Case graph tells it apart from the Manager's own
-turn attach (branch B, ``created_by='system'``).

Hermetic: a real ``MeshDB`` in a temp dir + the admission method run as a bound
method on a bare orchestrator (``__new__``). No SDK boot, no network, no backend.
Mirrors the ``tests/test_case_admission.py`` harness.
"""

import types

import pytest

from src.control.db import MeshDB
from src.control import work_read_model as wrm
from src.orchestrator import TaskOrchestrator


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _orch() -> TaskOrchestrator:
    return TaskOrchestrator.__new__(TaskOrchestrator)


class _StubStore:
    """Minimal in-memory session store for affiliation assertions."""

    def __init__(self) -> None:
        self._d: dict = {}

    def get(self, sid):
        return self._d.get(sid)

    def save(self, session) -> None:
        self._d[session.session_id] = session


def _session(session_id):
    return types.SimpleNamespace(
        session_id=session_id, current_case_id=None, case_role=None,
    )


def _join_task(task_id, session_id, case_id):
    """A dispatched worker task JOINing a named Case (admission branch J)."""
    return types.SimpleNamespace(
        id=task_id,
        metadata={
            "session_id": session_id,
            TaskOrchestrator._JOIN_CASE_META_KEY: case_id,
        },
    )


def _turn(task_id, session_id):
    """The Manager's own ordinary turn (admission branch B)."""
    return types.SimpleNamespace(id=task_id, metadata={"session_id": session_id})


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("HARNESS_FLOW_DRIVE", raising=False)


def _patch_db(monkeypatch, db) -> None:
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)


def _wired_orch(monkeypatch, db, *session_ids):
    _patch_db(monkeypatch, db)
    orch = _orch()
    store = _StubStore()
    for sid in session_ids:
        store.save(_session(sid))
    orch.session_store = store
    return orch


# ---------------------------------------------------------------------------
# (a) a worker JOIN writes the durable flow_links(session, role='worker') row
# ---------------------------------------------------------------------------

def test_worker_join_writes_session_link(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    orch = _wired_orch(monkeypatch, db, "mgr-sess", "wrk-sess")

    fid = db.open_case("ship it", "mgr-sess", role="manager")
    assert orch._record_flow_run_start(_join_task("wt-1", "wrk-sess", fid)) is None

    sess_links = db.list_flow_links(flow_run_id=fid, entity_type="session")
    by_sid = {l["entity_id"]: l for l in sess_links}
    # Manager session link (from open_case) AND the new worker session link.
    assert by_sid["mgr-sess"]["role"] == "manager"
    assert "wrk-sess" in by_sid, "worker session absent from the Case graph"
    assert by_sid["wrk-sess"]["role"] == "worker"
    assert by_sid["wrk-sess"]["created_by"] == "manager"

    # The worker task link carries the honest provenance marker (branch J).
    task_links = db.list_flow_links(flow_run_id=fid, entity_type="task")
    assert [(l["entity_id"], l["role"], l["created_by"]) for l in task_links] == [
        ("wt-1", "task", "manager"),
    ]

    # Durable affiliation is stamped on the worker session too.
    wrk = orch.session_store.get("wrk-sess")
    assert wrk.current_case_id == fid
    assert wrk.case_role == "worker"


# ---------------------------------------------------------------------------
# (b) a repeated JOIN of the same worker session is idempotent (no dup row)
# ---------------------------------------------------------------------------

def test_worker_join_session_link_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    orch = _wired_orch(monkeypatch, db, "mgr-sess", "wrk-sess")

    fid = db.open_case("ship it", "mgr-sess", role="manager")
    # Same worker session joins the same Case twice (e.g. T1 then T2).
    orch._record_flow_run_start(_join_task("wt-1", "wrk-sess", fid))
    orch._record_flow_run_start(_join_task("wt-2", "wrk-sess", fid))

    worker_sess_links = db.list_flow_links(
        flow_run_id=fid, entity_type="session", role="worker",
    )
    assert len(worker_sess_links) == 1  # NOT duplicated on the second join

    # Each join is still its own distinct task link (2 tasks, one session).
    task_ids = {
        l["entity_id"]
        for l in db.list_flow_links(flow_run_id=fid, entity_type="task")
    }
    assert task_ids == {"wt-1", "wt-2"}


# ---------------------------------------------------------------------------
# (c) the Case read-model surfaces the worker session + task DISTINCT from the
#     Manager's own turn — end-to-end through build_case_detail (what /api/work
#     /{case} feeds), not just the raw link rows.
# ---------------------------------------------------------------------------

def test_case_view_distinguishes_worker_from_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    orch = _wired_orch(monkeypatch, db, "mgr-sess", "wrk-sess")

    fid = db.open_case("ship it", "mgr-sess", role="manager")
    # Manager works its own turn (branch B attach).
    orch._record_flow_run_start(_turn("mgr-turn", "mgr-sess"))
    # Manager dispatches a worker that JOINs the same Case (branch J).
    orch._record_flow_run_start(_join_task("wrk-turn", "wrk-sess", fid))

    # Feed the projection the SAME rows the control API feeds it.
    flow = db.get_flow_run(fid)
    links = db.list_flow_links(flow_run_id=fid)
    events = db.list_flow_events(fid, limit=1000)
    detail = wrm.build_case_detail(flow, links, len(events))
    ledger = detail["ledger"]

    # Both sessions present and distinguishable by role.
    sess_roles = {s["entity_id"]: s["role"] for s in ledger["sessions"]}
    assert sess_roles == {"mgr-sess": "manager", "wrk-sess": "worker"}

    # Both tasks present and distinguishable by provenance: the worker's task is
    # created_by='manager', the Manager's own-turn attach is created_by='system'.
    task_prov = {t["entity_id"]: t["created_by"] for t in ledger["tasks"]}
    assert task_prov == {"mgr-turn": "system", "wrk-turn": "manager"}
