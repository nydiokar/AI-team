"""
[A82 Producer 7] Enrollment ROUTING proof for manager respawn.

``TaskOrchestrator._do_respawn_manager_for_case`` (src/orchestrator.py) contains
the Producer-7 fork at the enrollment gate::

    from src.control.turn_admission import session_enrollment
    if dead_session_id and await session_enrollment(db, dead_session_id):
        return await self._respawn_manager_managed(...)
    # else: legacy single-flight claim via db.enqueue_task(respawn_id,
    #        action=RESPAWN_ACTION, ...)

Every tick respawn path funnels through this method, but no test proved the
ROUTING end to end: that an ENROLLED dead Manager takes the managed path and an
UNENROLLED one takes the legacy single-flight path. These two tests close that
gap, driving the GENUINE method against a real ``MeshDB`` with the duck-typed
``_FakeOrch`` harness reused from test_case_respawn.py. The only override is
``_respawn_manager_managed`` — stubbed to record (never enter the real managed
machinery) so the test observes WHICH branch fired, not what the branch does.

Hermetic: real MeshDB on tmp_path, no network, no CLI.
"""

import asyncio

from src.core import Session, SessionStatus
from src.control.db import MeshDB, RESPAWN_ACTION, respawn_task_id
from src.orchestrator import TaskOrchestrator

# Reuse the proven harness verbatim — same duck-typed self / store / service.
from tests.test_case_respawn import (
    _db,
    _on,
    _FakeStore,
    _FakeSessionService,
    _FakeOrch,
    _open_case_with_dead_manager,
)


def _make_dead_session_row(db: MeshDB, sid: str) -> None:
    """Persist a canonical ``sessions`` row for the dead Manager so that
    ``enroll_session`` (a scoped UPDATE on that row) and the subsequent marker
    read can actually see it. Without a real row enrollment is a no-op."""
    now = "2026-09-27T00:00:00Z"
    sess = Session(
        session_id=sid, backend="claude", repo_path="/repo",
        status=SessionStatus.CLOSED, created_at=now, updated_at=now,
        machine_id="__local__", backend_session_id="", model=None, effort=None,
        last_task_id="", last_artifact_path="", last_summary="",
        last_user_message="", last_result_summary="", last_files_modified=[],
        telegram_chat_id=None, telegram_thread_id=None, owner_user_id=None,
        task_history=[], origin=None, driver_type="", driver_status="",
        cache_health="unknown", cache_unhealthy_count=0,
        previous_backend_session_ids=[], current_case_id=None, case_role=None,
        role_boot=None, continued_from=None, keep_pinned=False, keep_note="",
    )
    db.upsert_session(sess)


class _RoutingOrch(_FakeOrch):
    """``_FakeOrch`` with the managed path REPLACED by a recorder. The routing
    fork is what we test, not the managed machinery; recording lets us assert
    exactly which branch fired without entering the real managed procedure."""

    def __init__(self, store, session_service=None):
        super().__init__(store, session_service)
        self.managed_calls = []

    async def _respawn_manager_managed(self, db, case_id, generation,
                                       dead_session_id, objective):
        self.managed_calls.append(
            {"case_id": case_id, "generation": generation,
             "dead_session_id": dead_session_id, "objective": objective}
        )
        return True


# --------------------------------------------------------------------------- #
# (a) ENROLLED dead Manager → managed respawn (Producer 7), NO legacy row      #
# --------------------------------------------------------------------------- #

def test_enrolled_dead_manager_routes_to_managed_respawn(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)

    dead_sid = "dead-mgr-enrolled"
    objective = "ship feature X"
    case_id, _ = _open_case_with_dead_manager(db, dead_sid=dead_sid, objective=objective)

    # A canonical sessions row is required for enroll_session's scoped UPDATE to
    # persist the marker; then enrolling flips session_enrollment(...) → True.
    _make_dead_session_row(db, dead_sid)
    db.enroll_session(dead_sid)
    assert db.is_session_enrolled(dead_sid) is True

    store = _FakeStore()
    svc = _FakeSessionService(store)
    orch = _RoutingOrch(store, svc)

    generation = 1
    owned = asyncio.run(
        orch._do_respawn_manager_for_case(db, case_id, generation, dead_sid)
    )

    # ROUTING PROOF: the enrolled dead Manager took the MANAGED path.
    assert owned is True
    assert len(orch.managed_calls) == 1, "managed respawn was NOT invoked for an enrolled dead Manager"
    assert orch.managed_calls[0]["dead_session_id"] == dead_sid
    assert orch.managed_calls[0]["objective"] == objective

    # The managed branch RETURNS before the legacy single-flight enqueue, so NO
    # legacy respawn row exists and no legacy spawn side-effect fired.
    assert db.get_task(respawn_task_id(case_id, generation)) is None
    assert svc.created == []


# --------------------------------------------------------------------------- #
# (b) UNENROLLED dead Manager → legacy single-flight claim, managed NOT called #
# --------------------------------------------------------------------------- #

def test_unenrolled_dead_manager_takes_legacy_respawn(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)

    dead_sid = "dead-mgr-legacy"
    objective = "ship feature X"
    case_id, _ = _open_case_with_dead_manager(db, dead_sid=dead_sid, objective=objective)

    # Deliberately do NOT enroll. any_session_enrolled() is False here (nothing
    # enrolled anywhere) so session_enrollment(...) → False with no marker read.
    assert db.is_session_enrolled(dead_sid) is False

    store = _FakeStore()
    svc = _FakeSessionService(store)
    orch = _RoutingOrch(store, svc)

    generation = 1
    owned = asyncio.run(
        orch._do_respawn_manager_for_case(db, case_id, generation, dead_sid)
    )

    # ROUTING PROOF: the managed path was NEVER taken.
    assert orch.managed_calls == [], "unenrolled dead Manager wrongly routed to managed respawn"

    # The LEGACY single-flight claim row was created & owned by this tick.
    assert owned is True
    row = db.get_task(respawn_task_id(case_id, generation))
    assert row is not None, "legacy single-flight respawn row was NOT created"
    assert row["action"] == RESPAWN_ACTION
    # The legacy path drove through to a real spawn (one fresh session).
    assert len(svc.created) == 1
