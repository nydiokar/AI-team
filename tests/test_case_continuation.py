"""
M3.4 Job 1 — Autonomous Case continuation (live+idle re-entry).

The Wake-Dispatcher re-enters a live+idle Manager session when a wait-GROUP it
armed becomes satisfied: it schedules ONE deterministic ``mesh_tasks``
continuation row, atomically claims it (single winner), delivers ONE coalesced
proactive turn, and — on turn return — the HARNESS records the consumed
watermark. Bounded by a round cap; on exhaustion it escalates instead of
scheduling.

These tests exercise the whole contract from ``docs/AUTONOMOUS_CASE_CONTINUATION_
DESIGN.md`` §8 with a real ``MeshDB`` and a duck-typed orchestrator ``self`` — no
paid CLI, no live backend. Flag ``CASE_CONTINUATION_ENABLED`` gates every write.
"""

import asyncio
import socket

from src.core import SessionStatus
from src.control.db import (
    MeshDB,
    continuation_task_id,
    CONTINUATION_ACTION,
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
    # This file asserts the dead-session RESPAWN branch itself, so the operator
    # approval gate in front of it stays OFF here (covered in test_case_respawn.py).
    monkeypatch.setenv("CASE_RESPAWN_REQUIRES_APPROVAL", "0")


def _finished(db: MeshDB, case_id: str, task_id: str, outcome: str = "success") -> None:
    db.append_flow_event(
        case_id, "task.finished", "worker",
        entity_type="task", entity_id=task_id,
        payload={"outcome": outcome},
    )


def _reviewed(db: MeshDB, case_id: str, task_id: str, verdict: str = "accepted") -> None:
    """A Manager review verdict TAGGED to a worker task (entity_type='task') — the
    out-of-band consumption signal the Wake-Dispatcher reads."""
    db.append_flow_event(
        case_id, f"review.{verdict}", "manager",
        entity_type="task", entity_id=task_id,
        payload={"verdict": verdict, "reason": "ok"},
    )


def _events(db: MeshDB, case_id: str, event_type: str) -> list:
    return [e for e in db.list_flow_events(case_id) if e["event_type"] == event_type]


class _FakeSession:
    # Default AWAITING_INPUT: a Manager that armed a wait-group has already run a
    # turn, so that is the real post-turn state a wake target is in (NOT IDLE).
    def __init__(self, sid, status=SessionStatus.AWAITING_INPUT, backend="claude", repo="/repo"):
        self.session_id = sid
        self.status = status
        self.backend = backend
        self.repo_path = repo
        self.machine_id = "__local__"
        self.current_case_id = None
        self.case_role = None


class _FakeStore:
    def __init__(self, *sessions):
        self._s = {s.session_id: s for s in sessions}

    def get(self, sid):
        return self._s.get(sid)

    def add(self, s):
        self._s[s.session_id] = s

    def save(self, s):
        self._s[s.session_id] = s


class _CreateResult:
    def __init__(self, session):
        self.ok = session is not None
        self.session = session
        self.reason = None if session is not None else "create_session_failed"


class _FakeSessionService:
    """[A55] Minimal spawn stub so the dead-session branch's respawn path is
    exercisable from these continuation tests."""

    def __init__(self, store):
        self.store = store
        self.created = []

    def create_session(self, *, backend, repo_path, node_id, origin, bind_chat):
        sid = f"respawned-{len(self.created) + 1}"
        sess = _FakeSession(sid, status=SessionStatus.AWAITING_INPUT,
                            backend=backend, repo=repo_path)
        self.store.add(sess)
        self.created.append(sid)
        return _CreateResult(sess)


class _FakeNotifier:
    def __init__(self):
        self.errors = []

    async def notify_error(self, message, **kw):
        self.errors.append(message)


class _FakeOrch:
    """A duck-typed ``self`` that carries exactly the attributes the real
    ``_continue_case_once`` touches — so we drive the genuine orchestrator method
    against a real DB without booting the whole gateway."""

    def __init__(self, store):
        self.session_store = store
        self.session_service = _FakeSessionService(store)
        self.notifier = _FakeNotifier()
        self.active_tasks = {}
        self.running = True
        self.deliveries = []
        self.emitted = []
        self.finalized = []
        self.affiliations = []

    def _emit_event(self, name, _a, payload):
        self.emitted.append((name, payload))

    def cancel_task(self, task_id):
        return False

    async def interrupt_case(self, case_id, *, actor="operator", reason="operator_kill"):
        return await TaskOrchestrator.interrupt_case(
            self, case_id, actor=actor, reason=reason,
        )

    async def sweep_orphaned_cases(self, *, limit=200, dry_run=False, reason="manager_session_unavailable"):
        return await TaskOrchestrator.sweep_orphaned_cases(
            self, limit=limit, dry_run=dry_run, reason=reason,
        )

    async def set_case_state(self, case_id, *, state, actor="operator", reason="operator_state_change"):
        return await TaskOrchestrator.set_case_state(
            self, case_id, state=state, actor=actor, reason=reason,
        )

    def _manager_role_enabled(self):
        return TaskOrchestrator._manager_role_enabled(self)

    def _set_session_case_affiliation(self, sid, case_id, role=None):
        sess = self.session_store.get(sid)
        if sess is not None:
            sess.current_case_id = case_id
            sess.case_role = role
        self.affiliations.append((sid, case_id, role))

    async def _do_respawn_manager_for_case(self, db, case_id, generation, dead_sid):
        return await TaskOrchestrator._do_respawn_manager_for_case(
            self, db, case_id, generation, dead_sid,
        )

    async def _handle_dead_manager_session(self, db, case_id, generation, dead_sid):
        # The approval gate in front of the respawn. These tests assert the
        # dead-session BRANCH of the tick, so they run it with the gate OFF
        # (see _on) — the gate itself is covered in test_case_respawn.py.
        return await TaskOrchestrator._handle_dead_manager_session(
            self, db, case_id, generation, dead_sid,
        )

    async def _handle_quota_paused_case(self, db, case_id):
        # Inert for every Case here (none carries a `flow.quota_paused` event);
        # the real method is delegated rather than stubbed so that inertness is
        # proven, not assumed. Covered on its own in test_case_quota_resume.py.
        return await TaskOrchestrator._handle_quota_paused_case(self, db, case_id)

    def _render_respawn_turn(self, case_id, objective):
        return TaskOrchestrator._render_respawn_turn(self, case_id, objective)

    def _render_wake_turn(self, case_id, presented):
        return TaskOrchestrator._render_wake_turn(self, case_id, presented)

    async def submit_instruction(self, description, session_id, cwd, source):
        self.deliveries.append(
            {"description": description, "session_id": session_id, "source": source}
        )
        return f"wake-task-{len(self.deliveries)}"

    async def _escalate_case_continuation_cap(self, case_id, cap, generation):
        return await TaskOrchestrator._escalate_case_continuation_cap(
            self, case_id, cap, generation,
        )

    async def _escalate_headless_case(self, db, case_id, session_id):
        return await TaskOrchestrator._escalate_headless_case(
            self, db, case_id, session_id,
        )

    async def _finalize_continuation(self, *a, **k):
        # No-op stand-in so the background consumption task doesn't run here; the
        # HARNESS-records-consumption contract is asserted directly via
        # record_continuation_consumed (step 4).
        self.finalized.append((a, k))


def _continue(orch, db, case_id) -> int:
    return asyncio.run(TaskOrchestrator._continue_case_once(orch, db, case_id))


def _open_case(db, session_id="mgr-sess", round_cap=None):
    crit = None if round_cap is None else f'{{"round_cap": {round_cap}}}'
    return db.open_case("obj", session_id, role="manager", completion_criteria=crit)


# --------------------------------------------------------------------------- #
# Flag gating — OFF is byte-identical                                          #
# --------------------------------------------------------------------------- #

def test_arm_wait_group_noop_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("CASE_CONTINUATION_ENABLED", raising=False)
    db = _db(tmp_path)
    fid = _open_case(db)
    assert db.arm_wait_group(fid, "g1", "ANY", ["t1"]) is None
    assert _events(db, fid, "worker.wait_pending") == []


def test_tick_noop_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("CASE_CONTINUATION_ENABLED", raising=False)
    db = _db(tmp_path)
    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    # Even with a satisfied-looking ledger, the flag gate means the tick is inert.
    assert asyncio.run(TaskOrchestrator._wake_dispatcher_tick_once(orch)) == 0


# --------------------------------------------------------------------------- #
# Step 1 — not-yet-satisfied (ALL unmet)                                       #
# --------------------------------------------------------------------------- #

def test_all_condition_unsatisfied_until_every_member_finished(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = _open_case(db, round_cap=2)
    db.arm_wait_group(fid, "g1", "ALL", ["t1", "t2", "t3"])
    _finished(db, fid, "t1")
    _finished(db, fid, "t2")

    tick = db.compute_continuation_tick(fid)
    assert tick["satisfied"] is False

    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, fid) == 0
    assert db.list_continuation_rows(fid) == []  # no cont:{C}:1 row


# --------------------------------------------------------------------------- #
# Step 2 — satisfy → exactly one atomic claim, one coalesced turn              #
# --------------------------------------------------------------------------- #

def test_satisfy_schedules_one_row_single_claim_coalesced_turn(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=2)
    db.arm_wait_group(fid, "g1", "ALL", ["t1", "t2", "t3"])
    _finished(db, fid, "t1")
    _finished(db, fid, "t2")
    _finished(db, fid, "t3")

    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    delivered = _continue(orch, db, fid)
    assert delivered == 1

    cont_id = continuation_task_id(fid, 1)
    rows = db.list_continuation_rows(fid)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == cont_id
    assert row["status"] == "claimed"
    assert row["machine_id"] == CONTINUATION_MACHINE_SENTINEL
    assert row["action"] == CONTINUATION_ACTION
    assert row["claimed_by"] == socket.gethostname()

    # exactly ONE coalesced turn presenting all three members
    assert len(orch.deliveries) == 1
    assert orch.deliveries[0]["source"] == "manager_continuation"
    assert orch.deliveries[0]["session_id"] == "mgr-sess"
    for t in ("t1", "t2", "t3"):
        assert t in orch.deliveries[0]["description"]

    # a second claim on the same deterministic row loses
    assert db.claim_task(cont_id, socket.gethostname()) is False

    # round is 1 (highest continuation generation)
    _consumed, _completed, highest = db.continuation_watermark(fid)
    assert highest == 1


# --------------------------------------------------------------------------- #
# Regression — a Manager that has run a turn is AWAITING_INPUT, never IDLE.     #
# The Wake-Dispatcher must wake it; requiring strictly IDLE made the whole      #
# feature inert against every real (in-gateway OR node-carried) Manager.        #
# --------------------------------------------------------------------------- #

def test_awaiting_input_manager_is_woken(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=2)
    db.arm_wait_group(fid, "g1", "ANY", ["t1"])
    _finished(db, fid, "t1")

    # A Manager that armed a wait-group has, by definition, already run a turn —
    # so its session sits at AWAITING_INPUT (the real post-turn state), not IDLE.
    session = _FakeSession("mgr-sess", status=SessionStatus.AWAITING_INPUT)
    orch = _FakeOrch(_FakeStore(session))
    assert _continue(orch, db, fid) == 1
    assert len(db.list_continuation_rows(fid)) == 1
    assert len(orch.deliveries) == 1
    assert orch.deliveries[0]["session_id"] == "mgr-sess"
    assert "t1" in orch.deliveries[0]["description"]


def test_idle_never_run_session_is_not_woken(tmp_path, monkeypatch):
    # IDLE is not a wake condition: it means a freshly-created / reset / restored
    # session that has never run a turn, so it cannot legitimately own a satisfied
    # group. Even if the ledger looks satisfied, an IDLE session is skipped.
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=2)
    db.arm_wait_group(fid, "g1", "ANY", ["t1"])
    _finished(db, fid, "t1")

    session = _FakeSession("mgr-sess", status=SessionStatus.IDLE)
    orch = _FakeOrch(_FakeStore(session))
    assert _continue(orch, db, fid) == 0
    assert db.list_continuation_rows(fid) == []


# --------------------------------------------------------------------------- #
# [continuation-review-watermark] A finish the Manager already reviewed         #
# out-of-band (tagged review.*) is a consumption signal: it is NOT re-surfaced  #
# as a redundant wake, and its one-shot group retires without a paid turn.      #
# This is the live 2026-08-06 incident: a ceiling worker reviewed during an     #
# operator poke was re-woken as a "stale re-notification".                      #
# --------------------------------------------------------------------------- #

def test_reviewed_finish_is_not_re_woken_and_group_retires(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=3)
    db.arm_wait_group(fid, "batch-3", "ALL", ["t1"])
    _finished(db, fid, "t1")
    # Manager reviewed t1 out-of-band (e.g. during an operator turn) BEFORE the wake.
    _reviewed(db, fid, "t1", "accepted")

    tick = db.compute_continuation_tick(fid)
    assert tick["satisfied"] is False          # nothing new to present
    assert tick["presented_task_ids"] == []
    assert tick["retire_only_groups"] == ["batch-3"]

    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    # No paid wake is delivered, but the drained one-shot group is retired.
    assert _continue(orch, db, fid) == 0
    assert orch.deliveries == []               # no redundant paid turn
    assert db.list_continuation_rows(fid) == []
    resolved = _events(db, fid, "worker.wait_resolved")
    assert [e["entity_id"] for e in resolved] == ["batch-3"]
    assert any(n == "case_wait_group_review_drained" for n, _ in orch.emitted)

    # Idempotent: a second tick neither re-retires nor wakes.
    orch2 = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch2, db, fid) == 0
    assert orch2.deliveries == []
    assert len(_events(db, fid, "worker.wait_resolved")) == 1


def test_untagged_case_level_review_does_not_suppress_wake(tmp_path, monkeypatch):
    # A Case-level review (no task_id / no entity tag) is NOT a per-task consumption
    # signal — the wake still fires. This keeps pre-tagging behaviour byte-identical.
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=3)
    db.arm_wait_group(fid, "g1", "ALL", ["t1"])
    _finished(db, fid, "t1")
    db.append_flow_event(fid, "review.accepted", "manager",
                         payload={"verdict": "accepted"})  # untagged

    tick = db.compute_continuation_tick(fid)
    assert tick["satisfied"] is True
    assert tick["presented_task_ids"] == ["t1"]
    assert tick["retire_only_groups"] == []

    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, fid) == 1
    assert len(orch.deliveries) == 1


def test_any_group_partial_review_still_waits(tmp_path, monkeypatch):
    # ANY group [t1, t2]: t1 finished+reviewed out-of-band, t2 not finished. The
    # reviewed t1 is drained (no wake for it), but the group is NOT retired — it is
    # still a live obligation on t2. When t2 finishes it wakes normally.
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=3)
    db.arm_wait_group(fid, "g1", "ANY", ["t1", "t2"])
    _finished(db, fid, "t1")
    _reviewed(db, fid, "t1", "accepted")

    tick = db.compute_continuation_tick(fid)
    assert tick["satisfied"] is False
    assert tick["retire_only_groups"] == []    # t2 still outstanding — not drained

    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, fid) == 0
    assert orch.deliveries == []
    assert _events(db, fid, "worker.wait_resolved") == []

    # t2 finishes → a normal wake presenting ONLY the fresh, unreviewed t2.
    _finished(db, fid, "t2")
    tick2 = db.compute_continuation_tick(fid)
    assert tick2["satisfied"] is True
    assert tick2["presented_task_ids"] == ["t2"]
    orch2 = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch2, db, fid) == 1
    assert "t2" in orch2.deliveries[0]["description"]
    assert "t1" not in orch2.deliveries[0]["description"]


def test_satisfied_case_with_closed_manager_escalates_when_role_off(tmp_path, monkeypatch):
    # With MANAGER_ROLE OFF, respawn is not viable (a respawned Manager would be a
    # naked, tool-less session). The dead-session branch then falls back to the
    # pre-A55 visible-strand ESCALATION (once, idempotent) instead of respawning.
    _on(monkeypatch)
    monkeypatch.delenv("MANAGER_ROLE_ENABLED", raising=False)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=2)
    db.arm_wait_group(fid, "g1", "ANY", ["t1"])
    _finished(db, fid, "t1")

    session = _FakeSession("mgr-sess", status=SessionStatus.CLOSED)
    orch = _FakeOrch(_FakeStore(session))
    assert _continue(orch, db, fid) == 0
    assert db.list_continuation_rows(fid) == []
    marker = _events(db, fid, "case.manager_unavailable")
    assert len(marker) == 1
    assert len(orch.notifier.errors) == 1

    # Idempotent: a second tick does not double-escalate.
    assert _continue(orch, db, fid) == 0
    assert len(_events(db, fid, "case.manager_unavailable")) == 1
    assert len(orch.notifier.errors) == 1


def test_satisfied_case_with_closed_manager_respawns_when_role_on(tmp_path, monkeypatch):
    # [A55] With MANAGER_ROLE ON, a satisfied Case whose Manager session is CLOSED
    # is CRASH-RESPAWNED on the SAME Case — not left as a strand.
    _on(monkeypatch)
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=2)
    db.arm_wait_group(fid, "g1", "ANY", ["t1"])
    _finished(db, fid, "t1")

    session = _FakeSession("mgr-sess", status=SessionStatus.CLOSED)
    orch = _FakeOrch(_FakeStore(session))
    assert _continue(orch, db, fid) == 0
    assert len(orch.session_service.created) == 1  # exactly one respawn
    assert len(_events(db, fid, "case.manager_respawned")) == 1
    assert _events(db, fid, "case.manager_unavailable") == []


def test_missing_manager_session_respawns_when_role_on(tmp_path, monkeypatch):
    # [A55] Manager link resolves but the session row is GONE — respawn on the SAME
    # Case (with MANAGER_ROLE ON).
    _on(monkeypatch)
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=2)
    db.arm_wait_group(fid, "g1", "ANY", ["t1"])
    _finished(db, fid, "t1")

    orch = _FakeOrch(_FakeStore())  # empty store → session_store.get() returns None
    assert _continue(orch, db, fid) == 0
    assert len(orch.session_service.created) == 1
    assert len(_events(db, fid, "case.manager_respawned")) == 1
    assert _events(db, fid, "case.manager_unavailable") == []


def test_busy_manager_is_not_woken(tmp_path, monkeypatch):
    # BUSY = a turn already in flight; skip the needless enqueue.
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=2)
    db.arm_wait_group(fid, "g1", "ANY", ["t1"])
    _finished(db, fid, "t1")

    session = _FakeSession("mgr-sess", status=SessionStatus.BUSY)
    orch = _FakeOrch(_FakeStore(session))
    assert _continue(orch, db, fid) == 0
    assert db.list_continuation_rows(fid) == []


# --------------------------------------------------------------------------- #
# Step 3 — no concurrent duplicate under live ownership (session BUSY)         #
# --------------------------------------------------------------------------- #

def test_no_duplicate_while_turn_in_flight(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=2)
    db.arm_wait_group(fid, "g1", "ALL", ["t1", "t2", "t3"])
    for t in ("t1", "t2", "t3"):
        _finished(db, fid, t)

    session = _FakeSession("mgr-sess")
    orch = _FakeOrch(_FakeStore(session))
    assert _continue(orch, db, fid) == 1

    # the delivery marks the session BUSY (real submit_instruction would); simulate
    session.status = SessionStatus.BUSY
    # another tick while the row is claimed + session BUSY → no new row, no delivery
    assert _continue(orch, db, fid) == 0
    assert len(db.list_continuation_rows(fid)) == 1
    assert len(orch.deliveries) == 1


# --------------------------------------------------------------------------- #
# Step 4 — HARNESS-recorded consumption (not the LLM)                          #
# --------------------------------------------------------------------------- #

def test_harness_records_consumption_and_watermark_advances(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=5)
    db.arm_wait_group(fid, "g1", "ALL", ["t1", "t2", "t3"])
    for t in ("t1", "t2", "t3"):
        _finished(db, fid, t)

    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    _continue(orch, db, fid)
    cont_id = continuation_task_id(fid, 1)

    # the HARNESS (not the LLM) records consumption on turn return
    db.record_continuation_consumed(fid, cont_id, 1, ["t1", "t2", "t3"], ["g1"])

    row = db.get_task(cont_id)
    assert row["status"] == "completed"
    consumed, completed, _highest = db.continuation_watermark(fid)
    assert consumed == {"t1", "t2", "t3"}
    assert completed == 1
    # the one-shot ALL group is discharged
    assert [e["entity_id"] for e in _events(db, fid, "worker.wait_resolved")] == ["g1"]

    # a later tick delivers nothing (group resolved, everything consumed)
    orch2 = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch2, db, fid) == 0
    assert db.compute_continuation_tick(fid)["satisfied"] is False


# --------------------------------------------------------------------------- #
# Step 5 — redelivery ONLY after released ownership (crash / at-least-once)    #
# --------------------------------------------------------------------------- #

def test_redelivery_after_incarnation_bump_reaps_claim(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    node = socket.gethostname()
    inc1 = db.upsert_node(node, "", 9001, ["claude"], 2)

    fid = _open_case(db, round_cap=5)
    # round 1 already consumed
    db.arm_wait_group(fid, "g1", "ALL", ["t1"])
    _finished(db, fid, "t1")
    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    _continue(orch, db, fid)
    db.record_continuation_consumed(fid, continuation_task_id(fid, 1), 1, ["t1"], ["g1"])

    # round 2: a fresh group satisfied
    db.arm_wait_group(fid, "g2", "ALL", ["t2"])
    _finished(db, fid, "t2")
    orch2 = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch2, db, fid) == 1
    cont2 = continuation_task_id(fid, 2)
    assert db.get_task(cont2)["status"] == "claimed"
    assert len(orch2.deliveries) == 1

    # crash BEFORE consumption: the gateway restarts in place → new incarnation.
    inc2 = db.upsert_node(node, "", 9001, ["claude"], 2)
    assert inc2 != inc1
    # the reaper sees the claim held by the dead incarnation as stale …
    stale = db.list_stale_claims(lease_sec=0)
    stale_ids = {r["id"] for r in stale}
    assert cont2 in stale_ids
    # … and releases it back to pending (no consumption was ever written).
    assert db.release_task(cont2, node) is True
    assert db.get_task(cont2)["status"] == "pending"

    # next tick re-claims and REDELIVERS the same coalesced set (at-least-once).
    orch3 = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch3, db, fid) == 1
    assert db.get_task(cont2)["status"] == "claimed"
    assert "t2" in orch3.deliveries[0]["description"]


# --------------------------------------------------------------------------- #
# Step 6 — round cap → escalation instead of scheduling                       #
# --------------------------------------------------------------------------- #

def test_round_cap_exhaustion_interrupts_and_escalates(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=2)

    # drive two full consumed rounds (generations 1 and 2)
    for gen, (gid, tid) in enumerate([("g1", "t1"), ("g2", "t2")], start=1):
        db.arm_wait_group(fid, gid, "ALL", [tid])
        _finished(db, fid, tid)
        orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
        assert _continue(orch, db, fid) == 1
        db.record_continuation_consumed(
            fid, continuation_task_id(fid, gen), gen, [tid], [gid],
        )

    _consumed, completed, _highest = db.continuation_watermark(fid)
    assert completed == 2  # cap is 2

    # a THIRD satisfied condition would be generation 3 > cap 2 → interrupt
    db.arm_wait_group(fid, "g3", "ALL", ["t3"])
    _finished(db, fid, "t3")
    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, fid) == 0

    interrupts = _events(db, fid, "flow.interrupted")
    assert len(interrupts) == 1
    assert len(db.list_continuation_rows(fid)) == 2  # no cont:{C}:3 row
    assert any(n == "case_continuation_interrupted" for n, _ in orch.emitted)
    assert orch.notifier.errors  # operator escalation fired

    # idempotent: another satisfied tick does NOT emit a second interrupt
    orch2 = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch2, db, fid) == 0
    assert len(_events(db, fid, "flow.interrupted")) == 1


# --------------------------------------------------------------------------- #
# ANY condition — edge-triggered, repeating                                    #
# --------------------------------------------------------------------------- #

def test_any_condition_repeats_on_each_new_completion(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=9)
    db.arm_wait_group(fid, "g1", "ANY", ["t1", "t2"])

    # first completion → satisfied, presents just t1
    _finished(db, fid, "t1")
    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, fid) == 1
    assert "t1" in orch.deliveries[0]["description"]
    assert "t2" not in orch.deliveries[0]["description"]
    db.record_continuation_consumed(fid, continuation_task_id(fid, 1), 1, ["t1"], [])

    # ANY group is NOT retired (t2 still open) — a later completion re-satisfies
    assert db.compute_continuation_tick(fid)["satisfied"] is False  # t1 consumed, t2 unfinished
    _finished(db, fid, "t2")
    orch2 = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch2, db, fid) == 1
    assert "t2" in orch2.deliveries[0]["description"]
    assert db.get_task(continuation_task_id(fid, 2))["status"] == "claimed"


# --------------------------------------------------------------------------- #
# [A53] A killed (blocked) Case is NOT auto-resumed by the Wake-Dispatcher     #
# --------------------------------------------------------------------------- #

def test_blocked_case_is_not_continued(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    fid = _open_case(db, round_cap=2)
    db.arm_wait_group(fid, "g1", "ALL", ["t1", "t2"])
    _finished(db, fid, "t1")
    _finished(db, fid, "t2")
    # operator kill → blocked; the satisfied wait-group must NOT re-drive it
    db.update_flow_run(fid, status="blocked")

    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, fid) == 0
    assert db.list_continuation_rows(fid) == []
    assert orch.deliveries == []


# --------------------------------------------------------------------------- #
# Operator orphan cleanup                                                     #
# --------------------------------------------------------------------------- #

def test_sweep_orphaned_cases_blocks_missing_manager_case(tmp_path, monkeypatch):
    monkeypatch.setattr("src.control.db._db_instance", _db(tmp_path))
    from src.control.db import get_db

    db = get_db()
    fid = _open_case(db, session_id="missing-manager")
    orch = _FakeOrch(_FakeStore())

    result = asyncio.run(orch.sweep_orphaned_cases(limit=20))

    assert result["ok"] is True
    assert [c["case_id"] for c in result["candidates"]] == [fid]
    assert [c["case_id"] for c in result["cleaned"]] == [fid]
    assert db.get_flow_run(fid)["status"] == "blocked"
    interrupts = _events(db, fid, "flow.interrupted")
    assert len(interrupts) == 1


def test_sweep_orphaned_cases_skips_active_manager_and_dry_run_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr("src.control.db._db_instance", _db(tmp_path))
    from src.control.db import get_db

    db = get_db()
    active = _FakeSession("active-manager", status=SessionStatus.AWAITING_INPUT)
    active_case = _open_case(db, session_id=active.session_id)
    missing_case = _open_case(db, session_id="missing-manager")
    orch = _FakeOrch(_FakeStore(active))

    result = asyncio.run(orch.sweep_orphaned_cases(limit=20, dry_run=True))

    assert result["dry_run"] is True
    assert [c["case_id"] for c in result["candidates"]] == [missing_case]
    assert result["cleaned"] == []
    assert db.get_flow_run(active_case)["status"] is None
    assert db.get_flow_run(missing_case)["status"] is None


def test_sweep_orphaned_cases_treats_pinned_offline_manager_as_inactive(tmp_path, monkeypatch):
    monkeypatch.setattr("src.control.db._db_instance", _db(tmp_path))
    from src.control.db import get_db

    db = get_db()
    offline = _FakeSession("offline-manager", status=SessionStatus.PINNED_NODE_OFFLINE)
    fid = _open_case(db, session_id=offline.session_id)
    orch = _FakeOrch(_FakeStore(offline))

    result = asyncio.run(orch.sweep_orphaned_cases(limit=20, dry_run=True))

    assert [c["case_id"] for c in result["candidates"]] == [fid]
    assert result["candidates"][0]["reason"] == "manager_session_pinned_node_offline"


def test_sweep_orphaned_cases_skips_error_manager_as_recoverable(tmp_path, monkeypatch):
    monkeypatch.setattr("src.control.db._db_instance", _db(tmp_path))
    from src.control.db import get_db

    db = get_db()
    manager = _FakeSession("error-manager", status=SessionStatus.ERROR)
    fid = _open_case(db, session_id=manager.session_id)
    orch = _FakeOrch(_FakeStore(manager))

    result = asyncio.run(orch.sweep_orphaned_cases(limit=20, dry_run=True))

    assert result["candidates"] == []
    assert db.get_flow_run(fid)["status"] is None


def test_manager_unavailable_interrupt_refuses_active_manager(tmp_path, monkeypatch):
    monkeypatch.setattr("src.control.db._db_instance", _db(tmp_path))
    from src.control.db import get_db

    db = get_db()
    manager = _FakeSession("active-manager", status=SessionStatus.AWAITING_INPUT)
    fid = _open_case(db, session_id=manager.session_id)
    orch = _FakeOrch(_FakeStore(manager))

    result = asyncio.run(orch.interrupt_case(
        fid,
        actor="operator",
        reason="manager_session_unavailable",
    ))

    assert result["ok"] is False
    assert result["reason"] == "manager_session_active"
    assert db.get_flow_run(fid)["status"] is None
    assert _events(db, fid, "flow.interrupted") == []


def test_set_case_state_open_unblocks_case(tmp_path, monkeypatch):
    monkeypatch.setattr("src.control.db._db_instance", _db(tmp_path))
    from src.control.db import get_db

    db = get_db()
    fid = _open_case(db, session_id="mgr")
    db.update_flow_run(fid, status="blocked")
    orch = _FakeOrch(_FakeStore())

    result = asyncio.run(orch.set_case_state(
        fid,
        state="open",
        reason="manager_session_active_recovered",
    ))

    assert result == {"ok": True, "changed": True, "status": "open"}
    assert db.get_flow_run(fid)["status"] is None
    unblocked = _events(db, fid, "flow.unblocked")
    assert len(unblocked) == 1
