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


def test_unpinned_oneoff_is_self_claimed(_patch):
    """A session-less one-off runs on THIS host: its shadow row is self-claimed
    so no daemon sharing the node id can run it a second time."""
    db = _patch
    orch = _orch(db, None)  # no session ⇒ one-off

    orch._mesh_enqueue_task(
        types.SimpleNamespace(id="t-1", prompt="do the thing", metadata={}), "claude",
    )

    row = db.get_task("t-1")
    assert row["action"] == "run_oneoff"
    assert row["status"] == "claimed" and row["claimed_by"] == HOST
    assert db.get_pending_tasks(node_id=HOST) == []


@pytest.mark.parametrize("pin", [HOST, "", "Horse", "kanebra"],
                         ids=["this_host", "unpinned", "remote", "unknown_node"])
def test_session_task_never_leaves_a_claimable_protocol0_row(_patch, pin):
    """[A82 Stage 8a] Converted from the per-pin legacy shadow-row tests
    (host-pinned self-claim, unpinned self-claim, remote pin left pending,
    unknown pin failed fast): a SESSION task's protocol-0 execution row is now
    refused at insert, so wherever the session is pinned no daemon — same node
    id, remote or phantom — can ever claim (double-run) it, and no orphan is
    left pending. Session turns run on the managed queue."""
    db = _patch
    db.upsert_node("Horse", "100.0.0.2", 9001, ["claude"], 2)
    orch = _orch(db, _session(machine_id=pin))

    orch._mesh_enqueue_task(_task(), "claude")

    assert db.get_task("t-1") is None
    for node in (HOST, "Horse", "kanebra"):
        assert db.get_pending_tasks(node_id=node) == []
