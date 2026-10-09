"""Manager ↔ worker CONTRACT — what a Manager must experience, independent of how
the gateway implements it (pre-A104 wait groups, A84 outbox, A104 inbox ...).

Written from the Manager's point of view against the REAL managed stack (file-backed
MeshDB, real admission / scheduler / terminal seams / Wake-Dispatcher tick). The
expectations are the pre-A104 product behaviour, not the A104 internals:

  * a worker the Manager dispatched finishes ⇒ the Manager is woken ONCE with the
    "[continuation] Worker completion(s) are ready for your review on this Case
    (<case>). Finished since your last turn: <task>" turn + its review-gate text;
  * while its worker runs the Manager reads as WAITING (``waiting_workers``) — the
    session status is the primary state, the reason the secondary one;
  * a review recorded before the wake consumes the finish — no redundant paid wake;
  * rework → redispatch to the same (warm) worker → a SECOND wake for the redo;
    an unresolved rework blocks the close; accept unblocks it;
  * several workers finishing together arrive in ONE wake.

Each scenario runs for both client shapes that exist in production on 2026-10-09:
  * ``legacy``  — an un-redeployed mcp_manager: no requester id, POSTs /waits,
                  calls arm_wait_group(ANY) after dispatching;
  * ``stale``   — the new mcp_manager on a worker whose process env carries a DEAD
                  ``SESSION_ID`` (kanebra: ``d9342d3315a3``, closed 2026-09-08) — the
                  id the Manager's dispatch claims as its own is wrong.
The kanebra regression of 2026-10-09 (two Managers never nudged) was exactly the
``stale`` shape; these tests fail against the code that shipped before PR #216.
"""
import pytest

from src.core.interfaces import Session, SessionStatus as SS
from src.core.session_reason import derive_session_reasons
from tests.test_agent_inbox_delivery import (  # noqa: F401
    _add_session, _complete, _drive, _env, _fresh_allowance, _rows, _tick, _wakes,
)
from tests.test_turn_queue_4b import _pass, _run
from tests.test_turn_queue_producer1 import NOW, _flags, _no_cli_spawn, _submit  # noqa: F401

LEGACY_WAKE_HEAD = "[continuation] Worker completion(s) are ready for your review on this Case ({cid}). " \
                   "Finished since your last turn: {tids}."
CLIENTS = ("legacy", "stale")
STALE_ENV_SESSION = "d9342d3315a3"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_EMITTER_ENABLED", "1")
    db, o = _env(tmp_path, monkeypatch)
    # The dead session the stale worker env points at (closed long ago).
    db.upsert_session(Session(session_id=STALE_ENV_SESSION, backend="claude", repo_path="/tmp/repo",
                              status=SS.CLOSED, created_at=NOW, updated_at=NOW, machine_id="worker-a"))
    for w in ("w-1", "w-2"):
        _add_session(db, w)
    cid = db.open_case("ship the feature", "sess-1", role="manager")
    return db, o, cid


def _manager_turn(db, o, op):
    """The Manager starts a turn (operator message / its wake) and is executing."""
    t = _submit(o, description=f"operator: {op}", operation_id=op)
    _pass(db, o)
    tok = _run(db, t)
    return t, tok


def _dispatch(db, o, cid, worker, client, op):
    """dispatch_worker exactly as each production client does it."""
    kw = dict(description=f"do the work ({op})", session_id=worker, source="automation_session",
              join_case_id=cid, operation_id=op)
    if client == "stale":
        kw["requester_session_id"] = STALE_ENV_SESSION
    child = _submit(o, **kw)
    if client == "legacy":
        assert o.record_worker_wait(cid, str(child))["ok"]            # old client: POST /waits
        assert o.arm_wait_group(cid, f"g-{op}", "ANY", [str(child)])["ok"]  # old client: arm ANY
    return str(child)


def _finish_worker(db, o, child):
    _pass(db, o)
    _complete(db, child)


def _reason(db, o, sid="sess-1"):
    s = o.session_store.get(sid)
    s.status = SS.AWAITING_INPUT
    return (derive_session_reasons(db, [s])[sid] or {}).kind


@pytest.mark.parametrize("client", CLIENTS)
def test_CW01_finished_worker_wakes_its_manager_once_with_the_review_turn(world, client):
    db, o, cid = world
    turn, tok = _manager_turn(db, o, "op-1")
    child = _dispatch(db, o, cid, "w-1", client, "d-1")
    assert db.complete_turn(turn, tok, {"success": True, "output": "dispatched"}, status="completed")
    assert _reason(db, o) == "waiting_workers"                     # waiting on its worker
    _finish_worker(db, o, child)
    ran = _drive(db, o)
    wakes = _wakes(db)
    assert len(wakes) == 1 and len(ran) == 1, "the Manager must be woken exactly once"
    (wake,) = wakes
    assert wake["session_id"] == "sess-1"
    assert wake["prompt"].startswith(LEGACY_WAKE_HEAD.format(cid=cid, tids=child))
    assert "Run your review gate IN ORDER" in wake["prompt"]
    assert _rows(db, "SELECT id FROM mesh_tasks WHERE status = 'withdrawn'") == []
    assert _reason(db, o) != "waiting_workers"                      # nothing left waiting


@pytest.mark.parametrize("client", CLIENTS)
def test_CW02_review_before_the_wake_means_no_redundant_wake(world, client):
    db, o, cid = world
    turn, tok = _manager_turn(db, o, "op-1")
    child = _dispatch(db, o, cid, "w-1", client, "d-1")
    _finish_worker(db, o, child)
    # The operator turn is still running; the Manager reviews the worker in it.
    assert o.record_review(cid, verdict="accepted", task_id=child)["ok"]
    assert db.complete_turn(turn, tok, {"success": True, "output": "reviewed"}, status="completed")
    assert _drive(db, o) == [] and _wakes(db) == []
    assert _reason(db, o) != "waiting_workers"


@pytest.mark.parametrize("client", CLIENTS)
def test_CW03_rework_redispatch_wakes_again_and_gates_the_close(world, client):
    db, o, cid = world
    turn, tok = _manager_turn(db, o, "op-1")
    first = _dispatch(db, o, cid, "w-1", client, "d-1")
    assert db.complete_turn(turn, tok, {"success": True}, status="completed")
    _finish_worker(db, o, first)
    _tick(o)
    _pass(db, o)
    (wake1,) = _wakes(db)
    assert first in wake1["prompt"]
    # In its review wake the Manager asks for rework and sends the SAME worker back.
    wtok = _run(db, wake1["id"])
    assert o.record_review(cid, verdict="rework_requested", task_id=first, reason="tests missing")["ok"]
    redo = _dispatch(db, o, cid, "w-1", client, "d-2")
    assert db.complete_turn(wake1["id"], wtok, {"success": True}, status="completed")
    refused = o.close_case(cid, actor="operator")
    assert not refused["closed"] and "rework" in refused["reason"]
    assert _reason(db, o) == "waiting_workers"
    _finish_worker(db, o, redo)
    _drive(db, o)
    wakes = _wakes(db)
    assert len(wakes) == 2 and redo in wakes[1]["prompt"] and first not in wakes[1]["prompt"]
    assert o.record_review(cid, verdict="accepted", task_id=redo)["ok"]
    assert o.close_case(cid, actor="operator")["closed"]


@pytest.mark.parametrize("client", CLIENTS)
def test_CW04_parallel_workers_finishing_together_arrive_in_one_wake(world, client):
    db, o, cid = world
    turn, tok = _manager_turn(db, o, "op-1")
    a = _dispatch(db, o, cid, "w-1", client, "d-1")
    b = _dispatch(db, o, cid, "w-2", client, "d-2")
    assert db.complete_turn(turn, tok, {"success": True}, status="completed")
    _finish_worker(db, o, a)
    _finish_worker(db, o, b)
    _drive(db, o)
    (wake,) = _wakes(db)
    assert a in wake["prompt"] and b in wake["prompt"]


def test_CW05_the_stale_env_id_is_never_the_recipient(world):
    """The kanebra 2026-10-09 failure, asserted directly: the id the Manager's
    process env claims is dead — the completion must still reach the Manager,
    and the rejection is audited (never silent)."""
    db, o, cid = world
    turn, tok = _manager_turn(db, o, "op-1")
    child = _dispatch(db, o, cid, "w-1", "stale", "d-1")
    row = db.get_task(child)
    assert row["sender_session_id"] == "sess-1"
    assert db.has_flow_event(cid, "inbox.requester_unresolved", entity_id=child)
