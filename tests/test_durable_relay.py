"""
A46 / M3.3 — durable worker-wait relay tests (db layer).

``wait_for_worker`` is a pure in-process poll: a Manager/gateway crash mid-wait
loses it. A46 records the wait intent as an append-only ``worker.wait_pending``
marker at dispatch and reconciles outstanding waits against the already-durable
``task.finished`` event, so a resumed Manager recovers its waits from the ledger,
not from lost memory.

  * flag OFF ⇒ byte-identical (no ``worker.wait_*`` events written; reconcile no-ops).
  * ``record_worker_wait`` is idempotent (no duplicate pending marker per task).
  * ``reconcile_worker_waits`` resolves a finished worker (appends
    ``worker.wait_resolved``), leaves an open worker PENDING, and is idempotent
    across re-runs (crash-during-reconcile safe).
"""

from src.control.db import MeshDB


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _on(monkeypatch) -> None:
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")


def _off(monkeypatch) -> None:
    monkeypatch.delenv("DURABLE_RELAY_ENABLED", raising=False)


def _finished(db: MeshDB, case_id: str, task_id: str, outcome: str = "success") -> None:
    db.append_flow_event(
        case_id, "task.finished", "worker",
        entity_type="task", entity_id=task_id,
        payload={"outcome": outcome},
    )


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
    _off(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    assert db.reconcile_worker_waits(fid) == {"ok": False, "reason": "durable_relay_disabled"}


# --- record_worker_wait -----------------------------------------------------

def test_record_worker_wait_writes_pending_marker(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    eid = db.record_worker_wait(fid, "task_1", timeout=120.0)
    assert isinstance(eid, int)
    pend = _events(db, fid, "worker.wait_pending")
    assert len(pend) == 1 and pend[0]["entity_id"] == "task_1"


def test_record_worker_wait_idempotent(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    first = db.record_worker_wait(fid, "task_1")
    again = db.record_worker_wait(fid, "task_1")
    assert first == again  # same event id — no duplicate pending marker
    assert len(_events(db, fid, "worker.wait_pending")) == 1


# --- reconcile --------------------------------------------------------------

def test_reconcile_resolves_finished_and_keeps_open(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    db.record_worker_wait(fid, "task_done")
    db.record_worker_wait(fid, "task_open", timeout=90.0)
    _finished(db, fid, "task_done", outcome="success")

    out = db.reconcile_worker_waits(fid)
    assert out["ok"] is True
    assert [r["task_id"] for r in out["resolved"]] == ["task_done"]
    assert out["resolved"][0]["outcome"] == "success"
    assert [p["task_id"] for p in out["pending"]] == ["task_open"]
    assert out["pending"][0]["timeout"] == 90.0
    # a worker.wait_resolved marker was appended for the finished task ONLY.
    assert [e["entity_id"] for e in _events(db, fid, "worker.wait_resolved")] == ["task_done"]


def test_reconcile_idempotent_across_reruns(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    db.record_worker_wait(fid, "task_done")
    _finished(db, fid, "task_done")

    first = db.reconcile_worker_waits(fid)
    second = db.reconcile_worker_waits(fid)
    assert [r["task_id"] for r in first["resolved"]] == ["task_done"]
    assert second["resolved"] == [] and second["pending"] == []  # already reconciled
    assert len(_events(db, fid, "worker.wait_resolved")) == 1  # no duplicate marker


def test_reconcile_carries_failed_outcome(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    db.record_worker_wait(fid, "task_fail")
    _finished(db, fid, "task_fail", outcome="error")
    out = db.reconcile_worker_waits(fid)
    assert out["resolved"][0] == {"task_id": "task_fail", "outcome": "error"}


def test_record_after_resolve_starts_a_fresh_wait(tmp_path, monkeypatch):
    """A resolve CLEARS the pending state, so a later dispatch of a new task is a
    fresh, independent pending marker (idempotency is per unresolved wait)."""
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = db.open_case("obj", "sess-1")
    db.record_worker_wait(fid, "task_a")
    _finished(db, fid, "task_a")
    db.reconcile_worker_waits(fid)  # resolves task_a
    db.record_worker_wait(fid, "task_b")  # a different task, still open
    out = db.reconcile_worker_waits(fid)
    assert [p["task_id"] for p in out["pending"]] == ["task_b"]
    assert out["resolved"] == []
