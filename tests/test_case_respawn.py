"""
M3.4 Job 3 — Crash-respawn dispatcher path (A55).

When a Wake-Dispatcher tick finds a SATISFIED Case whose bound Manager session is
DEAD (gone/closed), the harness reconstructs the Case from the DB alone (A54's
``get_case_brief``), respawns EXACTLY ONE role-full Manager bound to the SAME
Case (same ``flow_run_id``, same objective — never a new Case), re-arms its
waits/groups, and resumes toward closure — under a strict single-flight lease
(the SAME atomic ``mesh_tasks`` claim the continuation lease uses) so a racing
tick never double-respawns.

These tests drive the GENUINE ``TaskOrchestrator._do_respawn_manager_for_case`` /
``_continue_case_once`` against a real ``MeshDB`` with a duck-typed ``self`` — no
paid CLI, no live backend. Flags ``CASE_CONTINUATION_ENABLED`` +
``MANAGER_ROLE_ENABLED`` gate the respawn.

A104: "satisfied" now means the dead Manager has a deliverable agent-inbox
message (a child it requested finished — ``tests/inbox_seed``); the tick reaches
the respawn through ``_deliver_inbox``'s dead-recipient branch (the recipient is
the Case's bound Manager seat).
"""

import asyncio
import socket

from src.core import SessionStatus
from src.control.db import (
    MeshDB,
    respawn_task_id,
)
from src.orchestrator import TaskOrchestrator
from tests.inbox_seed import seed_finished_child


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #

def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _on(monkeypatch) -> None:
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")
    # These tests drive the GENUINE unconditional respawn machinery directly —
    # the approval gate in front of it (CASE_RESPAWN_REQUIRES_APPROVAL, default
    # ON) is covered separately in test_case_respawn_approval_gate.py.
    monkeypatch.setenv("CASE_RESPAWN_REQUIRES_APPROVAL", "0")


def _finished(db: MeshDB, case_id: str, task_id: str, requester: str) -> None:
    """A child ``requester`` dispatched finished: its completion is in the inbox."""
    seed_finished_child(db, case_id, task_id, requester=requester)


def _events(db: MeshDB, case_id: str, event_type: str) -> list:
    return [e for e in db.list_flow_events(case_id) if e["event_type"] == event_type]


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


class _FakeNotifier:
    def __init__(self):
        self.errors = []

    async def notify_error(self, message, **kw):
        self.errors.append(message)


class _CreateResult:
    def __init__(self, session):
        self.ok = session is not None
        self.session = session
        self.reason = None if session is not None else "create_session_failed"


class _FakeSessionService:
    """Spawns a fresh AWAITING_INPUT session and registers it in the store — the
    minimum ``_do_respawn_manager_for_case`` needs. ``fail=True`` simulates a spawn
    failure AFTER the single-flight claim (recovery path)."""

    def __init__(self, store, fail=False):
        self.store = store
        self.fail = fail
        self.created = []
        self._n = 0

    def create_session(self, *, backend, repo_path, node_id, origin, bind_chat):
        if self.fail:
            return _CreateResult(None)
        self._n += 1
        sid = f"respawned-mgr-{self._n}"
        sess = _FakeSession(sid, status=SessionStatus.AWAITING_INPUT,
                            backend=backend, repo=repo_path, machine_id=node_id)
        self.store.add(sess)
        self.created.append({"session_id": sid, "node_id": node_id,
                             "backend": backend, "repo_path": repo_path})
        return _CreateResult(sess)


class _FakeOrch:
    """A duck-typed ``self`` carrying exactly the attributes the real respawn /
    continuation methods touch."""

    def __init__(self, store, session_service=None):
        self.session_store = store
        self.session_service = session_service or _FakeSessionService(store)
        self.notifier = _FakeNotifier()
        self.active_tasks = {}
        self.running = True
        self.deliveries = []
        self.emitted = []
        self.affiliations = []
        self.managed_respawns = []

    def _emit_event(self, name, _a, payload):
        self.emitted.append((name, payload))

    def _manager_role_enabled(self):
        return TaskOrchestrator._manager_role_enabled(self)

    def _render_wake_turn(self, case_id, presented):
        return TaskOrchestrator._render_wake_turn(self, case_id, presented)

    def _render_respawn_turn(self, case_id, objective, dead_session_id=None):
        return TaskOrchestrator._render_respawn_turn(
            self, case_id, objective, dead_session_id,
        )

    def _set_session_case_affiliation(self, sid, case_id, role=None):
        # Mirror the real seam's observable effect (store + record) without the DB
        # column write path — enough for the "bound as manager" assertion.
        sess = self.session_store.get(sid)
        if sess is not None:
            sess.current_case_id = case_id
            sess.case_role = role
        self.affiliations.append((sid, case_id, role))

    async def submit_instruction(self, description, session_id, cwd, source):
        self.deliveries.append(
            {"description": description, "session_id": session_id, "source": source}
        )
        return f"turn-{len(self.deliveries)}"

    async def _escalate_headless_case(self, db, case_id, session_id):
        return await TaskOrchestrator._escalate_headless_case(self, db, case_id, session_id)

    async def _escalate_case_continuation_cap(self, case_id, cap, generation):
        return await TaskOrchestrator._escalate_case_continuation_cap(self, case_id, cap, generation)

    async def _do_respawn_manager_for_case(self, db, case_id, generation, dead_sid):
        return await TaskOrchestrator._do_respawn_manager_for_case(
            self, db, case_id, generation, dead_sid,
        )

    async def _handle_dead_manager_session(self, db, case_id, generation, dead_sid):
        return await TaskOrchestrator._handle_dead_manager_session(
            self, db, case_id, generation, dead_sid,
        )

    async def _deliver_inbox(self, db, recipient, case_id, **kw):
        return await TaskOrchestrator._deliver_inbox(self, db, recipient, case_id, **kw)

    async def _escalate_round_cap_once(self, db, case_id, cap, generation):
        return await TaskOrchestrator._escalate_round_cap_once(self, db, case_id, cap, generation)

    async def _handle_quota_paused_case(self, db, case_id):
        # The quota-pause branch of the tick. Inert here (no Case in this file
        # carries a `flow.quota_paused` event) — it is covered on its own in
        # test_case_quota_resume.py. Delegating to the REAL method keeps that
        # inertness honest rather than asserting it with a stub.
        return await TaskOrchestrator._handle_quota_paused_case(self, db, case_id)

    async def _handle_transient_paused_case(self, db, case_id):
        # The transient-5xx pause branch of the tick. Inert here (no Case carries a
        # `flow.transient_paused` event) — covered on its own in
        # test_case_transient_resume.py. Delegated to the REAL method so the
        # inertness is proven, not assumed.
        return await TaskOrchestrator._handle_transient_paused_case(self, db, case_id)

    async def _respawn_manager_managed(self, db, case_id, generation, dead_sid, objective):
        # [A82 Stage 8a] The respawned Manager is born managed, so EVERY dead
        # Manager is replaced on the managed path (producer 7). Its mechanics
        # (exactly one session/turn under concurrent ticks, same Case, no new
        # Case, spawn failure ⇒ not owned then converges) are proven on real
        # pieces in test_turn_queue_stage8a.py S8-10 and the 4e suites; here
        # only the tick's respawn DECISION is observed.
        self.managed_respawns.append((case_id, generation, dead_sid, objective))
        return True


def _continue(orch, db, case_id) -> int:
    return asyncio.run(TaskOrchestrator._continue_case_once(orch, db, case_id))


def _open_case_with_dead_manager(db, dead_sid="dead-mgr", round_cap=5,
                                 objective="ship feature X"):
    """Open a Case whose bound Manager session link exists but whose session row
    is dead (not in the store / closed). Returns (case_id, dead_sid)."""
    crit = f'{{"round_cap": {round_cap}}}'
    case_id = db.open_case(objective, dead_sid, role="manager", completion_criteria=crit)
    return case_id, dead_sid


# --------------------------------------------------------------------------- #
# Acceptance 1 — dead + satisfied → exactly ONE role-full Manager respawned    #
# --------------------------------------------------------------------------- #

def test_dead_satisfied_case_respawns_exactly_one_manager(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    case_id, dead_sid = _open_case_with_dead_manager(db)
    db.arm_wait_group(case_id, "g1", "ALL", ["t1", "t2"])
    _finished(db, case_id, "t1", dead_sid)
    _finished(db, case_id, "t2", dead_sid)

    # The bound Manager session is GONE: store is empty of the dead sid.
    store = _FakeStore()  # dead_sid not present ⇒ session_store.get() → None
    svc = _FakeSessionService(store)
    orch = _FakeOrch(store, svc)

    # A tick on the satisfied+dead Case respawns instead of stranding.
    assert _continue(orch, db, case_id) == 0  # respawn returns 0 (new session woken next tick)

    # [A82 Stage 8a] exactly ONE (managed) respawn of THIS Case's dead Manager,
    # with the Case's objective; no legacy spawn, no strand escalation.
    assert orch.managed_respawns == [(case_id, 1, dead_sid, "ship feature X")]
    assert svc.created == [] and orch.deliveries == []
    assert db.get_task(respawn_task_id(case_id, 1)) is None
    assert _events(db, case_id, "case.manager_unavailable") == []
    assert orch.notifier.errors == []


def test_closed_manager_session_is_respawned(tmp_path, monkeypatch):
    # A CLOSED (not merely missing) session is also dead → respawn.
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    case_id, dead_sid = _open_case_with_dead_manager(db)
    db.arm_wait_group(case_id, "g1", "ANY", ["t1"])
    _finished(db, case_id, "t1", dead_sid)

    dead = _FakeSession(dead_sid, status=SessionStatus.CLOSED,
                        repo="/repo-dead", machine_id="__local__")
    store = _FakeStore(dead)
    svc = _FakeSessionService(store)
    orch = _FakeOrch(store, svc)

    assert _continue(orch, db, case_id) == 0
    assert [r[:3] for r in orch.managed_respawns] == [(case_id, 1, dead_sid)]
    assert _events(db, case_id, "case.manager_unavailable") == []


# [A82 Stage 8a] RETIRED here (legacy single-flight respawn mechanics, a branch
# no dead Manager reaches any more): test_concurrent_ticks_respawn_exactly_one_manager,
# test_second_claim_on_respawn_row_loses, test_respawn_preserves_flow_run_id_and_creates_no_new_case,
# test_spawn_failure_after_claim_releases_lease_and_escalates,
# test_reaped_respawn_claim_retries_next_tick. Their oracles on the managed path:
# test_turn_queue_stage8a.py::test_S8_10_* (exactly one under concurrent ticks,
# same Case / no new Case / objective, spawn failure ⇒ not owned then converges)
# and the producer-7 crash-safety suite (lost ack / crash between claim and start).


def test_respawn_turn_offers_prior_session_readback():
    """A fresh Manager rebuilds from the ledger, but the ledger holds verdicts —
    not the prior Manager's own reasoning/in-flight intent. When the dead session
    is known, the resume turn must point the new Manager at read_session_history
    so it can catch up on what was actually done, without inheriting the fat
    context. No dead session id ⇒ no dangling hint."""
    orch = _FakeOrch(_FakeStore())
    turn = orch._render_respawn_turn("case-x", "do the thing", "dead-sid-123")
    assert "read_session_history" in turn
    assert "dead-sid-123" in turn

    bare = orch._render_respawn_turn("case-x", "do the thing", None)
    assert "read_session_history" not in bare


# --------------------------------------------------------------------------- #
# Acceptance 4 — REFUSE blocked / interrupted (operator-halted) Cases          #
# --------------------------------------------------------------------------- #

def test_blocked_case_with_dead_manager_is_not_respawned(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    case_id, dead_sid = _open_case_with_dead_manager(db)
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    _finished(db, case_id, "t1", dead_sid)
    # operator kill → blocked; even with a dead Manager + satisfied wait, NO respawn.
    db.update_flow_run(case_id, status="blocked")

    store = _FakeStore()
    svc = _FakeSessionService(store)
    orch = _FakeOrch(store, svc)

    # the 'blocked' guard in _continue_case_once short-circuits BEFORE the dead branch
    assert _continue(orch, db, case_id) == 0
    assert svc.created == []
    assert _events(db, case_id, "case.manager_respawned") == []
    assert db.get_task(respawn_task_id(case_id, 1)) is None
    assert orch.deliveries == []
    # the message is HELD (not killed) while the Case is blocked.
    assert [r["task_id"] for r in db.reconcile_worker_waits(case_id)["resolved"]] == ["t1"]


# --------------------------------------------------------------------------- #
# Guard — MANAGER_ROLE off ⇒ no naked respawn, strand escalation instead       #
# --------------------------------------------------------------------------- #

def test_manager_role_off_falls_back_to_strand_escalation(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")
    monkeypatch.delenv("MANAGER_ROLE_ENABLED", raising=False)  # role OFF
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    case_id, dead_sid = _open_case_with_dead_manager(db)
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    _finished(db, case_id, "t1", dead_sid)

    store = _FakeStore()
    svc = _FakeSessionService(store)
    orch = _FakeOrch(store, svc)
    assert _continue(orch, db, case_id) == 0
    # no respawn (would be a naked, tool-less Manager) — escalate the strand instead
    assert svc.created == []
    assert _events(db, case_id, "case.manager_respawned") == []
    assert len(_events(db, case_id, "case.manager_unavailable")) == 1
    assert len(orch.notifier.errors) == 1
    # [A104] the strand is terminal for the message: dead(recipient_gone), not
    # retried every tick.
    assert db.pending_for(dead_sid, case_id=case_id).messages == []

