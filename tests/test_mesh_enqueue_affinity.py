"""
Regression: a session pinned to THIS host must not be double-executed.

Root cause (fixed): the gateway host and a standalone worker daemon can share a
node_id (e.g. both 'gateway-host' — the Pi's hostname). When a session was pinned to
that node, `process_task` ran it locally (machine_id == host ⇒ NOT remote) while
`_mesh_enqueue_task` left the row 'pending' (it only self-claimed when machine_id
was UNSET). The daemon then claimed the same 'pending' row and ran the task a
SECOND time → two agents for one task.

Fix: `_mesh_enqueue_task` self-claims whenever the task runs on THIS host — no
pin OR the pin names this host — mirroring process_task's local/remote split. A
pin to a DIFFERENT host still stays 'pending' for that remote worker.
"""
import types

import pytest

from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator
import src.orchestrator as orch_mod


HOST = "gateway-host"


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _orch(db, session) -> TaskOrchestrator:
    o = TaskOrchestrator.__new__(TaskOrchestrator)
    o.session_store = types.SimpleNamespace(get=lambda _sid: session)
    return o


def _session(machine_id):
    # Only the fields _mesh_enqueue_task reads.
    return types.SimpleNamespace(
        session_id="sess-1", machine_id=machine_id, backend="claude",
        repo_path="/tmp/x", backend_session_id="bsid-1", model="m",
        telegram_chat_id=None, telegram_thread_id=None, owner_user_id=None,
        last_user_message="", driver_type="", driver_status="",
        cache_health="", cache_unhealthy_count=0, previous_backend_session_ids=[],
    )


def _task(task_id="t-1"):
    return types.SimpleNamespace(
        id=task_id, prompt="do the thing", metadata={"session_id": "sess-1"},
    )


def _seed_session(db):
    """Minimal sessions row so the mesh_tasks.session_id FK is satisfied."""
    import sqlite3
    con = sqlite3.connect(str(db._path))
    con.execute(
        "INSERT INTO sessions (session_id, backend, repo_path, status, "
        "created_at, updated_at) VALUES ('sess-1','claude','/tmp/x','idle','t0','t0')"
    )
    con.commit()
    con.close()


@pytest.fixture
def _patch(monkeypatch, tmp_path):
    db = _db(tmp_path)
    _seed_session(db)
    monkeypatch.setattr(orch_mod, "get_db", lambda: db, raising=False)
    import src.control.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    monkeypatch.setattr(orch_mod.socket, "gethostname", lambda: HOST)
    return db


def test_pinned_to_this_host_is_self_claimed_not_pending(_patch):
    db = _patch
    orch = _orch(db, _session(machine_id=HOST))  # picker chose the local node

    orch._mesh_enqueue_task(_task(), "claude")

    row = db.get_task("t-1")
    assert row["status"] == "claimed", "host-pinned task must be self-claimed"
    assert row["claimed_by"] == HOST
    # The decisive check: a daemon with the SAME node_id must NOT see it as work.
    assert db.get_pending_tasks(node_id=HOST) == []


def test_unpinned_is_self_claimed(_patch):
    db = _patch
    orch = _orch(db, _session(machine_id=""))  # built-in gateway worker

    orch._mesh_enqueue_task(_task(), "claude")

    row = db.get_task("t-1")
    assert row["status"] == "claimed" and row["claimed_by"] == HOST
    assert db.get_pending_tasks(node_id=HOST) == []


def test_pinned_to_other_host_stays_pending_for_that_worker(_patch):
    db = _patch
    # A genuinely remote node must be REGISTERED for its pinned work to stay
    # claimable (#177 rejects a pin to an unknown node).
    db.upsert_node("Horse", "100.0.0.2", 9001, ["claude"], 2)
    orch = _orch(db, _session(machine_id="Horse"))  # a genuinely remote node

    orch._mesh_enqueue_task(_task(), "claude")

    row = db.get_task("t-1")
    assert row["status"] == "pending", "remote-pinned task must stay claimable"
    assert row["claimed_by"] is None
    # This host must NOT self-claim another node's work…
    assert db.get_pending_tasks(node_id=HOST, accept_unpinned=False) == []
    # …but the pinned remote worker sees it.
    seen = db.get_pending_tasks(node_id="Horse")
    assert [r["id"] for r in seen] == ["t-1"]


def test_pinned_to_unknown_node_is_rejected_not_left_pending(_patch):
    """[#177] A pin to a node that was never registered (e.g. 'kanebra' when the
    real node is 'kanebra-worker') is rejected at enqueue — marked failed so it
    never becomes a pending orphan and the dispatch poller fails fast."""
    db = _patch
    orch = _orch(db, _session(machine_id="kanebra"))  # no such node

    orch._mesh_enqueue_task(_task(), "claude")

    row = db.get_task("t-1")
    assert row["status"] == "failed", "unknown-node pin must fail fast, never linger pending"
    assert "not a registered mesh node" in (row["error"] or "")
    # No worker — real or phantom — ever sees it as claimable work.
    assert db.get_pending_tasks(node_id="kanebra") == []
