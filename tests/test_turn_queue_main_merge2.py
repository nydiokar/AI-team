"""A82 x main merge 2 (A88 #180 + #179) — semantic integration seams.

* #179's legacy pending reaper must never retire a managed (protocol-1) row:
  a waiting turn legitimately queues behind a busy / offline-pinned session
  for a long time, and managed rows on a closed session belong to
  ``close_session_turns``.

Real file-backed ``MeshDB`` and the REAL task-server reaper entry point. No
CLI is spawned (test cost guard).
"""
from datetime import datetime, timedelta, timezone

from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
GRACE = 1800
MAX_AGE = 604800
TS = "2026-10-02T00:00:00+00:00"


def _session(db: MeshDB, sid: str, node: str) -> None:
    db.upsert_session(Session(
        session_id=sid, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.IDLE, created_at=TS, updated_at=TS, machine_id=node,
    ))
    db.enroll_session(sid)


def _backdate_all(db: MeshDB, seconds: int) -> None:
    old = (NOW - timedelta(seconds=seconds)).isoformat()
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET created_at = ?", (old,))


def _managed_world(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_node("Horse", "100.0.0.9", 9001, ["claude"], 2, status="online")
    _session(db, "s-busy", "Horse")
    for tid, body in (("m-head", "first"), ("m-wait", "second")):
        db.enqueue_turn(
            task_id=tid, session_id="s-busy", backend="claude", action="resume_session",
            payload={"task_id": tid, "prompt": body}, turn_source="human",
            turn_kind="instruction", machine_id="Horse",
        )
    assert db.activate_turn("m-head")
    # Legacy orphans pinned to the same node and to an unknown node.
    db.enqueue_task("legacy-off", None, "Horse", "claude", "resume_session", {"prompt": "x"})
    db.enqueue_task("legacy-unknown", None, "ghost", "claude", "resume_session", {"prompt": "x"})
    db.upsert_node("Horse", "100.0.0.9", 9001, ["claude"], 2, status="offline")
    _backdate_all(db, MAX_AGE + 60)  # past BOTH the grace and the age ceiling
    return db


def _status(db: MeshDB, tid: str) -> str:
    return db._conn().execute("SELECT status FROM mesh_tasks WHERE id = ?", (tid,)).fetchone()[0]


def test_pending_reaper_never_classifies_managed_rows(tmp_path):
    db = _managed_world(tmp_path)
    assert (_status(db, "m-head"), _status(db, "m-wait")) == ("pending", "queued")
    stale = {r["id"] for r in db.list_stale_pending_tasks(grace_sec=GRACE, max_age_sec=MAX_AGE, now=NOW)}
    assert stale == {"legacy-off", "legacy-unknown"}
    assert db.stats()["tasks_stale_pending"] == 2


def test_cancel_task_refuses_a_managed_row(tmp_path):
    db = _managed_world(tmp_path)
    assert db.cancel_task("m-head", "pending reaped: node_offline") is False
    assert db.cancel_task("m-wait", "pending reaped: node_offline") is False
    assert (_status(db, "m-head"), _status(db, "m-wait")) == ("pending", "queued")


def test_reaper_passes_leave_managed_turns_and_reap_legacy(tmp_path, monkeypatch):
    import src.control.db as db_mod
    from config import config as cfg
    from src.control import task_server

    db = _managed_world(tmp_path)
    monkeypatch.setattr(db_mod, "_db_instance", db, raising=False)
    monkeypatch.setattr(task_server, "get_db", lambda: db)
    monkeypatch.setattr(cfg.mesh, "pending_reaper_enabled", True, raising=False)
    monkeypatch.setattr(cfg.mesh, "pending_reaper_grace_sec", GRACE, raising=False)
    monkeypatch.setattr(cfg.mesh, "pending_max_age_sec", MAX_AGE, raising=False)
    for _ in range(3):
        task_server._reap_stale_pending_once()
    assert (_status(db, "m-head"), _status(db, "m-wait")) == ("pending", "queued")
    assert (_status(db, "legacy-off"), _status(db, "legacy-unknown")) == ("cancelled", "cancelled")


def test_reaper_leaves_managed_rows_of_a_closed_session_to_close_session_turns(tmp_path):
    db = _managed_world(tmp_path)
    with db._write() as conn:
        conn.execute("UPDATE sessions SET status = 'closed' WHERE session_id = 's-busy'")
    stale = {r["id"] for r in db.list_stale_pending_tasks(grace_sec=GRACE, max_age_sec=MAX_AGE, now=NOW)}
    assert "m-head" not in stale and "m-wait" not in stale


# --------------------------------------------------------------------------- #
# #177 unknown-node pin rejection vs managed admission / host affinity
# --------------------------------------------------------------------------- #
import pytest  # noqa: E402

from tests.test_turn_queue_producer1 import _no_cli_spawn  # noqa: E402,F401 — autouse guard
from tests.test_turn_queue_sender import ADMIN, SOLO, _mk_world  # noqa: E402


@pytest.mark.parametrize("pinned", [True, False])
def test_enrolled_admission_never_reaches_legacy_unknown_node_rejection(tmp_path, monkeypatch, pinned):
    """Enrolled sessions (pinned, or unpinned routed via MESH_LOCAL_CARRIER_NODE_ID)
    are admitted on the managed path — #177's legacy ``_mesh_enqueue_task``
    unknown-node rejection is never consulted. Once the pinned carrier goes
    offline the admitted turn WAITS (host affinity: requeued with a visible
    reason, never relocated), and repeated pending-reaper passes far past the
    grace/age ceiling never cancel it."""
    import src.control.db as db_mod
    from config import config as cfg
    from src.control import task_server
    from src.orchestrator import TaskOrchestrator

    def _legacy(*_a, **_k):
        raise AssertionError("enrolled admission reached the legacy mesh enqueue")

    monkeypatch.setattr(TaskOrchestrator, "_mesh_enqueue_task", _legacy, raising=True)
    w = _mk_world(tmp_path, monkeypatch, pinned=pinned)
    r = w.api.post(f"/api/sessions/{SOLO}/turn-requests",
                   json={"body": "operator turn", "operation_id": "op-aff"},
                   headers={"Authorization": f"Bearer {ADMIN}", "Idempotency-Key": "op-aff"})
    assert r.status_code == 202, r.text
    tid = r.json()["turn_id"]
    row = w.db.get_task(tid)
    assert row["machine_id"] == w.node and row["status"] in ("queued", "pending")
    w.db.activate_turn(tid)
    w.db.upsert_node(w.node, "127.0.0.1", 0, ["claude"], 2, status="offline")
    w.db.requeue_turns_on_dead_carriers()
    _backdate_all(w.db, MAX_AGE + 60)
    monkeypatch.setattr(db_mod, "_db_instance", w.db, raising=False)
    monkeypatch.setattr(cfg.mesh, "pending_reaper_enabled", True, raising=False)
    monkeypatch.setattr(cfg.mesh, "pending_reaper_grace_sec", GRACE, raising=False)
    monkeypatch.setattr(cfg.mesh, "pending_max_age_sec", MAX_AGE, raising=False)
    for _ in range(3):
        task_server._reap_stale_pending_once()
    row = w.db.get_task(tid)
    assert row["status"] in ("queued", "pending"), row["status"]
    assert row["machine_id"] == w.node
