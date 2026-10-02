"""
[A82 Stage 4e] Managed-respawn activation revalidation guard.

``_respawn_manager_managed`` derives a DETERMINISTIC new session id from the
respawn token and then decides create-vs-reuse. The JSON session store
(``state/sessions/*.json``) is NEVER deleted per architecture rules, so
``self.session_store.get(new_sid)`` can return a STALE cached session even when
the canonical DB has no row for it. The DB is canonical: respawn must only REUSE
an existing session when a DB row exists; otherwise it must CREATE a fresh one
via ``session_service.create_session(..., session_id=new_sid)``.

This drives the GENUINE ``TaskOrchestrator._respawn_manager_managed`` against a
real ``MeshDB`` with a duck-typed ``self`` (reusing the harness patterns from
test_case_respawn.py) — no paid CLI, no live backend. The create-vs-reuse
decision is isolated by stubbing the turn-admission / turn-render seams.
"""

import asyncio
import hashlib

from src.core import Session, SessionStatus
from src.control.db import (
    MeshDB,
    respawn_task_id,
    RESPAWN_ACTION,
)
from src.orchestrator import TaskOrchestrator


# --------------------------------------------------------------------------- #
# Fixtures / helpers (mirroring test_case_respawn.py)                          #
# --------------------------------------------------------------------------- #

def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _on(monkeypatch) -> None:
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")
    monkeypatch.setenv("CASE_RESPAWN_REQUIRES_APPROVAL", "0")


class _FakeSession:
    def __init__(self, sid, status=SessionStatus.AWAITING_INPUT, backend="claude",
                 repo="/repo", machine_id="__local__"):
        self.session_id = sid
        self.status = status
        self.backend = backend
        self.repo_path = repo
        self.machine_id = machine_id
        self.current_case_id = None
        self.case_role = None


class _FakeStore:
    def __init__(self, *sessions):
        self._s = {s.session_id: s for s in sessions}

    def get(self, sid):
        return self._s.get(sid)

    def save(self, s):
        self._s[s.session_id] = s

    def add(self, s):
        self._s[s.session_id] = s


class _CreateResult:
    def __init__(self, session):
        self.ok = session is not None
        self.session = session
        self.reason = None if session is not None else "create_session_failed"


def _make_session(sid, *, backend="claude", repo="/repo", machine_id="__local__"):
    now = "2026-09-27T00:00:00Z"
    return Session(
        session_id=sid, backend=backend, repo_path=repo,
        status=SessionStatus.AWAITING_INPUT, created_at=now, updated_at=now,
        machine_id=machine_id, backend_session_id="", model=None, effort=None,
        last_task_id="", last_artifact_path="", last_summary="",
        last_user_message="", last_result_summary="", last_files_modified=[],
        telegram_chat_id=None, telegram_thread_id=None, owner_user_id=None,
        task_history=[], origin=None, driver_type="", driver_status="",
        cache_health="unknown", cache_unhealthy_count=0,
        previous_backend_session_ids=[], current_case_id=None, case_role=None,
        role_boot=None, continued_from=None, keep_pinned=False, keep_note="",
    )


class _FakeSessionService:
    """Spawns a fresh session under the caller-supplied deterministic id — the
    managed respawn path passes ``session_id=new_sid``. A real create writes a
    canonical ``sessions`` DB row (the managed path's ``enroll_session`` /
    ``record_respawn_link`` seams require one). Records every call so the test can
    assert whether create was reached (fresh session) or skipped (stale store row
    reused)."""

    def __init__(self, store, db):
        self.store = store
        self.db = db
        self.created = []

    def create_session(self, *, backend, repo_path, node_id, origin, bind_chat,
                        session_id):
        sess = _make_session(session_id, backend=backend, repo=repo_path,
                             machine_id=node_id)
        self.db.upsert_session(sess)  # canonical DB row (what a real create does)
        self.store.add(sess)
        self.created.append({"session_id": session_id, "node_id": node_id,
                             "backend": backend, "repo_path": repo_path})
        return _CreateResult(sess)


class _FakeOrch:
    """Duck-typed ``self`` carrying exactly what ``_respawn_manager_managed``
    touches, with the turn-admission / turn-render seams stubbed so the test
    isolates the create-vs-reuse decision."""

    def __init__(self, store, session_service):
        self.session_store = store
        self.session_service = session_service
        self.emitted = []
        self.admit_calls = []

    def _emit_event(self, name, _a, payload):
        self.emitted.append((name, payload))

    async def _admit_managed_recovery_turn(self, db, *, session_id, **kw):
        self.admit_calls.append(session_id)
        return "sturn-fake-1"

    def _render_respawn_turn(self, case_id, objective, dead_session_id=None):
        return f"resume {case_id}"

    async def _respawn_manager_managed(self, db, case_id, generation,
                                       dead_session_id, objective):
        return await TaskOrchestrator._respawn_manager_managed(
            self, db, case_id, generation, dead_session_id, objective,
        )


# --------------------------------------------------------------------------- #
# Guard — a STALE store row with NO DB row must NOT be reused                  #
# --------------------------------------------------------------------------- #

def test_managed_respawn_ignores_stale_store_row_without_db_row(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)

    objective = "ship feature X"
    dead_sid = "dead-mgr"
    generation = 1
    case_id = db.open_case(objective, dead_sid, role="manager",
                           completion_criteria='{"round_cap": 5}')

    # Pre-create the respawn token as PENDING so the token branch does NOT
    # short-circuit to "completed"/"owned".
    token_id = respawn_task_id(case_id, generation)
    db.enqueue_task(token_id, session_id=None, machine_id="__local__",
                    backend="claude", action=RESPAWN_ACTION,
                    payload={"case_id": case_id})

    # The deterministic new session id the method will compute.
    new_sid = hashlib.sha256(
        f"{token_id}\0respawn".encode("utf-8")).hexdigest()[:12]

    # STALE store row under new_sid, but NO canonical DB session row for it.
    stale = _FakeSession(new_sid, status=SessionStatus.AWAITING_INPUT)
    store = _FakeStore(stale)
    assert db.get_session(new_sid) is None  # canonical: no row

    svc = _FakeSessionService(store, db)
    orch = _FakeOrch(store, svc)

    ok = asyncio.run(
        orch._respawn_manager_managed(db, case_id, generation, dead_sid, objective)
    )

    # PRIMARY: the stale store row must be IGNORED — a fresh session is CREATED
    # because the canonical DB had no row for new_sid. WITHOUT the revalidation
    # guard the stale store row is reused and create is skipped (svc.created == []),
    # so this is the assertion that fails without the guard.
    assert len(svc.created) == 1, (
        "stale store row was reused despite no canonical DB row — "
        "revalidation guard missing"
    )
    assert svc.created[0]["session_id"] == new_sid
    # The created session is the one the recovery turn was admitted against.
    assert orch.admit_calls == [new_sid]
    # With the guard, the full respawn converges (the create wrote the canonical
    # row record_respawn_link needs), so the method reports owned.
    assert ok is True
