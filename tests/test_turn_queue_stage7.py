"""A82 Stage 7 — Stage-6 review carries (release under-count + Telegram NITs).

Carry (1): a managed row moved claimed → pending by ``task_server.release_managed``
or by ``NodeRegistry.register`` (``release_superseded_managed_grants``) re-enters
the fleet waiting count. Before Stage 7 neither path told the shared waiting
allowance, so ``_managed_cache`` sat BELOW the DB count until the next hint /
60 s safety-net pass and legacy puts could push the fleet past the cap. The
task server is embedded in the gateway (``embedded_server`` runs
``task_server.app`` on a thread of the gateway process), so the allowance and
scheduler are the same objects and the correction is direct.

Real pieces: temp file-backed ``MeshDB``, the real task-server app (TestClient),
the real ``NodeRegistry`` and the real legacy ``SessionTaskQueue``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any, List

import pytest
from fastapi.testclient import TestClient

import src.control.task_server as ts
from src.control import turn_admission, turn_scheduler
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus

NODE = "Horse"
TOKEN = "tok"
NOW = datetime(2026, 10, 2, 12, 0, 0).isoformat()
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def db(tmp_path, monkeypatch) -> MeshDB:
    import src.control.db as db_mod
    import src.control.node_registry as nr_mod

    mdb = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(ts, "get_db", lambda: mdb)
    monkeypatch.setattr(db_mod, "get_db", lambda: mdb)
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    monkeypatch.setattr(ts, "_worker_token", lambda: TOKEN)
    return mdb


class _HintCounter:
    def __init__(self) -> None:
        self.hints = 0

    def hint(self) -> None:
        self.hints += 1


@pytest.fixture()
def hints() -> Any:
    counter = _HintCounter()
    turn_scheduler._register(counter)  # type: ignore[arg-type]
    yield counter
    turn_scheduler._unregister(counter)  # type: ignore[arg-type]


def _register(client: TestClient, incarnation_id: str) -> None:
    r = client.post("/nodes/register", json={
        "node_id": NODE, "tailscale_ip": "127.0.0.1", "api_port": 0,
        "incarnation_id": incarnation_id,
        "capabilities": {"backends": ["claude"], "queue_protocols": [0, 1],
                         "managed_backends": ["claude"]},
    }, headers=H)
    assert r.status_code == 200, r.text


def _seed_claimed(db: MeshDB, client: TestClient, n: int, incarnation_id: str) -> List[str]:
    """``n`` managed rows on ``n`` sessions, activated and CLAIMED (not
    started) — claimed rows are outside the waiting (queued+pending) count."""
    tokens: List[str] = []
    for i in range(n):
        sid, tid = f"sess-{i}", f"t-{i}"
        db.upsert_session(Session(
            session_id=sid, backend="claude", repo_path="/tmp/repo",
            status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id=NODE,
        ))
        db.enroll_session(sid)
        db.enqueue_turn(
            task_id=tid, session_id=sid, backend="claude", action="resume_session",
            payload={"task_id": tid, "prompt": "p"}, turn_source="human",
            turn_kind="instruction", machine_id=NODE,
        )
        db.activate_turn(tid)
        r = client.post(f"/tasks/{tid}/claim-managed",
                        json={"node_id": NODE, "incarnation_id": incarnation_id}, headers=H)
        assert r.status_code == 200, r.text
        tokens.append(r.json()["claim_token"])
    return tokens


def _sync_allowance(db: MeshDB) -> turn_scheduler.SchedulerPassResult:
    """A REAL scheduler pass (no queued heads → ``prepare`` never runs): its
    allowance refresh is the production one."""

    async def _prepare(*_a: Any) -> Any:  # pragma: no cover — nothing queued
        raise AssertionError("no head expected")

    res = asyncio.run(turn_scheduler.run_scheduler_pass(db, _prepare))
    assert not res.refresh_deferred
    return res


def _legacy_put_admitted(cap: int, legacy_waiting: int) -> bool:
    """Drive the real legacy queue with the shared allowance: True when a put
    beyond the current legacy occupancy is admitted."""
    from src.core.session_task_queue import SessionTaskQueue

    q = SessionTaskQueue(maxsize=cap, key=lambda t: str(t.id))
    for i in range(legacy_waiting):  # occupancy that predates the managed rows
        q.put_nowait(SimpleNamespace(id=f"seed-{i}"))  # type: ignore[arg-type]
    q.share_allowance(turn_scheduler.ALLOWANCE)
    try:
        q.put_nowait(SimpleNamespace(id="over"))  # type: ignore[arg-type]
    except asyncio.QueueFull:
        return False
    return True


def test_S7_C1_release_managed_keeps_fleet_cap(db, hints):
    """Carrier release (claimed → pending) through the real task-server route:
    the allowance must count the released rows at once, so a legacy put can no
    longer push legacy + managed waiting past the fleet cap."""
    client = TestClient(ts.app)
    _register(client, "inc-1")
    cap = 3
    tokens = _seed_claimed(db, client, 2, "inc-1")
    _sync_allowance(db)
    hints.hints = 0
    for i, tok in enumerate(tokens):
        r = client.post(f"/tasks/t-{i}/release-managed",
                        json={"node_id": NODE, "claim_token": tok}, headers=H)
        assert r.status_code == 200, r.text
    assert db.managed_waiting_totals()["count"] == 2
    # The cache never under-counts committed managed waiting rows …
    assert turn_scheduler.ALLOWANCE.managed_cached() >= 2
    # … so with 1 legacy waiting item the fleet (1 + 2 == cap 3) is full.
    assert not _legacy_put_admitted(cap, legacy_waiting=1)
    assert hints.hints >= 1  # the scheduler re-reads the DB promptly


def test_S7_C1b_reregistration_superseded_grants_keep_fleet_cap(db, hints):
    """A carrier restart (new incarnation) releases the previous incarnation's
    unstarted grants via ``release_superseded_managed_grants``."""
    client = TestClient(ts.app)
    _register(client, "inc-1")
    cap = 4
    _seed_claimed(db, client, 3, "inc-1")
    _sync_allowance(db)
    hints.hints = 0
    _register(client, "inc-2")  # restart → superseded grants released
    assert db.managed_waiting_totals()["count"] == 3
    assert turn_scheduler.ALLOWANCE.managed_cached() >= 3
    assert not _legacy_put_admitted(cap, legacy_waiting=1)
    assert hints.hints >= 1


def test_S7_C1c_stale_refresh_after_release_cannot_lower_cache(db, hints):
    """A scheduler pass that read the DB count BEFORE the release must not apply
    that stale-low figure afterwards (the release bumps the generation)."""
    client = TestClient(ts.app)
    _register(client, "inc-1")
    tokens = _seed_claimed(db, client, 1, "inc-1")
    allowance = turn_scheduler.ALLOWANCE
    gen = allowance.snapshot_generation()
    stale_count = db.managed_waiting_totals()["count"]  # 0: still claimed
    r = client.post("/tasks/t-0/release-managed",
                    json={"node_id": NODE, "claim_token": tokens[0]}, headers=H)
    assert r.status_code == 200
    assert allowance.refresh_managed(stale_count, gen) is False
    assert allowance.managed_cached() >= 1


def test_S7_C1d_pass_refresh_corrects_over_count(db, hints):
    """The conservative raise is corrected by the next scheduler-pass refresh
    (no permanent over-count that would starve legacy work)."""
    client = TestClient(ts.app)
    _register(client, "inc-1")
    tokens = _seed_claimed(db, client, 1, "inc-1")
    r = client.post("/tasks/t-0/release-managed",
                    json={"node_id": NODE, "claim_token": tokens[0]}, headers=H)
    assert r.status_code == 200
    # Re-claim so the DB waiting count is 0 again, then a fresh pass refresh.
    r = client.post("/tasks/t-0/claim-managed",
                    json={"node_id": NODE, "incarnation_id": "inc-1"}, headers=H)
    assert r.status_code == 200
    _sync_allowance(db)
    assert turn_scheduler.ALLOWANCE.managed_cached() == 1  # still claimed: counted
    with db._write() as conn:  # it starts (out of process): no longer waiting
        conn.execute("UPDATE mesh_tasks SET status = 'running' WHERE id = 't-0'")
    _sync_allowance(db)
    assert turn_scheduler.ALLOWANCE.managed_cached() == 0


def test_S7_C1e_out_of_process_release_keeps_fleet_cap(db, hints):
    """Live topology: ``MESH_EMBEDDED_SERVER=false`` (config default; compose /
    ecosystem split) runs the task server in ANOTHER process — its release
    cannot touch this process's allowance or hint this scheduler. The legacy
    gate must hold anyway: unstarted (claimed) grants are counted, so a
    claimed → pending hand-back never lowers the true figure below the cache."""
    client = TestClient(ts.app)
    _register(client, "inc-1")
    cap = 3
    tokens = _seed_claimed(db, client, 2, "inc-1")
    _sync_allowance(db)
    hints.hints = 0
    for i, tok in enumerate(tokens):  # the other process's commit: DB only
        assert db.release_turn(f"t-{i}", tok)
    assert hints.hints == 0
    assert db.managed_waiting_totals()["count"] == 2
    assert not _legacy_put_admitted(cap, legacy_waiting=1)


def test_S7_C1f_claimed_rows_keep_a_bounded_refresh_clock(db, hints):
    """Claimed grants start (or are handed back) in the task-server process
    with no hint here: while any exist the scheduler re-reads the DB on the
    3 s fallback clock, so the conservative count is corrected promptly and
    never strands legacy capacity on the long safety net / indefinite sleep."""
    client = TestClient(ts.app)
    _register(client, "inc-1")
    _seed_claimed(db, client, 1, "inc-1")
    res = _sync_allowance(db)
    assert res.claimed == 1 and res.waiting == 0 and res.pending == 0
    timeout = turn_scheduler._next_timeout(res, turn_scheduler.ACTIVATION_LIMIT_PER_PASS,
                                           turn_scheduler.SAFETY_NET_SEC)
    assert timeout is not None and timeout <= turn_scheduler.FALLBACK_INTERVAL_SEC
