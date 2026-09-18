"""Event-driven Wake-Dispatcher optimisations.

The dispatcher used to recompute continuation state for EVERY open Case on every
30s tick by reading its whole flow_event log — O(cases x events) of blocking DB
work on the gateway event loop even when nothing had happened. These tests pin
the two behaviour-preserving wins:

1. ``MeshDB.max_flow_event_ids`` — one batched change-signal query.
2. ``_continue_case_once`` skips the per-Case read + recompute while a Case's
   newest flow_event id has not advanced since it was last found idle, and
   resumes recomputing the instant a new event lands.
"""

import asyncio

from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator

from tests.test_case_continuation import _FakeOrch, _FakeStore, _FakeSession, _open_case, _finished


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
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("CASE_RESPAWN_REQUIRES_APPROVAL", "0")
    db = _db(tmp_path)
    case_id = _open_case(db, session_id="mgr-sess")

    calls = {"n": 0}
    real_compute = db.compute_continuation_tick

    def _counting_compute(cid):
        calls["n"] += 1
        return real_compute(cid)

    db.compute_continuation_tick = _counting_compute  # type: ignore[assignment]

    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    orch._continuation_skip_cache = {}

    mid0 = db.max_flow_event_ids([case_id]).get(case_id)

    # First evaluation: nothing satisfied -> computes once and caches idle @ mid0.
    assert asyncio.run(
        TaskOrchestrator._continue_case_once(orch, db, case_id, cur_max_event_id=mid0)
    ) == 0
    assert calls["n"] == 1
    assert orch._continuation_skip_cache.get(case_id) == mid0

    # Second evaluation, SAME event id: provably identical -> skip the recompute.
    assert asyncio.run(
        TaskOrchestrator._continue_case_once(orch, db, case_id, cur_max_event_id=mid0)
    ) == 0
    assert calls["n"] == 1  # not incremented -> the 500-row read was skipped

    # A new event lands: the id advances, the skip must release and recompute.
    _finished(db, case_id, "t1")
    mid1 = db.max_flow_event_ids([case_id]).get(case_id)
    assert mid1 != mid0
    assert asyncio.run(
        TaskOrchestrator._continue_case_once(orch, db, case_id, cur_max_event_id=mid1)
    ) == 0
    assert calls["n"] == 2  # recomputed because the Case changed
