"""
A46 / M3.3 — durable worker-wait relay tests (db layer), A104 inbox shims.

``wait_for_worker`` is a pure in-process poll: a Manager/gateway crash mid-wait
loses it. A46 recorded the wait intent as a ``worker.wait_pending`` ledger marker;
A104 replaced that ledger with the agent inbox — the child's requester is stamped
at dispatch and its completion row is written in the child's terminal txn — so the
obligation is durable without any marker.

  * ``record_worker_wait`` is a no-op shim (returns None, writes no event) whatever
    the flag.
  * ``reconcile_worker_waits`` is a READ-ONLY view over ``pending_for``: ``resolved``
    = finished children whose completion is still unconsumed, ``pending`` = requested
    children still running. Re-runs are identical (nothing is written); a tagged
    ``review.*`` event about the task consumes it.
"""

from src.control.db import MeshDB
from tests.inbox_seed import seed_child, seed_finished_child


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _on(monkeypatch) -> None:
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")


def _off(monkeypatch) -> None:
    monkeypatch.delenv("DURABLE_RELAY_ENABLED", raising=False)


def _events(db: MeshDB, case_id: str, event_type: str) -> list:
    return [e for e in db.list_flow_events(case_id) if e["event_type"] == event_type]


# --- flag gating: OFF is byte-identical -------------------------------------

def test_record_worker_wait_noop_when_flag_off(tmp_path, monkeypatch):
    _off(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    assert db.record_worker_wait(fid, "task_1") is None
    assert _events(db, fid, "worker.wait_pending") == []


def test_reconcile_disabled_when_flag_off(tmp_path, monkeypatch):
    """A104: reconcile is a read-only view over the inbox, no longer flag-gated —
    flag OFF still reports the Case truth and writes nothing. (Only
    ``boot_reconcile_case`` stays gated on DURABLE_RELAY_ENABLED.)"""
    _off(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    seed_finished_child(db, fid, "task_done", requester="sess-1")
    before = len(db.list_flow_events(fid))
    out = db.reconcile_worker_waits(fid)
    assert out["ok"] is True
    assert out["resolved"] == [{"task_id": "task_done", "outcome": "success"}]
    assert len(db.list_flow_events(fid)) == before
    assert db.reconcile_worker_waits("no-such-case")["ok"] is False


# --- record_worker_wait -----------------------------------------------------

def test_record_worker_wait_writes_pending_marker(tmp_path, monkeypatch):
    """A104 shim: even flag ON, no ``worker.wait_pending`` marker is written —
    the inbox records the obligation from the requester stamp. Repeat calls
    stay no-ops (the old per-task idempotency has nothing left to dedupe)."""
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    assert db.record_worker_wait(fid, "task_1", timeout=120.0) is None
    assert db.record_worker_wait(fid, "task_1") is None
    assert _events(db, fid, "worker.wait_pending") == []


# --- reconcile --------------------------------------------------------------

def test_reconcile_resolves_finished_and_keeps_open(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    seed_finished_child(db, fid, "task_done", requester="sess-1")
    seed_child(db, fid, "task_open", requester="sess-1")

    out = db.reconcile_worker_waits(fid)
    assert out["ok"] is True
    assert [r["task_id"] for r in out["resolved"]] == ["task_done"]
    assert out["resolved"][0]["outcome"] == "success"
    assert [p["task_id"] for p in out["pending"]] == ["task_open"]
    # read-only: no legacy wait markers are written.
    assert _events(db, fid, "worker.wait_resolved") == []
    assert _events(db, fid, "worker.wait_pending") == []


def test_reconcile_idempotent_across_reruns(tmp_path, monkeypatch):
    """Reconcile never consumes: re-runs return the same unconsumed completion
    (crash-during-reconcile safe by construction). A tagged review consumes it."""
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    seed_finished_child(db, fid, "task_done", requester="sess-1")

    first = db.reconcile_worker_waits(fid)
    second = db.reconcile_worker_waits(fid)
    assert [r["task_id"] for r in first["resolved"]] == ["task_done"]
    assert second == first
    db.append_flow_event(fid, "review.accepted", "manager", entity_type="task", entity_id="task_done")
    third = db.reconcile_worker_waits(fid)
    assert third["resolved"] == [] and third["pending"] == []


def test_reconcile_carries_failed_outcome(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    seed_finished_child(db, fid, "task_fail", requester="sess-1", status="failed")
    out = db.reconcile_worker_waits(fid)
    assert out["resolved"][0] == {"task_id": "task_fail", "outcome": "failed"}


def test_record_after_resolve_starts_a_fresh_wait(tmp_path, monkeypatch):
    """Once a completion is consumed (tagged review), a later dispatch of a new
    task is a fresh, independent outstanding request."""
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    seed_finished_child(db, fid, "task_a", requester="sess-1")
    db.append_flow_event(fid, "review.accepted", "manager", entity_type="task", entity_id="task_a")
    seed_child(db, fid, "task_b", requester="sess-1", token="tok-b")
    out = db.reconcile_worker_waits(fid)
    assert [p["task_id"] for p in out["pending"]] == ["task_b"]
    assert out["resolved"] == []
