"""Event-driven Wake-Dispatcher optimisations.

The dispatcher used to recompute continuation state for EVERY open Case on every
30s tick by reading its whole flow_event log — O(cases x events) of blocking DB
work on the gateway event loop even when nothing had happened. These tests pin
the two behaviour-preserving wins:

1. ``MeshDB.max_flow_event_ids`` — one batched change-signal query.
2. [A104] The dispatcher no longer keeps a per-Case skip-cache keyed on
   ``max_flow_event_ids``: its work list is the agent inbox's ready
   (recipient, Case) pairs (an index-served read), so an idle Case costs no
   per-Case delivery work at all, and is delivered the moment a message lands.
"""

from src.control.db import MeshDB

from tests.test_agent_inbox_delivery import _fresh_allowance  # noqa: F401 — autouse fixture
from tests.test_case_continuation import _open_case, _finished
from tests.test_turn_queue_producer1 import _flags, _no_cli_spawn  # noqa: F401


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def test_max_flow_event_ids_is_batched_and_correct(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    db = _db(tmp_path)
    c1 = _open_case(db, session_id="s1")
    c2 = _open_case(db, session_id="s2")
    _finished(db, c1, "t1")
    _finished(db, c1, "t2")
    _finished(db, c2, "t3")

    ids = db.max_flow_event_ids([c1, c2, "does-not-exist"])
    # Both real Cases present; the absent one is simply omitted (caller = 'compute').
    assert set(ids.keys()) == {c1, c2}
    # The newest id per Case matches the tail of its own event list — proving the
    # batched GROUP BY isolates each Case correctly (no cross-Case bleed).
    assert ids[c1] == max(e["id"] for e in db.list_flow_events(c1))
    assert ids[c2] == max(e["id"] for e in db.list_flow_events(c2))
    # Empty input is a no-op (no query).
    assert db.max_flow_event_ids([]) == {}


def test_idle_case_recompute_is_skipped_until_a_new_event_lands(tmp_path, monkeypatch):
    """[A104] An open Case with an empty inbox gets NO per-Case delivery call on a
    tick (no flow_event read, no recompute); a completion landing in the inbox
    makes the very next tick deliver it."""
    from tests.inbox_seed import seed_finished_child
    from tests.test_agent_inbox_delivery import _env, _tick

    db, o = _env(tmp_path, monkeypatch)
    case_id = _open_case(db, session_id="sess-1")
    _finished(db, case_id, "noise")  # Case events alone are not a delivery signal

    calls = []
    real_deliver = o._deliver_inbox

    async def _counting_deliver(db_, recipient, cid, **kw):
        calls.append((recipient, cid))
        return await real_deliver(db_, recipient, cid, **kw)

    o._deliver_inbox = _counting_deliver

    assert _tick(o) == 0 and _tick(o) == 0
    assert calls == []  # idle: no per-Case delivery work

    seed_finished_child(db, case_id, "t1", requester="sess-1")
    assert _tick(o) == 1
    assert calls == [("sess-1", case_id)]
