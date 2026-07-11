"""
A36 — M2.5 Case admission & Task/Session affiliation tests.

Under ``HARNESS_FLOW_DRIVE`` ON the retired per-turn mint is replaced by an
admission policy: a turn either BIRTHS a Case (dispatched/managed task),
ATTACHES to the session's open Case, or runs Case-less (standalone). Only a
birth creates a flow_run — so a reused session no longer shatters into one fake
Case per turn.

  * flag OFF ⇒ byte-identical to A19 (one dispatch_start RECORD per task).
  * find_open_case_for_session / open_case are the new lookup-or-reuse + the ONLY
    sanctioned Case-birth path.
  * durable session affiliation (current_case_id / case_role) survives turns.

Helpers run the admission path as real bound methods on a bare orchestrator
(``__new__``), with a real temp MeshDB wired in as get_db().
"""

import types

import pytest

from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _orch() -> TaskOrchestrator:
    return TaskOrchestrator.__new__(TaskOrchestrator)


def _task(task_id, metadata=None):
    return types.SimpleNamespace(id=task_id, metadata=metadata)


def _turn(task_id, session_id):
    """An ordinary turn: a session, no lineage, no managed marker."""
    return _task(task_id, {"session_id": session_id})


class _StubStore:
    """Minimal in-memory session store for affiliation assertions."""

    def __init__(self):
        self._d = {}

    def get(self, sid):
        return self._d.get(sid)

    def save(self, session):
        self._d[session.session_id] = session


def _session(session_id):
    return types.SimpleNamespace(
        session_id=session_id, current_case_id=None, case_role=None,
    )


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("HARNESS_FLOW_DRIVE", raising=False)


def _patch_db(monkeypatch, db):
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)


# ---------------------------------------------------------------------------
# db.open_case — the ONLY sanctioned Case-birth path
# ---------------------------------------------------------------------------

def test_open_case_creates_exactly_one(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("ship the thing", "sess-1", role="manager")

    runs = db.list_flow_runs()
    assert len(runs) == 1
    assert runs[0]["flow_run_id"] == fid
    assert runs[0]["current_stage"] == "objective_lock"
    assert runs[0]["objective_lock"] == "ship the thing"
    assert runs[0]["status"] is None  # open

    sess_links = db.list_flow_links(flow_run_id=fid, entity_type="session")
    assert len(sess_links) == 1
    assert (sess_links[0]["entity_id"], sess_links[0]["role"]) == ("sess-1", "manager")

    created = [e for e in db.list_flow_events(fid) if e["event_type"] == "flow.created"]
    assert len(created) == 1


def test_open_case_persists_completion_criteria(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case(
        "ship it", "sess-1", completion_criteria="tests green && deployed",
    )
    assert db.get_flow_run(fid)["completion_criteria"] == "tests green && deployed"


def test_open_case_absent_criteria_is_null(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("ship it", "sess-1")
    assert db.get_flow_run(fid)["completion_criteria"] is None


# ---------------------------------------------------------------------------
# db.find_open_case_for_session
# ---------------------------------------------------------------------------

def test_find_open_case_returns_open(tmp_path):
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    assert db.find_open_case_for_session("sess-1") == fid


def test_find_open_case_skips_closed_but_keeps_blocked(tmp_path):
    db = _db(tmp_path)
    closed = db.open_case("obj-a", "sess-1")
    db.update_flow_run(closed, status="closed")
    # A closed Case is not returned.
    assert db.find_open_case_for_session("sess-1") is None

    blocked = db.open_case("obj-b", "sess-1")
    db.update_flow_run(blocked, status="blocked")
    # 'blocked' is needs-attention, still OPEN: a follow-up turn attaches to it.
    assert db.find_open_case_for_session("sess-1") == blocked


def test_find_open_case_returns_newest(tmp_path):
    db = _db(tmp_path)
    db.open_case("old", "sess-1")
    newest = db.open_case("new", "sess-1")
    assert db.find_open_case_for_session("sess-1") == newest


def test_find_open_case_blank_session_is_none(tmp_path):
    db = _db(tmp_path)
    assert db.find_open_case_for_session("") is None
    assert db.find_open_case_for_session("no-such-session") is None


# ---------------------------------------------------------------------------
# Admission — standalone / attach (flag ON)
# ---------------------------------------------------------------------------

def test_standalone_turn_creates_no_case(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    for i in range(10):
        assert orch._record_flow_run_start(_turn(f"t-{i}", "sess-standalone")) is None

    assert db.list_flow_runs() == []  # 0 Cases from 10 standalone turns


def test_attach_on_open_case(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    fid = db.open_case("obj", "sess-1", role="manager")
    task = _turn("t-1", "sess-1")
    assert orch._record_flow_run_start(task) is None  # no new flow_run

    # Attached: a task link on the SAME Case + the shared id stashed under the
    # DISTINCT case key (NOT _FLOW_RUN_META_KEY).
    task_links = db.list_flow_links(flow_run_id=fid, entity_type="task", role="task")
    assert [l["entity_id"] for l in task_links] == ["t-1"]
    assert task.metadata[TaskOrchestrator._CASE_ID_META_KEY] == fid
    assert TaskOrchestrator._FLOW_RUN_META_KEY not in task.metadata

    attached = [e for e in db.list_flow_events(fid) if e["event_type"] == "task.attached"]
    assert len(attached) == 1 and attached[0]["entity_id"] == "t-1"


def test_ten_turns_one_flowrun_ten_task_links_one_session_link(tmp_path, monkeypatch):
    """Headline acceptance: a Case-attached session running 10 turns yields
    exactly 1 flow_run, 10 task links, and 1 session link (not 10)."""
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    fid = db.open_case("obj", "sess-1", role="manager")
    for i in range(10):
        orch._record_flow_run_start(_turn(f"t-{i}", "sess-1"))

    assert len(db.list_flow_runs()) == 1
    task_links = db.list_flow_links(flow_run_id=fid, entity_type="task", role="task")
    assert len(task_links) == 10
    session_links = db.list_flow_links(flow_run_id=fid, entity_type="session")
    assert len(session_links) == 1


def test_attach_does_not_reclose_across_turns(tmp_path, monkeypatch):
    """A turn attaching to an open Case does NOT stash _FLOW_RUN_META_KEY, so the
    per-turn terminal helper cannot auto-close the shared Case — it stays open for
    the next turn to attach (the core A36 continuity guarantee)."""
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()

    fid = db.open_case("obj", "sess-1")
    t1 = _turn("t-1", "sess-1")
    orch._record_flow_run_start(t1)
    # The terminal helper keys off _FLOW_RUN_META_KEY (absent on an attached turn)
    # ⇒ it is a no-op and the Case status stays open.
    orch._flow_terminal_outcome(t1, success=True)
    assert db.get_flow_run(fid)["status"] is None

    # Turn 2 still finds the open Case and attaches.
    orch._record_flow_run_start(_turn("t-2", "sess-1"))
    assert db.find_open_case_for_session("sess-1") == fid
    assert len(db.list_flow_links(flow_run_id=fid, entity_type="task", role="task")) == 2


# ---------------------------------------------------------------------------
# Durable session affiliation
# ---------------------------------------------------------------------------

def test_attach_sets_durable_affiliation_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()
    store = _StubStore()
    store.save(_session("sess-1"))
    orch.session_store = store

    fid = db.open_case("obj", "sess-1", role="manager")
    orch._record_flow_run_start(_turn("t-1", "sess-1"))

    session = store.get("sess-1")
    assert session.current_case_id == fid
    assert session.case_role == "manager"  # resolved from the authoritative link

    # A second turn on the same Case is a no-op write (value already current).
    calls = {"n": 0}
    orig_save = store.save
    store.save = lambda s: (calls.__setitem__("n", calls["n"] + 1), orig_save(s))
    orch._record_flow_run_start(_turn("t-2", "sess-1"))
    assert calls["n"] == 0  # steady-state: no redundant session write


# ---------------------------------------------------------------------------
# OFF path — byte-identical to A19
# ---------------------------------------------------------------------------

def test_off_path_creates_dispatch_start_record(tmp_path, monkeypatch):
    db = _db(tmp_path)  # flag unset
    _patch_db(monkeypatch, db)
    orch = _orch()

    task = _turn("t-off", "sess-1")
    fid = orch._record_flow_run_start(task)

    rows = db.list_flow_runs(task_id="t-off")
    assert len(rows) == 1 and rows[0]["flow_run_id"] == fid
    assert rows[0]["current_stage"] == "dispatch_start"
    # OFF ⇒ no admission side effects: no links/events, nothing stashed.
    assert db.list_flow_links(flow_run_id=fid) == []
    assert TaskOrchestrator._CASE_ID_META_KEY not in (task.metadata or {})
    assert TaskOrchestrator._FLOW_RUN_META_KEY not in (task.metadata or {})


def test_off_path_ignores_open_case(tmp_path, monkeypatch):
    """With the flag OFF there is no admission at all — even an existing open Case
    is ignored and A19's per-task dispatch_start record is written."""
    db = _db(tmp_path)  # flag unset
    _patch_db(monkeypatch, db)
    orch = _orch()

    db.open_case("obj", "sess-1")
    before = len(db.list_flow_runs())
    orch._record_flow_run_start(_turn("t-off", "sess-1"))
    assert len(db.list_flow_runs()) == before + 1  # a new dispatch_start row


# ---------------------------------------------------------------------------
# orchestrator.open_case seam
# ---------------------------------------------------------------------------

def test_orchestrator_open_case_sets_affiliation(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _patch_db(monkeypatch, db)
    orch = _orch()
    store = _StubStore()
    store.save(_session("sess-1"))
    orch.session_store = store

    fid = orch.open_case("obj", "sess-1", role="manager", completion_criteria="done?")
    assert fid is not None
    assert db.get_flow_run(fid)["completion_criteria"] == "done?"
    session = store.get("sess-1")
    assert session.current_case_id == fid
    assert session.case_role == "manager"
