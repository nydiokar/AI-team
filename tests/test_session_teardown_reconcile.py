"""Claude session teardown: whole-tree kill on close + periodic pooled-session reconcile.

Live incident 2026-10-10 (Horse): 10 pooled claude.exe under one live worker while the
gateway had 1 open session — their close_session tasks were reaped before claim, and a
plain disconnect leaves MCP grandchildren (cmd → npx → node) running.
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import psutil
import pytest
from pydantic import ValidationError

from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from src.core.process_utils import (
    WORKER_INCARNATION_ENV,
    WORKER_NODE_ENV,
    process_tree_snapshot,
    reap_unowned_worker_children,
    terminate_snapshot,
)

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# Gateway: terminal_session_ids + bounded payload
# --------------------------------------------------------------------------- #
def test_terminal_session_ids_returns_only_closed_known_rows(tmp_path: Any) -> None:
    db = MeshDB(str(tmp_path / "mesh.db"))
    for sid, status in (("s-open", SessionStatus.AWAITING_INPUT), ("s-closed", SessionStatus.CLOSED)):
        db.upsert_session(Session(
            session_id=sid, backend="claude", repo_path="/tmp/r",
            status=status, created_at=NOW, updated_at=NOW, machine_id="Horse",
        ))
    got: List[str] = db.terminal_session_ids(["s-open", "s-closed", "s-unknown", "s-closed", ""])
    assert got == ["s-closed"]  # unknown ids are never proof of closure
    assert db.terminal_session_ids([]) == []


def test_reconcile_payload_is_bounded() -> None:
    from src.control.task_server import _MAX_RECONCILE_SESSION_IDS, SessionReconcilePayload

    SessionReconcilePayload(node_id="n", session_ids=["s"] * _MAX_RECONCILE_SESSION_IDS)
    with pytest.raises(ValidationError):
        SessionReconcilePayload(node_id="n", session_ids=["s"] * (_MAX_RECONCILE_SESSION_IDS + 1))


# --------------------------------------------------------------------------- #
# Close: snapshot taken while the root lives still reaches the orphaned grandchild
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name == "nt", reason="POSIX process fixture")
def test_snapshot_kills_grandchild_after_root_dies() -> None:
    script = (
        "import subprocess,sys,time;"
        "c=subprocess.Popen(['sleep','60']);print(c.pid,flush=True);time.sleep(60)"
    )
    root = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    child_pid = int(root.stdout.readline())
    tree = process_tree_snapshot(root.pid)
    assert {p.pid for p in tree} >= {root.pid, child_pid}

    root.kill()  # what disconnect() does: only the direct pid dies
    root.wait()
    assert psutil.pid_exists(child_pid)  # the leak: grandchild outlives it

    assert terminate_snapshot(tree) == 1  # root already gone; only the survivor
    assert _gone(child_pid)
    assert process_tree_snapshot(-1) == [] and terminate_snapshot([]) == 0


# --------------------------------------------------------------------------- #
# Worker sweep: kill only this incarnation's unowned, old-enough direct children
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name == "nt", reason="POSIX process fixture")
def test_reap_unowned_spares_owned_foreign_and_young() -> None:
    def spawn(**stamps: str) -> subprocess.Popen:
        env: Dict[str, str] = {**os.environ, **stamps}
        return subprocess.Popen(["sleep", "60"], env=env)

    ours = {WORKER_NODE_ENV: "node-a", WORKER_INCARNATION_ENV: "inc-1"}
    unowned = spawn(**ours)
    owned = spawn(**ours)
    other_incarnation = spawn(**{**ours, WORKER_INCARNATION_ENV: "inc-0"})
    unstamped = spawn()
    procs = [unowned, owned, other_incarnation, unstamped]
    try:
        time.sleep(0.2)  # let exec() replace the forked image (env readable)
        # Young processes are spared (a session mid-spawn has no pid recorded yet).
        assert reap_unowned_worker_children(
            "inc-1", "node-a", [owned.pid], names=("sleep",), min_age_sec=3600,
        ) == []
        reaped = reap_unowned_worker_children(
            "inc-1", "node-a", [owned.pid], names=("sleep",), min_age_sec=0,
        )
        assert reaped == [unowned.pid]
        unowned.wait(timeout=5)
        assert all(p.poll() is None for p in (owned, other_incarnation, unstamped))
        assert reap_unowned_worker_children("", "node-a", [], names=("sleep",)) == []
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()


# --------------------------------------------------------------------------- #
# Worker reconcile: closes what the gateway reports closed, via backend.close
# --------------------------------------------------------------------------- #
class _FakeBackend:
    def __init__(self, ids: List[str]) -> None:
        self.ids = ids
        self.closed: List[str] = []

    def live_session_ids(self) -> List[str]:
        return list(self.ids)

    def owned_pids(self) -> List[int]:
        return []

    def close(self, session: Any) -> None:
        self.closed.append(session.session_id)


class _FakeHTTP:
    def __init__(self, closed: List[str]) -> None:
        self.closed = closed
        self.posts: List[Any] = []

    def post(self, path: str, body: Any = None, timeout: int = 10) -> Any:
        self.posts.append((path, body))
        return {"closed": self.closed}


def test_reconcile_once_closes_gateway_closed_sessions() -> None:
    from src.worker.agent import WorkerAgent

    w = WorkerAgent.__new__(WorkerAgent)
    backend = _FakeBackend(["s-open", "s-closed", "oneoff-abc"])
    w._backends = {"claude": backend}
    w._http = _FakeHTTP(["s-closed"])
    w.cfg = SimpleNamespace(node_id="Horse")
    w._incarnation_id = ""  # orphan sweep is a no-op without an identity

    assert w._reconcile_pooled_sessions_once() == {"closed": 1, "reaped": 0}
    assert backend.closed == ["s-closed"]
    path, body = w._http.posts[0]
    assert path == "/nodes/sessions/reconcile"
    assert body == {"node_id": "Horse", "session_ids": ["s-open", "s-closed"]}  # one-offs never sent


def test_reconcile_skips_http_when_pool_empty() -> None:
    from src.worker.agent import WorkerAgent

    w = WorkerAgent.__new__(WorkerAgent)
    w._backends = {"claude": _FakeBackend([]), "codex": object()}
    w._http = _FakeHTTP([])
    w.cfg = SimpleNamespace(node_id="Horse")
    w._incarnation_id = ""
    assert w._reconcile_pooled_sessions_once() == {"closed": 0, "reaped": 0}
    assert w._http.posts == []


# --------------------------------------------------------------------------- #
# Driver: claude pid read from the SDK client; unknown layouts yield 0
# --------------------------------------------------------------------------- #
def test_client_pid() -> None:
    from src.backends.claude_driver import _client_pid

    client = SimpleNamespace(_transport=SimpleNamespace(_process=SimpleNamespace(pid=4242)))
    assert _client_pid(client) == 4242
    assert _client_pid(SimpleNamespace()) == 0
    assert _client_pid(SimpleNamespace(_transport=SimpleNamespace(_process=None))) == 0
