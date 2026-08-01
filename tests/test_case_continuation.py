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


def _finished(db: MeshDB, case_id: str, task_id: str, outcome: str = "success") -> None:
    db.append_flow_event(
        case_id, "task.finished", "worker",
        entity_type="task", entity_id=task_id,
        payload={"outcome": outcome},
    )


def _events(db: MeshDB, case_id: str, event_type: str) -> list:
    return [e for e in db.list_flow_events(case_id) if e["event_type"] == event_type]


class _FakeSession:
    def __init__(self, sid, status=SessionStatus.IDLE, backend="claude", repo="/repo"):
        self.session_id = sid
        self.status = status
        self.backend = backend
        self.repo_path = repo


class _FakeStore:
    def __init__(self, *sessions):
        self._s = {s.session_id: s for s in sessions}

    def get(self, sid):
        return self._s.get(sid)


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
        self.notifier = _FakeNotifier()
        self.active_tasks = {}
        self.running = True
        self.deliveries = []
        self.emitted = []
        self.finalized = []

    def _emit_event(self, name, _a, payload):
        self.emitted.append((name, payload))

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
