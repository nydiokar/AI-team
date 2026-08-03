"""
M3.4 Job 3 — Crash-respawn dispatcher path (A55).

When a Wake-Dispatcher tick finds a SATISFIED Case whose bound Manager session is
DEAD (gone/closed), the harness reconstructs the Case from the DB alone (A54's
``get_case_brief``), respawns EXACTLY ONE role-full Manager bound to the SAME
Case (same ``flow_run_id``, same objective — never a new Case), re-arms its
waits/groups, and resumes toward closure — under a strict single-flight lease
(the SAME atomic ``mesh_tasks`` claim the continuation lease uses) so a racing
tick never double-respawns.

These tests drive the GENUINE ``TaskOrchestrator._respawn_manager_for_case`` /
``_continue_case_once`` against a real ``MeshDB`` with a duck-typed ``self`` — no
paid CLI, no live backend. Flags ``CASE_CONTINUATION_ENABLED`` +
``MANAGER_ROLE_ENABLED`` gate the respawn.
"""

import asyncio
import socket

from src.core import SessionStatus
from src.control.db import (
    MeshDB,
    respawn_task_id,
    RESPAWN_ACTION,
    CONTINUATION_MACHINE_SENTINEL,
)
from src.orchestrator import TaskOrchestrator


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #

def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _on(monkeypatch) -> None:
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")


def _finished(db: MeshDB, case_id: str, task_id: str, outcome: str = "success") -> None:
    db.append_flow_event(
        case_id, "task.finished", "worker",
        entity_type="task", entity_id=task_id,
        payload={"outcome": outcome},
    )


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
    minimum ``_respawn_manager_for_case`` needs. ``fail=True`` simulates a spawn
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

    def _emit_event(self, name, _a, payload):
        self.emitted.append((name, payload))

    def _manager_role_enabled(self):
        return TaskOrchestrator._manager_role_enabled(self)

    def _render_wake_turn(self, case_id, presented):
        return TaskOrchestrator._render_wake_turn(self, case_id, presented)

    def _render_respawn_turn(self, case_id, objective):
        return TaskOrchestrator._render_respawn_turn(self, case_id, objective)

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

    async def _respawn_manager_for_case(self, db, case_id, generation, dead_sid):
        return await TaskOrchestrator._respawn_manager_for_case(
            self, db, case_id, generation, dead_sid,
        )


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
    _finished(db, case_id, "t1")
    _finished(db, case_id, "t2")

    # The bound Manager session is GONE: store is empty of the dead sid.
    store = _FakeStore()  # dead_sid not present ⇒ session_store.get() → None
    svc = _FakeSessionService(store)
    orch = _FakeOrch(store, svc)

    # A tick on the satisfied+dead Case respawns instead of stranding.
    assert _continue(orch, db, case_id) == 0  # respawn returns 0 (new session woken next tick)

    # exactly ONE Manager respawned, bound to the SAME Case as role=manager
    assert len(svc.created) == 1
    new_sid = svc.created[0]["session_id"]
    mgr_links = db.list_flow_links(flow_run_id=case_id, entity_type="session", role="manager")
    link_sids = [l["entity_id"] for l in mgr_links]
    assert new_sid in link_sids
    # the wake target now resolves to the fresh live session
    assert db.case_manager_session_id(case_id) == new_sid
    assert store.get(new_sid).case_role == "manager"

    # a resume turn (role-full, RESUMING the same Case) was delivered
    assert len(orch.deliveries) == 1
    d = orch.deliveries[0]
    assert d["source"] == "manager_respawn"
    assert d["session_id"] == new_sid
    assert case_id in d["description"]
    assert "ship feature X" in d["description"]
    assert "get_case" in d["description"]  # instructed to reconstruct

    # respawn marker recorded, no strand escalation fired
    assert len(_events(db, case_id, "case.manager_respawned")) == 1
    assert _events(db, case_id, "case.manager_unavailable") == []
    assert orch.notifier.errors == []

    # single-flight row COMPLETED (respawn owner finished)
    row = db.get_task(respawn_task_id(case_id, 1))
    assert row is not None
    assert row["status"] == "completed"
    assert row["machine_id"] == CONTINUATION_MACHINE_SENTINEL
    assert row["action"] == RESPAWN_ACTION


def test_closed_manager_session_is_respawned(tmp_path, monkeypatch):
    # A CLOSED (not merely missing) session is also dead → respawn.
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    case_id, dead_sid = _open_case_with_dead_manager(db)
    db.arm_wait_group(case_id, "g1", "ANY", ["t1"])
    _finished(db, case_id, "t1")

    dead = _FakeSession(dead_sid, status=SessionStatus.CLOSED,
                        repo="/repo-dead", machine_id="__local__")
    store = _FakeStore(dead)
    svc = _FakeSessionService(store)
    orch = _FakeOrch(store, svc)

    assert _continue(orch, db, case_id) == 0
    assert len(svc.created) == 1
    # node/repo reused from the dead session row (persisted by open_case's session link?)
    assert len(_events(db, case_id, "case.manager_respawned")) == 1


# --------------------------------------------------------------------------- #
# Acceptance 2 — two CONCURRENT ticks → single-flight, exactly ONE respawn     #
# --------------------------------------------------------------------------- #

def test_concurrent_ticks_respawn_exactly_one_manager(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    case_id, dead_sid = _open_case_with_dead_manager(db)
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    _finished(db, case_id, "t1")

    # Two independent orchestrators (two ticks) share the SAME db + dead Case, each
    # with an empty store (session dead). Run their respawn attempts concurrently.
    storeA, storeB = _FakeStore(), _FakeStore()
    svcA, svcB = _FakeSessionService(storeA), _FakeSessionService(storeB)
    orchA, orchB = _FakeOrch(storeA, svcA), _FakeOrch(storeB, svcB)

    async def _race():
        # generation is 1 (no rounds consumed yet) — both compute the SAME respawn id
        return await asyncio.gather(
            orchA._respawn_manager_for_case(db, case_id, 1, dead_sid),
            orchB._respawn_manager_for_case(db, case_id, 1, dead_sid),
        )

    resA, resB = asyncio.run(_race())

    # both report "owned" (True) — the winner respawned, the loser saw the claim taken
    assert resA is True and resB is True
    # …but EXACTLY ONE actually spawned a session
    total_created = len(svcA.created) + len(svcB.created)
    assert total_created == 1, f"expected exactly one respawn, got {total_created}"
    # exactly one respawn marker, one respawn row, one live manager link
    assert len(_events(db, case_id, "case.manager_respawned")) == 1
    row = db.get_task(respawn_task_id(case_id, 1))
    assert row["status"] == "completed"
    mgr_links = db.list_flow_links(flow_run_id=case_id, entity_type="session", role="manager")
    # original dead link + exactly one respawned link
    respawned = [l for l in mgr_links if l["entity_id"] != dead_sid]
    assert len(respawned) == 1


def test_second_claim_on_respawn_row_loses(tmp_path, monkeypatch):
    # Prove the ATOMIC claim directly (not check-then-act): once the winner claims
    # respawn:{C}:{gen}, a second claim_task on the same deterministic id is False.
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    case_id, dead_sid = _open_case_with_dead_manager(db)
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    _finished(db, case_id, "t1")

    store = _FakeStore()
    orch = _FakeOrch(store)
    assert asyncio.run(orch._respawn_manager_for_case(db, case_id, 1, dead_sid)) is True

    # the deterministic respawn row is claimed/completed — a fresh claim loses
    assert db.claim_task(respawn_task_id(case_id, 1), socket.gethostname()) is False


# --------------------------------------------------------------------------- #
# Acceptance 3 — SAME flow_run_id, SAME objective, NO new Case (anti-goal)     #
# --------------------------------------------------------------------------- #

def test_respawn_preserves_flow_run_id_and_creates_no_new_case(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    cases_before = {c["flow_run_id"] for c in db.list_open_cases()}
    case_id, dead_sid = _open_case_with_dead_manager(db, objective="migrate DB to v9")
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    _finished(db, case_id, "t1")
    open_before = {c["flow_run_id"] for c in db.list_open_cases()}
    assert open_before == cases_before | {case_id}

    store = _FakeStore()
    orch = _FakeOrch(store)
    assert asyncio.run(orch._respawn_manager_for_case(db, case_id, 1, dead_sid)) is True

    # NO new Case: the open-case set is UNCHANGED after the respawn.
    open_after = {c["flow_run_id"] for c in db.list_open_cases()}
    assert open_after == open_before, "respawn must NOT mint a new Case"

    # the respawned Manager's Case is the SAME flow_run_id with the SAME objective
    new_sid = orch.session_service.created[0]["session_id"]
    assert store.get(new_sid).current_case_id == case_id
    brief = db.get_case_brief(case_id)
    assert brief["objective"] == "migrate DB to v9"
    # objective_lock on the row is untouched
    assert db.get_flow_run(case_id)["objective_lock"] == "migrate DB to v9"


# --------------------------------------------------------------------------- #
# Acceptance 4 — REFUSE blocked / interrupted (operator-halted) Cases          #
# --------------------------------------------------------------------------- #

def test_blocked_case_with_dead_manager_is_not_respawned(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    case_id, dead_sid = _open_case_with_dead_manager(db)
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    _finished(db, case_id, "t1")
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
    _finished(db, case_id, "t1")

    store = _FakeStore()
    svc = _FakeSessionService(store)
    orch = _FakeOrch(store, svc)
    assert _continue(orch, db, case_id) == 0
    # no respawn (would be a naked, tool-less Manager) — escalate the strand instead
    assert svc.created == []
    assert _events(db, case_id, "case.manager_respawned") == []
    assert len(_events(db, case_id, "case.manager_unavailable")) == 1
    assert len(orch.notifier.errors) == 1


# --------------------------------------------------------------------------- #
# Recovery — spawn failure AFTER the claim releases the lease for a retry      #
# --------------------------------------------------------------------------- #

def test_spawn_failure_after_claim_releases_lease_and_escalates(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    case_id, dead_sid = _open_case_with_dead_manager(db)
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    _finished(db, case_id, "t1")

    store = _FakeStore()
    svc = _FakeSessionService(store, fail=True)  # spawn fails after the claim
    orch = _FakeOrch(store, svc)

    assert _continue(orch, db, case_id) == 0
    # respawn returned False ⇒ the caller escalated the strand …
    assert len(_events(db, case_id, "case.manager_unavailable")) == 1
    # … and the single-flight row was RELEASED back to pending so a later tick retries
    row = db.get_task(respawn_task_id(case_id, 1))
    assert row is not None
    assert row["status"] == "pending", "lease must be released for at-least-once retry"


def test_reaped_respawn_claim_retries_next_tick(tmp_path, monkeypatch):
    # A crash BETWEEN claim and respawn leaves the row 'claimed' by a dead
    # incarnation → the SAME reaper returns it to pending → a later tick re-claims.
    _on(monkeypatch)
    db = _db(tmp_path)
    node = socket.gethostname()
    inc1 = db.upsert_node(node, "", 9001, ["claude"], 2)
    case_id, dead_sid = _open_case_with_dead_manager(db)
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    _finished(db, case_id, "t1")

    # Simulate: a tick claimed the respawn row then crashed before completing it.
    respawn_id = respawn_task_id(case_id, 1)
    db.enqueue_task(respawn_id, session_id=None,
                    machine_id=CONTINUATION_MACHINE_SENTINEL, backend="claude",
                    action=RESPAWN_ACTION, payload={"case_id": case_id})
    assert db.claim_task(respawn_id, node) is True

    # gateway restarts in place → new incarnation; the reaper sees the stale claim …
    inc2 = db.upsert_node(node, "", 9001, ["claude"], 2)
    assert inc2 != inc1
    stale = {r["id"] for r in db.list_stale_claims(lease_sec=0)}
    assert respawn_id in stale
    assert db.release_task(respawn_id, node) is True
    assert db.get_task(respawn_id)["status"] == "pending"

    # next tick re-claims and respawns (at-least-once — no permanent stall)
    store = _FakeStore()
    svc = _FakeSessionService(store)
    orch = _FakeOrch(store, svc)
    assert _continue(orch, db, case_id) == 0
    assert len(svc.created) == 1
    assert db.get_task(respawn_id)["status"] == "completed"
