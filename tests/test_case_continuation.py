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

[A104] The wait-group ledger / ``cont:`` token / watermark machinery is replaced
by the agent inbox: a finished REQUESTED child writes one message addressed to
its requester, and the Wake-Dispatcher admits ONE managed wake turn per
(recipient, Case) carrying the ready messages. The contract tests below are
re-seeded through the inbox (``tests/inbox_seed``) and positive deliveries run on
the real H3 managed harness (``tests/test_agent_inbox_delivery``). The duck
``_FakeOrch`` stays for paths that never reach admission, and as a shared helper
for other suites.
"""

import asyncio

from src.core import SessionStatus
from src.control.db import MeshDB
from src.orchestrator import TaskOrchestrator
from tests.inbox_seed import finish_child, seed_child, seed_finished_child
from tests.test_agent_inbox_delivery import (  # noqa: F401 — autouse fixtures
    _drive, _env, _fresh_allowance, _wakes,
)
from tests.test_turn_queue_4b import _pass, _run
from tests.test_turn_queue_producer1 import _flags, _no_cli_spawn  # noqa: F401


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
        self.managed_respawns = []

    def _emit_event(self, name, _a, payload):
        self.emitted.append((name, payload))

    def cancel_task(self, task_id):
        return False

    async def interrupt_case(self, case_id, *, actor="operator", reason="operator_kill"):
        return await TaskOrchestrator.interrupt_case(
            self, case_id, actor=actor, reason=reason,
        )

    async def sweep_orphaned_cases(self, *, limit=200, dry_run=False, reason="manager_session_unavailable", close_terminal_orphans=True):
        return await TaskOrchestrator.sweep_orphaned_cases(
            self, limit=limit, dry_run=dry_run, reason=reason, close_terminal_orphans=close_terminal_orphans,
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

    async def _respawn_manager_managed(self, db, case_id, generation, dead_sid, objective):
        # [A82 Stage 8a] Every dead Manager is replaced on the managed path
        # (producer 7, born-managed replacement). Its mechanics are proven on
        # real pieces in test_turn_queue_stage8a.py (S8-10) and the 4e suites;
        # here only the tick's decision to respawn is observed.
        self.managed_respawns.append((case_id, generation, dead_sid))
        return True

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

    async def _handle_transient_paused_case(self, db, case_id):
        # The transient-5xx pause branch of the tick. Inert here (no Case in this
        # file carries a `flow.transient_paused` event) — covered on its own in
        # test_case_transient_resume.py. Delegated to the REAL method so its
        # inertness is proven, not stubbed.
        return await TaskOrchestrator._handle_transient_paused_case(self, db, case_id)

    async def _deliver_inbox(self, db, recipient, case_id, **kw):
        # [A104] The REAL per-recipient delivery gates (closed/blocked Case, round
        # cap, dead recipient → respawn/escalate). This duck has no managed
        # admission (_admit_inbox_wake), so it is only used for paths that stop
        # before admission; positive deliveries run on the H3 harness.
        return await TaskOrchestrator._deliver_inbox(self, db, recipient, case_id, **kw)

    async def _escalate_round_cap_once(self, db, case_id, cap, generation):
        return await TaskOrchestrator._escalate_round_cap_once(self, db, case_id, cap, generation)

    def _render_respawn_turn(self, case_id, objective, dead_session_id=None):
        return TaskOrchestrator._render_respawn_turn(
            self, case_id, objective, dead_session_id,
        )

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
# [A104] The wake contract now runs on the agent inbox. Positive-delivery tests #
# drive the REAL managed admission (the H3 harness of                          #
# test_agent_inbox_delivery: file-backed MeshDB as get_db(), real scheduler    #
# pass, real claim/start/complete seams, no CLI). Requester = "sess-1".        #
# Tests whose path never reaches admission (dead Manager, blocked Case) keep   #
# the duck ``_FakeOrch`` above, re-seeded through the inbox.                   #
# --------------------------------------------------------------------------- #

def _h3_case(db, round_cap=None, session_id="sess-1"):
    return _open_case(db, session_id=session_id, round_cap=round_cap)


def _cont(o, db, case_id) -> int:
    """The per-Case seam on the REAL orchestrator (H3)."""
    return asyncio.run(o._continue_case_once(db, case_id))


def _msg_states(db) -> dict:
    return {
        r["about_task_id"]: r["state"]
        for r in db._conn().execute("SELECT about_task_id, state FROM agent_inbox").fetchall()
    }


# --------------------------------------------------------------------------- #
# Step 1 — not-yet-satisfied (ALL unmet)                                       #
# --------------------------------------------------------------------------- #

def test_all_condition_unsatisfied_until_every_member_finished(tmp_path, monkeypatch):
    """[A104] An ALL wait filter HOLDS its members' messages until every member
    task is terminal — no wake while one is still running."""
    db, o = _env(tmp_path, monkeypatch)
    fid = _h3_case(db, round_cap=2)
    for t in ("t1", "t2", "t3"):
        seed_child(db, fid, t, requester="sess-1", token=f"tok-{t}")
    db.arm_wait_group(fid, "g1", "ALL", ["t1", "t2", "t3"])
    finish_child(db, "t1", token="tok-t1")
    finish_child(db, "t2", token="tok-t2")

    view = db.pending_for("sess-1", case_id=fid)
    assert sorted((m.about_task_id, m.held) for m in view.messages) == [("t1", True), ("t2", True)]
    assert view.outstanding_task_ids == ["t3"]
    assert _cont(o, db, fid) == 0
    assert _wakes(db) == []


# --------------------------------------------------------------------------- #
# Step 2 — satisfy → exactly one admitted, coalesced wake turn                 #
# --------------------------------------------------------------------------- #

def test_satisfy_schedules_one_row_single_claim_coalesced_turn(tmp_path, monkeypatch):
    """[A104] Three finished requested children ⇒ ONE coalesced wake turn
    (``wake_<hex>``, continuation, addressed to the requester, Case provenance)
    carrying all three messages; a second producer pass admits nothing."""
    db, o = _env(tmp_path, monkeypatch)
    fid = _h3_case(db, round_cap=2)
    for t in ("t1", "t2", "t3"):
        seed_finished_child(db, fid, t, requester="sess-1")

    assert _cont(o, db, fid) == 1

    (wake,) = _wakes(db)
    assert wake["id"].startswith("wake_")
    assert wake["session_id"] == "sess-1"
    assert wake["flow_run_id"] == fid
    for t in ("t1", "t2", "t3"):
        assert t in wake["prompt"]
    carried = db.pending_for("sess-1", case_id=fid).carried_by(wake["id"])
    assert sorted(m.about_task_id for m in carried) == ["t1", "t2", "t3"]

    # a racing / repeated producer pass collapses: one wake in flight per recipient
    assert _cont(o, db, fid) == 0
    assert len(_wakes(db)) == 1


# --------------------------------------------------------------------------- #
# Regression — a Manager that has run a turn is AWAITING_INPUT, never IDLE.     #
# The Wake-Dispatcher must wake it; requiring strictly IDLE made the whole      #
# feature inert against every real (in-gateway OR node-carried) Manager.        #
# --------------------------------------------------------------------------- #

def test_awaiting_input_manager_is_woken(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)  # sess-1 is AWAITING_INPUT (post-turn state)
    assert o.session_store.get("sess-1").status == SessionStatus.AWAITING_INPUT
    fid = _h3_case(db, round_cap=2)
    seed_finished_child(db, fid, "t1", requester="sess-1")

    assert _cont(o, db, fid) == 1
    (wake,) = _wakes(db)
    assert wake["session_id"] == "sess-1"
    assert "t1" in wake["prompt"]


# --------------------------------------------------------------------------- #
# [continuation-review-watermark] A finish the Manager already reviewed         #
# out-of-band (tagged review.*) is a consumption signal: it is NOT re-surfaced  #
# as a redundant wake. Under A104 the tagged review ACKS the inbox message in   #
# the review's own txn. This is the live 2026-08-06 incident: a ceiling worker  #
# reviewed during an operator poke was re-woken as a "stale re-notification".   #
# --------------------------------------------------------------------------- #

def test_reviewed_finish_is_not_re_woken_and_group_retires(tmp_path, monkeypatch):
    """[A104] A tagged review before the wake acks t1's message: no wake, and a
    later tick stays quiet (the legacy group-retire markers are gone)."""
    db, o = _env(tmp_path, monkeypatch)
    fid = _h3_case(db, round_cap=3)
    seed_child(db, fid, "t1", requester="sess-1")
    db.arm_wait_group(fid, "batch-3", "ALL", ["t1"])
    finish_child(db, "t1")
    # Manager reviewed t1 out-of-band (e.g. during an operator turn) BEFORE the wake.
    _reviewed(db, fid, "t1", "accepted")

    assert _msg_states(db) == {"t1": "acked"}
    assert db.pending_for("sess-1", case_id=fid).messages == []
    assert _cont(o, db, fid) == 0
    assert _wakes(db) == []                    # no redundant paid turn

    # Idempotent: a second pass neither wakes nor resurrects the message.
    assert _drive(db, o) == []
    assert _wakes(db) == [] and _msg_states(db) == {"t1": "acked"}


def test_untagged_case_level_review_does_not_suppress_wake(tmp_path, monkeypatch):
    # A Case-level review (no task_id / no entity tag) is NOT a per-task consumption
    # signal — the wake still fires. This keeps pre-tagging behaviour byte-identical.
    db, o = _env(tmp_path, monkeypatch)
    fid = _h3_case(db, round_cap=3)
    seed_finished_child(db, fid, "t1", requester="sess-1")
    db.append_flow_event(fid, "review.accepted", "manager",
                         payload={"verdict": "accepted"})  # untagged

    assert [m.state for m in db.pending_for("sess-1", case_id=fid).messages] == ["pending"]
    assert _cont(o, db, fid) == 1
    (wake,) = _wakes(db)
    assert "t1" in wake["prompt"]


def test_any_group_partial_review_still_waits(tmp_path, monkeypatch):
    # ANY group [t1, t2]: t1 finished+reviewed out-of-band, t2 not finished. The
    # reviewed t1 is acked (no wake for it); t2 is still an outstanding request.
    # When t2 finishes it wakes normally, presenting ONLY t2.
    db, o = _env(tmp_path, monkeypatch)
    fid = _h3_case(db, round_cap=3)
    seed_child(db, fid, "t1", requester="sess-1", token="tok-t1")
    seed_child(db, fid, "t2", requester="sess-1", token="tok-t2")
    db.arm_wait_group(fid, "g1", "ANY", ["t1", "t2"])
    finish_child(db, "t1", token="tok-t1")
    _reviewed(db, fid, "t1", "accepted")

    view = db.pending_for("sess-1", case_id=fid)
    assert view.messages == [] and view.outstanding_task_ids == ["t2"]
    assert _cont(o, db, fid) == 0
    assert _wakes(db) == []

    finish_child(db, "t2", token="tok-t2")
    assert _cont(o, db, fid) == 1
    (wake,) = _wakes(db)
    assert "t2" in wake["prompt"]
    assert "t1" not in wake["prompt"]


def test_satisfied_case_with_closed_manager_escalates_when_role_off(tmp_path, monkeypatch):
    # With MANAGER_ROLE OFF, respawn is not viable (a respawned Manager would be a
    # naked, tool-less session). The dead-session branch then falls back to the
    # pre-A55 visible-strand ESCALATION (once, idempotent) instead of respawning;
    # [A104] the stranded message dies (recipient_gone) instead of looping.
    _on(monkeypatch)
    monkeypatch.delenv("MANAGER_ROLE_ENABLED", raising=False)
    db = _db(tmp_path)
    fid = _open_case(db, round_cap=2)
    seed_finished_child(db, fid, "t1", requester="mgr-sess")

    session = _FakeSession("mgr-sess", status=SessionStatus.CLOSED)
    orch = _FakeOrch(_FakeStore(session))
    assert _continue(orch, db, fid) == 0
    assert orch.managed_respawns == []
    marker = _events(db, fid, "case.manager_unavailable")
    assert len(marker) == 1
    assert len(orch.notifier.errors) == 1
    assert _msg_states(db) == {"t1": "dead"}

    # Idempotent: a second tick does not double-escalate.
    assert _continue(orch, db, fid) == 0
    assert len(_events(db, fid, "case.manager_unavailable")) == 1
    assert len(orch.notifier.errors) == 1


def test_satisfied_case_with_closed_manager_respawns_when_role_on(tmp_path, monkeypatch):
    # [A55] With MANAGER_ROLE ON, a Case whose bound Manager session is CLOSED and
    # holds an undelivered message is CRASH-RESPAWNED on the SAME Case — not left
    # as a strand. [A104] The message stays pending for the successor.
    _on(monkeypatch)
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    db = _db(tmp_path)
    fid = _open_case(db, round_cap=2)
    seed_finished_child(db, fid, "t1", requester="mgr-sess")

    session = _FakeSession("mgr-sess", status=SessionStatus.CLOSED)
    orch = _FakeOrch(_FakeStore(session))
    assert _continue(orch, db, fid) == 0
    # [A82 Stage 8a] exactly one (managed) respawn on the SAME Case, no strand
    assert orch.managed_respawns == [(fid, 1, "mgr-sess")]
    assert _events(db, fid, "case.manager_unavailable") == []
    assert _msg_states(db) == {"t1": "pending"}


def test_missing_manager_session_respawns_when_role_on(tmp_path, monkeypatch):
    # [A55] Manager link resolves but the session row is GONE — respawn on the SAME
    # Case (with MANAGER_ROLE ON).
    _on(monkeypatch)
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    db = _db(tmp_path)
    fid = _open_case(db, round_cap=2)
    seed_finished_child(db, fid, "t1", requester="mgr-sess")

    orch = _FakeOrch(_FakeStore())  # empty store → session_store.get() returns None
    assert _continue(orch, db, fid) == 0
    # [A82 Stage 8a] exactly one (managed) respawn on the SAME Case, no strand
    assert [r[0] for r in orch.managed_respawns] == [fid]
    assert _events(db, fid, "case.manager_unavailable") == []
    assert _msg_states(db) == {"t1": "pending"}


# --------------------------------------------------------------------------- #
# Step 3 — no concurrent duplicate while a wake is in flight                   #
# --------------------------------------------------------------------------- #

def test_no_duplicate_while_turn_in_flight(tmp_path, monkeypatch):
    """[A104] One wake in flight per (recipient, Case): while it runs, a further
    pass — even with a NEW completion arriving — admits nothing; the new message
    waits and goes out in the next wake after the first completes."""
    db, o = _env(tmp_path, monkeypatch)
    fid = _h3_case(db, round_cap=3)
    for t in ("t1", "t2", "t3"):
        seed_finished_child(db, fid, t, requester="sess-1")

    assert _cont(o, db, fid) == 1
    _pass(db, o)
    (wake,) = _wakes(db)
    tok = _run(db, wake["id"])                 # the wake is RUNNING on the session
    seed_finished_child(db, fid, "t4", requester="sess-1")
    assert _cont(o, db, fid) == 0
    assert len(_wakes(db)) == 1

    assert db.complete_turn(wake["id"], tok, {"success": True, "output": "ok"}, status="completed")
    ran = _drive(db, o)
    assert len(ran) == 1 and len(_wakes(db)) == 2
    assert "t4" in _wakes(db)[1]["prompt"] and "t1" not in _wakes(db)[1]["prompt"]
    assert set(_msg_states(db).values()) == {"acked"}


# --------------------------------------------------------------------------- #
# Step 4 — HARNESS-recorded consumption (not the LLM)                          #
# --------------------------------------------------------------------------- #

def test_harness_records_consumption_and_watermark_advances(tmp_path, monkeypatch):
    """[A104] The wake's own terminal transition (the harness, not the LLM) acks
    every message it carried; the round counter advances and a later pass
    delivers nothing."""
    db, o = _env(tmp_path, monkeypatch)
    fid = _h3_case(db, round_cap=5)
    for t in ("t1", "t2", "t3"):
        seed_finished_child(db, fid, t, requester="sess-1")

    ran = _drive(db, o)
    assert len(ran) == 1
    assert db.get_task(ran[0])["status"] == "completed"
    assert _msg_states(db) == {"t1": "acked", "t2": "acked", "t3": "acked"}
    assert db.inbox_rounds_used(fid) == 1

    # a later pass delivers nothing (everything consumed)
    assert _cont(o, db, fid) == 0
    assert _drive(db, o) == []
    assert db.pending_for("sess-1", case_id=fid).messages == []


# --------------------------------------------------------------------------- #
# Step 5 — redelivery ONLY after the wake is released (at-least-once)          #
# --------------------------------------------------------------------------- #

def test_redelivery_after_incarnation_bump_reaps_claim(tmp_path, monkeypatch):
    """[A104] A wake that never completes (failed before consumption) does NOT ack:
    its message returns to pending with backoff, and once the backoff elapses the
    next pass REDELIVERS the same task in a fresh wake (at-least-once)."""
    db, o = _env(tmp_path, monkeypatch)
    fid = _h3_case(db, round_cap=5)
    seed_finished_child(db, fid, "t2", requester="sess-1")

    assert _cont(o, db, fid) == 1
    _pass(db, o)
    (wake1,) = _wakes(db)
    tok = _run(db, wake1["id"])
    # the wake dies BEFORE consumption (e.g. its carrier was lost)
    assert db.complete_turn(wake1["id"], tok, {"success": False, "output": ""}, status="failed")
    (m,) = db.pending_for("sess-1", case_id=fid).messages
    assert m.state == "pending" and m.attempts == 1
    assert _cont(o, db, fid) == 0              # backoff holds the redelivery

    with db._write() as conn:
        conn.execute("UPDATE agent_inbox SET next_attempt_at = '1970-01-01T00:00:00+00:00' "
                     "WHERE state = 'pending'")
    assert _cont(o, db, fid) == 1
    wakes = _wakes(db)
    assert len(wakes) == 2 and wakes[1]["id"] != wake1["id"]
    assert "t2" in wakes[1]["prompt"]
    assert db.pending_for("sess-1", case_id=fid).carried_by(wakes[1]["id"])[0].attempts == 2


# --------------------------------------------------------------------------- #
# Step 6 — round cap → escalation instead of scheduling                       #
# --------------------------------------------------------------------------- #

def test_round_cap_exhaustion_interrupts_and_escalates(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    o.notifier = _FakeNotifier()
    fid = _h3_case(db, round_cap=2)

    # drive two full consumed rounds (generations 1 and 2)
    for tid in ("t1", "t2"):
        seed_finished_child(db, fid, tid, requester="sess-1")
        assert len(_drive(db, o)) == 1
    assert db.inbox_rounds_used(fid) == 2  # cap is 2

    # a THIRD delivery would be generation 3 > cap 2 → interrupt, no wake
    seed_finished_child(db, fid, "t3", requester="sess-1")
    assert _cont(o, db, fid) == 0

    interrupts = _events(db, fid, "flow.interrupted")
    assert len(interrupts) == 1
    assert '"round_cap_exhausted"' in interrupts[0]["payload_json"]
    assert len(_wakes(db)) == 2                # no third wake
    assert "case_continuation_interrupted" in o.events
    assert o.notifier.errors                   # operator escalation fired

    # idempotent: another pass does NOT emit a second interrupt
    assert _cont(o, db, fid) == 0
    assert len(_events(db, fid, "flow.interrupted")) == 1
    assert len(o.notifier.errors) == 1


# --------------------------------------------------------------------------- #
# ANY condition — edge-triggered, repeating                                    #
# --------------------------------------------------------------------------- #

def test_any_condition_repeats_on_each_new_completion(tmp_path, monkeypatch):
    db, o = _env(tmp_path, monkeypatch)
    fid = _h3_case(db, round_cap=9)
    seed_child(db, fid, "t1", requester="sess-1", token="tok-t1")
    seed_child(db, fid, "t2", requester="sess-1", token="tok-t2")
    db.arm_wait_group(fid, "g1", "ANY", ["t1", "t2"])

    # first completion → one wake presenting just t1 (ANY holds nothing)
    finish_child(db, "t1", token="tok-t1")
    assert len(_drive(db, o)) == 1
    (w1,) = _wakes(db)
    assert "t1" in w1["prompt"] and "t2" not in w1["prompt"]

    # t2 is still an outstanding request; its later completion wakes again
    assert db.pending_for("sess-1", case_id=fid).outstanding_task_ids == ["t2"]
    finish_child(db, "t2", token="tok-t2")
    assert _cont(o, db, fid) == 1
    w2 = _wakes(db)[1]
    assert "t2" in w2["prompt"] and "t1" not in w2["prompt"]


# --------------------------------------------------------------------------- #
# [A53] A killed (blocked) Case is NOT auto-resumed by the Wake-Dispatcher     #
# --------------------------------------------------------------------------- #

def test_blocked_case_is_not_continued(tmp_path, monkeypatch):
    _on(monkeypatch)
    db = _db(tmp_path)
    fid = _open_case(db, round_cap=2)
    seed_finished_child(db, fid, "t1", requester="mgr-sess")
    seed_finished_child(db, fid, "t2", requester="mgr-sess")
    # operator kill → blocked; the ready messages must NOT re-drive it
    db.update_flow_run(fid, status="blocked")

    orch = _FakeOrch(_FakeStore(_FakeSession("mgr-sess")))
    assert _continue(orch, db, fid) == 0
    assert orch.deliveries == []
    # held, not consumed and not killed: an unblock delivers them
    assert _msg_states(db) == {"t1": "pending", "t2": "pending"}


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
