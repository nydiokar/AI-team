"""
Quota pause → restore → resume for Manager Cases.

The defect this covers: a Manager turn killed by the account's quota window used
to leave NO durable trace, and the harness had exactly ONE resume trigger — a
satisfied wait-group. So a quota-killed Case either stalled silently forever (no
workers in flight) or came back at an unrelated random moment (whenever some
worker happened to finish), and because there was no operator-triggered
continuation either, an operator who started their own session on the Case ended
up with TWO Managers once the engine got round to it.

What is asserted here:
  * a quota death is classified honestly (session stays reusable, turn stays
    failed — a quota pause is not a success and not salvage);
  * it PAUSES the Case durably on the ledger;
  * the pause is read back off telemetry, not a timer;
  * restore PROPOSES (approval + estimate) rather than silently spending;
  * every entry point funnels through ONE leased resume, so two of them cannot
    produce two Managers;
  * both resume modes stay on the SAME Case (never a fork).

Drives the GENUINE ``TaskOrchestrator`` methods against a real ``MeshDB`` with a
duck-typed ``self`` — no paid CLI, no live backend, no network.
"""

import asyncio
import socket
from datetime import datetime, timedelta, timezone

import pytest

from src.control import db as db_mod
from src.control.db import (
    CONTINUATION_MACHINE_SENTINEL,
    MeshDB,
    QUOTA_RESUME_ACTION,
    quota_resume_task_id,
)
from src.core import SessionStatus
from src.core.interfaces import TaskResult
from src.orchestrator import (
    CASE_RESUME_APPROVAL_ACTION,
    TaskOrchestrator,
    _parse_iso_utc,
    _session_status_after_result,
    is_quota_pause_result,
)


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _flags(monkeypatch, *, resume="1", auto="0", ceiling=None) -> None:
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("MANAGER_ROLE_ENABLED", "1")
    monkeypatch.setenv("CASE_QUOTA_RESUME_ENABLED", resume)
    monkeypatch.setenv("CASE_QUOTA_RESUME_AUTO", auto)
    if ceiling is not None:
        monkeypatch.setenv("CASE_QUOTA_RESUME_AUTO_MAX_USD", str(ceiling))


class _FakeQuotaStore:
    """Stands in for QuotaWindowStore: only ``status()['latest_snapshots']`` is
    read by the harness, and only the fields the provider actually reports."""

    def __init__(self, snapshots):
        self._snapshots = snapshots

    def status(self, **_kw):
        return {"latest_snapshots": self._snapshots}


class _FakeCoordinator:
    def __init__(self, snapshots):
        self.store = _FakeQuotaStore(snapshots)


def _spent_snapshot(reset_in_minutes=60, provider="claude"):
    return [{
        "provider": provider, "bucket_id": "five_hour", "used_percent": 100.0,
        "limit_reached": 1, "observed_at": _iso(_now()),
        "reset_at": _iso(_now() + timedelta(minutes=reset_in_minutes)),
        "telemetry_quality": "authoritative",
    }]


def _restored_snapshot(provider="claude"):
    return [{
        "provider": provider, "bucket_id": "five_hour", "used_percent": 12.0,
        "limit_reached": 0, "observed_at": _iso(_now()),
        "reset_at": _iso(_now() + timedelta(hours=4)),
        "telemetry_quality": "authoritative",
    }]


class _FakeSession:
    def __init__(self, sid, status=SessionStatus.AWAITING_INPUT, backend="claude",
                 repo="/repo", machine_id="__local__", model="opus"):
        self.session_id = sid
        self.status = status
        self.backend = backend
        self.repo_path = repo
        self.machine_id = machine_id
        self.model = model
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


class _FakeSessionService:
    def __init__(self, store):
        self.store = store
        self.created = []

    def create_session(self, *, backend, repo_path, node_id, origin, bind_chat):
        sid = f"respawned-mgr-{len(self.created) + 1}"
        sess = _FakeSession(sid, backend=backend, repo=repo_path, machine_id=node_id)
        self.store.add(sess)
        self.created.append(sid)
        return _CreateResult(sess)


class _FakeNotifier:
    def __init__(self):
        self.errors = []

    async def notify_error(self, message, **kw):
        self.errors.append(message)


class _Orch:
    """Duck-typed ``self`` that delegates every method under test to the REAL
    ``TaskOrchestrator`` implementation."""

    _FLOW_RUN_META_KEY = TaskOrchestrator._FLOW_RUN_META_KEY
    _CASE_ID_META_KEY = TaskOrchestrator._CASE_ID_META_KEY

    def __init__(self, store, snapshots=None):
        self.session_store = store
        self.session_service = _FakeSessionService(store)
        self.notifier = _FakeNotifier()
        self.approval_service = None
        self.quota_coordinator = _FakeCoordinator(snapshots) if snapshots is not None else None
        self.deliveries = []
        self.emitted = []
        self.affiliations = []
        self.active_tasks = {}
        self.running = True

    # -- real implementations ------------------------------------------------
    def _emit_event(self, name, _t=None, payload=None):
        self.emitted.append((name, payload or {}))

    def _harness_flow_drive_enabled(self):
        return TaskOrchestrator._harness_flow_drive_enabled()

    def _manager_role_enabled(self):
        return TaskOrchestrator._manager_role_enabled(self)

    def _short_failure_reason(self, result):
        return TaskOrchestrator._short_failure_reason(result)

    def quota_window_state(self, provider="claude"):
        return TaskOrchestrator.quota_window_state(self, provider)

    def estimate_case_resume_cost(self, session_id):
        return TaskOrchestrator.estimate_case_resume_cost(self, session_id)

    def _recommended_resume_mode(self, session_id, *, paused_at=None):
        return TaskOrchestrator._recommended_resume_mode(
            self, session_id, paused_at=paused_at,
        )

    def _ensure_approval_service(self, db):
        return TaskOrchestrator._ensure_approval_service(self, db)

    async def _on_case_approval_resolved(self, row):
        return await TaskOrchestrator._on_case_approval_resolved(self, row)

    def _find_case_pause_approval(self, case_id, paused_task_id):
        return TaskOrchestrator._find_case_pause_approval(self, case_id, paused_task_id)

    def _extract_rate_limit_info(self, result):
        return TaskOrchestrator._extract_rate_limit_info(result)

    def _rate_limit_reset_iso(self, result):
        return TaskOrchestrator._rate_limit_reset_iso(self, result)

    def _record_quota_pause(self, task, result):
        return TaskOrchestrator._record_quota_pause(self, task, result)

    def _render_quota_resume_turn(self, case_id, row):
        return TaskOrchestrator._render_quota_resume_turn(self, case_id, row)

    def _render_respawn_turn(self, case_id, objective):
        return TaskOrchestrator._render_respawn_turn(self, case_id, objective)

    def _render_wake_turn(self, case_id, presented):
        return TaskOrchestrator._render_wake_turn(self, case_id, presented)

    def _set_session_case_affiliation(self, sid, case_id, role=None):
        sess = self.session_store.get(sid)
        if sess is not None:
            sess.current_case_id = case_id
            sess.case_role = role
        self.affiliations.append((sid, case_id, role))

    async def submit_instruction(self, description, session_id=None, cwd=None, source=""):
        self.deliveries.append(
            {"description": description, "session_id": session_id, "source": source}
        )
        return f"turn-{len(self.deliveries)}"

    async def _finalize_continuation(self, *a, **k):
        # The post-wake consumption recorder — out of scope here (covered by
        # test_case_continuation); this fake only needs the wake to be delivered.
        return None

    async def _escalate_headless_case(self, db, case_id, session_id):
        return await TaskOrchestrator._escalate_headless_case(self, db, case_id, session_id)

    async def _escalate_case_continuation_cap(self, case_id, cap, generation):
        return await TaskOrchestrator._escalate_case_continuation_cap(
            self, case_id, cap, generation,
        )

    async def _do_respawn_manager_for_case(self, db, case_id, generation, dead_sid):
        return await TaskOrchestrator._do_respawn_manager_for_case(
            self, db, case_id, generation, dead_sid,
        )

    async def _handle_dead_manager_session(self, db, case_id, generation, dead_sid):
        return await TaskOrchestrator._handle_dead_manager_session(
            self, db, case_id, generation, dead_sid,
        )

    async def _handle_quota_paused_case(self, db, case_id):
        return await TaskOrchestrator._handle_quota_paused_case(self, db, case_id)

    async def resume_case(self, case_id, *, mode=None, actor="operator", paused_task_id=None):
        return await TaskOrchestrator.resume_case(
            self, case_id, mode=mode, actor=actor, paused_task_id=paused_task_id,
        )

    def case_resume_state(self, case_id):
        return TaskOrchestrator.case_resume_state(self, case_id)


class _Task:
    def __init__(self, task_id, case_id, session_id):
        self.id = task_id
        self.metadata = {
            TaskOrchestrator._CASE_ID_META_KEY: case_id,
            "session_id": session_id,
        }


def _quota_result(task_id="t1", error_class="usage_limit") -> TaskResult:
    return TaskResult(
        task_id=task_id,
        success=False,
        output="",
        errors=["You've hit your session limit · resets 4:30pm"],
        files_modified=[],
        execution_time=12.0,
        timestamp=_iso(_now()),
        raw_stdout='{"type":"result","is_error":true,"api_error_status":429}',
        raw_stderr="",
        return_code=1,
        error_class=error_class,
    )


def _mk_db(tmp_path, monkeypatch) -> MeshDB:
    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    db.upsert_node(socket.gethostname(), "", 9001, ["claude"], 2)
    return db


def _case(db: MeshDB, manager_sid: str, objective: str = "ship feature X") -> str:
    return db.open_case(objective, manager_sid, role="manager",
                        completion_criteria='{"round_cap": 5}')


def _pause(orch, db, case_id, sid, task_id="paused-turn"):
    orch._record_quota_pause(_Task(task_id, case_id, sid), _quota_result(task_id))


def _events(db, case_id, event_type):
    return [e for e in db.list_flow_events(case_id) if e["event_type"] == event_type]


# --------------------------------------------------------------------------- #
# 1. Classification honesty                                                    #
# --------------------------------------------------------------------------- #

def test_quota_pause_keeps_session_reusable_but_turn_still_failed():
    """The session must stay AWAITING_INPUT (nothing broke; the provider refused
    the turn) while the turn keeps reporting failed — no work was produced, so
    flipping success would lie to task status, telemetry and the operator."""
    result = _quota_result()
    assert is_quota_pause_result(result) is True
    assert _session_status_after_result(result) == SessionStatus.AWAITING_INPUT
    assert result.success is False
    assert result.error_class == "usage_limit"


def test_agent_text_about_rate_limits_is_not_a_quota_pause():
    """A genuine hard failure whose OUTPUT merely discusses rate limiting (the
    agent was writing rate-limit code) must not be laundered into a pause: the
    predicate keys on the structured error_class only."""
    result = TaskResult(
        task_id="t-text", success=False,
        output="I implemented the rate limit middleware, then the tool crashed.",
        errors=["boom"], files_modified=[], execution_time=1.0,
        timestamp=_iso(_now()), raw_stdout="", raw_stderr="", return_code=1,
        error_class="fatal",
    )
    assert is_quota_pause_result(result) is False
    assert _session_status_after_result(result) == SessionStatus.ERROR


def test_rate_limit_event_classifies_as_usage_limit_and_does_not_retry():
    """A rejected rate_limit_event IS the subscription window (it carries
    resetsAt), so it must land in the same class as a 429 — and a window that
    reopens in hours is not retry-eligible seconds later."""
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    result = TaskResult(
        task_id="t-rl", success=False, output="", errors=["limit"],
        files_modified=[], execution_time=1.0, timestamp=_iso(_now()),
        raw_stdout='{"type":"rate_limit_event","rate_limit_info":'
                   '{"status":"rejected","rateLimitType":"five_hour","resetsAt":1755450000}}',
        raw_stderr="", return_code=1,
    )
    assert TaskOrchestrator._classify_error(orch, result) == "usage_limit"
    assert TaskOrchestrator._get_retry_strategy(orch, "usage_limit")["max_retries"] == 0


# --------------------------------------------------------------------------- #
# 2. The pause is durable, Manager-only, and single                            #
# --------------------------------------------------------------------------- #

def test_manager_quota_death_pauses_the_case(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    store = _FakeStore(_FakeSession("mgr-1"))
    orch = _Orch(store, snapshots=_spent_snapshot())
    case_id = _case(db, "mgr-1")

    _pause(orch, db, case_id, "mgr-1")

    pause = db.case_quota_pause(case_id)
    assert pause is not None
    assert pause["session_id"] == "mgr-1"
    assert pause["paused_task_id"] == "paused-turn"
    assert pause["reset_at"] is not None  # telemetry travels with the record
    assert len(_events(db, case_id, "flow.quota_paused")) == 1
    assert any(name == "case_quota_paused" for name, _ in orch.emitted)


def test_second_quota_death_does_not_open_a_second_pause(tmp_path, monkeypatch):
    """One open pause per Case: the resume lease is keyed on the FIRST paused
    task, so a second refused turn must not shift it."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_spent_snapshot())
    case_id = _case(db, "mgr-1")

    _pause(orch, db, case_id, "mgr-1", task_id="turn-a")
    _pause(orch, db, case_id, "mgr-1", task_id="turn-b")

    assert len(_events(db, case_id, "flow.quota_paused")) == 1
    assert db.case_quota_pause(case_id)["paused_task_id"] == "turn-a"


def test_worker_quota_death_does_not_pause_the_case(tmp_path, monkeypatch):
    """A worker hitting quota is the worker's retry problem — it must not
    propose bringing a Manager back."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_spent_snapshot())
    case_id = _case(db, "mgr-1")

    _pause(orch, db, case_id, "worker-9")

    assert db.case_quota_pause(case_id) is None


def test_flag_off_records_nothing(tmp_path, monkeypatch):
    _flags(monkeypatch, resume="0")
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_spent_snapshot())
    case_id = _case(db, "mgr-1")

    _pause(orch, db, case_id, "mgr-1")

    assert db.case_quota_pause(case_id) is None
    assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is False


def test_resume_closes_the_pause(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")
    assert db.case_quota_pause(case_id) is not None

    assert asyncio.run(orch.resume_case(case_id, mode="in_place"))["ok"] is True
    assert db.case_quota_pause(case_id) is None


# --------------------------------------------------------------------------- #
# 3. Restore is a telemetry fact, not a timer                                  #
# --------------------------------------------------------------------------- #

def test_quota_window_state_reads_the_provider_limit_bit(tmp_path, monkeypatch):
    orch = _Orch(_FakeStore(), snapshots=_spent_snapshot())
    state = orch.quota_window_state("claude")
    assert state["exhausted"] is True
    assert state["evidence"] == "limit_reached"
    assert state["reset_at"] is not None


def test_quota_window_state_restored_when_under_limit():
    orch = _Orch(_FakeStore(), snapshots=_restored_snapshot())
    state = orch.quota_window_state("claude")
    assert state["exhausted"] is False
    assert state["evidence"] == "below_limit"


def test_spent_reading_past_its_own_reset_is_not_exhausted():
    """The observer polls on an interval; a spent reading whose reset time has
    already passed means the window rolled over — treating it as still spent
    would strand the Case until the next poll."""
    orch = _Orch(_FakeStore(), snapshots=_spent_snapshot(reset_in_minutes=-5))
    state = orch.quota_window_state("claude")
    assert state["exhausted"] is False
    assert state["evidence"] == "reset_elapsed"


def test_no_quota_instrument_is_not_exhausted():
    """Fail OPEN: waiting forever on an instrument that may never report is worse
    than proposing a resume the operator can decline."""
    orch = _Orch(_FakeStore(), snapshots=None)
    state = orch.quota_window_state("claude")
    assert state["exhausted"] is False
    assert state["evidence"] == "no_telemetry"


# --------------------------------------------------------------------------- #
# 4. Restore proposes; it does not silently spend                              #
# --------------------------------------------------------------------------- #

def test_still_exhausted_owns_the_tick_without_asking(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_spent_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")

    # Owns the tick (True ⇒ no wake attempt) but asks nothing yet.
    assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is True
    assert orch.approval_service is None or not orch.approval_service.list(limit=50)
    assert orch.deliveries == []


def test_restore_proposes_a_resume_with_an_estimate(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")

    assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is True

    pending = [a for a in orch.approval_service.list(status="pending", limit=50)
               if a["action"] == CASE_RESUME_APPROVAL_ACTION]
    assert len(pending) == 1
    assert any(name == "case_resume_proposed" for name, _ in orch.emitted)
    # Proposing must not spend: no turn delivered, no session spawned.
    assert orch.deliveries == []
    assert orch.session_service.created == []


def test_proposal_is_not_repeated_every_tick(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")

    for _ in range(3):
        assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is True

    assert len([a for a in orch.approval_service.list(limit=50)
                if a["action"] == CASE_RESUME_APPROVAL_ACTION]) == 1


def test_rejected_proposal_closes_the_pause_instead_of_stalling_forever(tmp_path, monkeypatch):
    """A declined resume must not leave the Case permanently unable to continue
    — it returns to ordinary behaviour, and is never re-proposed for this pause."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")
    asyncio.run(orch._handle_quota_paused_case(db, case_id))
    approval_id = orch.approval_service.list(status="pending", limit=50)[0]["id"]

    asyncio.run(orch.approval_service.resolve(approval_id, "rejected", resolved_by="operator"))

    assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is False
    assert db.case_quota_pause(case_id) is None
    assert len(_events(db, case_id, "flow.quota_pause_declined")) == 1
    assert orch.deliveries == []


def test_approving_the_proposal_runs_the_resume(tmp_path, monkeypatch):
    """The operator's decision is what RUNS the gated action — the whole point of
    the durable approval queue."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")
    asyncio.run(orch._handle_quota_paused_case(db, case_id))
    approval_id = orch.approval_service.list(status="pending", limit=50)[0]["id"]

    asyncio.run(orch.approval_service.resolve(approval_id, "approved", resolved_by="operator"))

    assert len(orch.deliveries) == 1
    assert orch.deliveries[0]["session_id"] == "mgr-1"
    assert orch.deliveries[0]["source"] == "manager_quota_resume"
    assert db.case_quota_pause(case_id) is None


def test_auto_resume_fires_without_asking_when_enabled(tmp_path, monkeypatch):
    _flags(monkeypatch, auto="1")
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")

    assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is True

    assert len(orch.deliveries) == 1
    assert not [a for a in orch.approval_service.list(limit=50)
                if a["action"] == CASE_RESUME_APPROVAL_ACTION]


def test_auto_resume_holds_back_above_the_usd_ceiling(tmp_path, monkeypatch):
    """AUTO is a convenience, not a blank cheque: an estimate above the ceiling
    still goes to the operator."""
    _flags(monkeypatch, auto="1", ceiling="0.01")
    db = _mk_db(tmp_path, monkeypatch)
    store = _FakeStore(_FakeSession("mgr-1"))
    orch = _Orch(store, snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")
    # A fat session: a 250k-token cache write costs far more than $0.01.
    _record_cache_write(db, "mgr-1", 250_000)

    assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is True

    assert orch.deliveries == []
    assert len([a for a in orch.approval_service.list(limit=50)
                if a["action"] == CASE_RESUME_APPROVAL_ACTION]) == 1


# --------------------------------------------------------------------------- #
# 5. One leased resume — two entry points cannot make two Managers             #
# --------------------------------------------------------------------------- #

def test_manual_resume_is_a_continuation_not_a_fork(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")
    open_before = {c["flow_run_id"] for c in db.list_open_cases()}

    out = asyncio.run(orch.resume_case(case_id, mode="in_place", actor="operator"))

    assert out["ok"] is True and out["mode"] == "in_place"
    assert out["session_id"] == "mgr-1"                      # SAME session
    assert {c["flow_run_id"] for c in db.list_open_cases()} == open_before  # NO new Case
    assert orch.session_service.created == []                 # nothing spawned
    assert len(_events(db, case_id, "flow.quota_resumed")) == 1


def test_second_resume_of_the_same_pause_loses_the_lease(tmp_path, monkeypatch):
    """The operator pressing Resume and the engine auto-resuming are the SAME
    leased row — the observed 'two sessions on one Case' cannot happen."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")

    first = asyncio.run(orch.resume_case(case_id, mode="in_place"))
    second = asyncio.run(orch.resume_case(
        case_id, mode="in_place", paused_task_id="paused-turn",
    ))

    assert first["ok"] is True
    assert second["ok"] is False and second["reason"] == "resume_in_flight"
    assert len(orch.deliveries) == 1
    row = db.get_task(quota_resume_task_id(case_id, "paused-turn"))
    assert row is not None and row["action"] == QUOTA_RESUME_ACTION
    assert row["machine_id"] == CONTINUATION_MACHINE_SENTINEL


def test_concurrent_resumes_of_one_pause_deliver_exactly_one_turn(tmp_path, monkeypatch):
    """The scenario the operator actually hit: they trigger a resume while the
    engine is about to auto-resume the same pause. Both carry the pause id (the
    approval payload and the API path both do), so both compute the same lease."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")

    async def _race():
        return await asyncio.gather(
            orch.resume_case(case_id, mode="in_place", paused_task_id="paused-turn"),
            orch.resume_case(case_id, mode="in_place", paused_task_id="paused-turn"),
        )

    a, b = asyncio.run(_race())
    assert sorted([a["ok"], b["ok"]]) == [False, True]
    assert len(orch.deliveries) == 1


def test_manual_resume_without_a_pause_is_leased_per_poke(tmp_path, monkeypatch):
    """With no open pause the button is an ordinary operator poke: it must stay
    repeatable (a Case can need more than one nudge over its life) while two
    callers that observe the SAME state still collapse to one turn — the lease
    key is derived from the resumes already recorded, not from a constant."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")

    # A concurrent caller already owns the lease for this poke.
    lease_id = quota_resume_task_id(case_id, "manual:0")
    db.enqueue_task(lease_id, session_id=None, machine_id=CONTINUATION_MACHINE_SENTINEL,
                    backend="claude", action=QUOTA_RESUME_ACTION, payload={})
    assert db.claim_task(lease_id, socket.gethostname()) is True

    blocked = asyncio.run(orch.resume_case(case_id, mode="in_place"))
    assert blocked["ok"] is False and blocked["reason"] == "resume_in_flight"
    assert orch.deliveries == []

    # A later, deliberate poke (a different lease key) is allowed.
    db.complete_task(lease_id, result={})
    db.append_flow_event(case_id, "flow.quota_resumed", "operator", payload={"mode": "in_place"})
    assert asyncio.run(orch.resume_case(case_id, mode="in_place"))["ok"] is True
    assert len(orch.deliveries) == 1


def test_busy_manager_is_not_resumed(tmp_path, monkeypatch):
    """A Manager mid-turn is not stuck; resuming it would queue a redundant
    (expensive) turn."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1", status=SessionStatus.BUSY)),
                 snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")

    out = asyncio.run(orch.resume_case(case_id, mode="in_place"))

    assert out["ok"] is False and out["reason"] == "manager_busy"
    assert out["session_id"] is None
    assert orch.deliveries == []


def test_dead_session_downgrades_in_place_to_a_fresh_manager(tmp_path, monkeypatch):
    """Asking to resume 'in place' a session that no longer exists must not
    silently do nothing — the whole defect being fixed. It reconstructs the Case
    from the ledger instead, on the SAME flow_run_id."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(), snapshots=_restored_snapshot())  # mgr-1 is gone
    case_id = _case(db, "mgr-1")
    _pause(orch, db, case_id, "mgr-1")
    open_before = {c["flow_run_id"] for c in db.list_open_cases()}

    out = asyncio.run(orch.resume_case(case_id, mode="in_place"))

    assert out["ok"] is True and out["mode"] == "fresh_manager"
    assert orch.session_service.created == ["respawned-mgr-1"]
    assert db.case_manager_session_id(case_id) == "respawned-mgr-1"
    assert {c["flow_run_id"] for c in db.list_open_cases()} == open_before
    assert orch.deliveries[0]["source"] == "manager_respawn"


def test_resume_refuses_a_terminal_case(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")
    db.update_flow_run(case_id, status="closed")

    out = asyncio.run(orch.resume_case(case_id))

    assert out["ok"] is False and out["reason"] == "case_terminal"


def test_resume_refuses_an_unknown_case(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(), snapshots=_restored_snapshot())

    out = asyncio.run(orch.resume_case("no-such-case"))

    assert out["ok"] is False and out["reason"] == "case_not_found"


# --------------------------------------------------------------------------- #
# 6. The paused Case does not take the ordinary wake path                      #
# --------------------------------------------------------------------------- #

def test_paused_case_is_not_woken_even_when_a_wait_group_satisfies(tmp_path, monkeypatch):
    """The old behaviour: the ONLY way back was a satisfied wait-group, which is
    why resumption looked random. While the window is spent, a wake is just
    another refused turn."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_spent_snapshot())
    case_id = _case(db, "mgr-1")
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    db.append_flow_event(case_id, "task.finished", "worker",
                         entity_type="task", entity_id="t1",
                         payload={"outcome": "success"})
    _pause(orch, db, case_id, "mgr-1")

    delivered = asyncio.run(TaskOrchestrator._continue_case_once(orch, db, case_id))

    assert delivered == 0
    assert orch.deliveries == []


def test_unpaused_case_still_wakes_normally(tmp_path, monkeypatch):
    """Regression guard: with no pause on the ledger the quota branch is inert
    and the pre-existing continuation behaviour is unchanged."""
    _flags(monkeypatch)
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_spent_snapshot())
    case_id = _case(db, "mgr-1")
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    db.append_flow_event(case_id, "task.finished", "worker",
                         entity_type="task", entity_id="t1",
                         payload={"outcome": "success"})

    delivered = asyncio.run(TaskOrchestrator._continue_case_once(orch, db, case_id))

    assert delivered == 1
    assert orch.deliveries[0]["source"] == "manager_continuation"


# --------------------------------------------------------------------------- #
# 7. The estimate is honest                                                    #
# --------------------------------------------------------------------------- #

def _record_cache_write(db: MeshDB, session_id: str, cache_creation: int) -> None:
    """Insert the minimal turn + request telemetry the estimate reads."""
    stamp = _iso(_now())
    with db._write() as conn:
        conn.execute(
            "INSERT INTO llm_turns (turn_id, session_id, task_id, observed_models, "
            "final_status, timeout_status, metrics_json, coverage_json, data_quality_json, "
            "projection_version, created_at, updated_at, ended_at) "
            "VALUES (?, ?, ?, '[\"opus\"]', 'completed', 'none', '{}', '{}', '[]', 1, ?, ?, ?)",
            (f"turn-{session_id}", session_id, f"task-{session_id}", stamp, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO llm_invocations (invocation_id, turn_id, attempt, spawn_reason, "
            "action, node_id, backend, status, started_at, ended_at) "
            "VALUES (?, ?, 1, 'initial', 'session_turn', '__local__', 'claude', 'completed', ?, ?)",
            (f"inv-{session_id}", f"turn-{session_id}", stamp, stamp),
        )
        conn.execute(
            "INSERT INTO llm_model_requests (model_request_id, invocation_id, turn_id, sequence, "
            "model, work_category, input_token_semantics, usage_granularity, usage_coverage, "
            "data_quality_json, input_tokens, output_tokens, cache_read_tokens, "
            "cache_creation_tokens, is_duplicate) "
            "VALUES (?, ?, ?, 0, 'opus', 'primary', 'excludes_cache', 'invocation_total', "
            "'aggregate_only', '[]', 10, 10, 100, ?, 0)",
            (f"req-{session_id}", f"inv-{session_id}", f"turn-{session_id}", cache_creation),
        )


def test_estimate_uses_the_observed_cache_write(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    _record_cache_write(db, "mgr-1", 250_000)

    est = orch.estimate_case_resume_cost("mgr-1")

    assert est["known"] is True
    assert est["cache_creation_tokens"] == 250_000
    assert est["usd"] > 0
    assert est["basis"] == "max_recent_turn_cache_creation"


def test_estimate_is_honest_when_unmeasurable(tmp_path, monkeypatch):
    """No fabricated number when there is no telemetry to base one on."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())

    est = orch.estimate_case_resume_cost("mgr-1")

    assert est["known"] is False
    assert est["usd"] is None
    assert est["reason"] == "no_recorded_turn"


# --------------------------------------------------------------------------- #
# 8. The operator-facing state read                                            #
# --------------------------------------------------------------------------- #

def test_resume_state_reports_pause_quota_estimate_and_decision(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_spent_snapshot())
    case_id = _case(db, "mgr-1")
    _record_cache_write(db, "mgr-1", 120_000)
    _pause(orch, db, case_id, "mgr-1")

    state = orch.case_resume_state(case_id)

    assert state["paused"] is True
    assert state["manager_session_id"] == "mgr-1"
    assert state["manager_session_status"] == SessionStatus.AWAITING_INPUT.value
    assert state["recommended_mode"] == "in_place"
    assert state["quota"]["exhausted"] is True
    assert state["estimate"]["known"] is True
    assert state["pending_approval"] is None


def test_resume_state_of_an_unpaused_case(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")

    state = orch.case_resume_state(case_id)

    assert state["paused"] is False and state["pause"] is None
    assert state["enabled"] is True and state["auto"] is False


# --------------------------------------------------------------------------- #
# 9. No quota observer — the refusal's own reset time governs the wait         #
# --------------------------------------------------------------------------- #

def _rate_limited_result(reset_epoch: int) -> TaskResult:
    """The live shape: the provider attaches its reset instant to the refusal."""
    return TaskResult(
        task_id="paused-turn", success=False, output="", errors=["limit"],
        files_modified=[], execution_time=5.0, timestamp=_iso(_now()),
        raw_stdout='{"type":"rate_limit_event","rate_limit_info":'
                   f'{{"status":"rejected","rateLimitType":"five_hour","resetsAt":{reset_epoch}}}}}',
        raw_stderr="", return_code=1, error_class="usage_limit",
    )


def test_pause_records_the_providers_own_reset_when_telemetry_is_absent(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=None)  # no observer
    case_id = _case(db, "mgr-1")
    reset = int((_now() + timedelta(hours=2)).timestamp())

    orch._record_quota_pause(_Task("paused-turn", case_id, "mgr-1"), _rate_limited_result(reset))

    pause = db.case_quota_pause(case_id)
    assert pause is not None and pause["reset_at"] is not None
    assert _parse_iso_utc(pause["reset_at"]) > _now()


def test_without_telemetry_the_recorded_reset_still_holds_the_resume_back(tmp_path, monkeypatch):
    """A missing quota observer must not mean 'propose immediately' — that would
    just buy another refused turn."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=None)
    case_id = _case(db, "mgr-1")
    orch._record_quota_pause(
        _Task("paused-turn", case_id, "mgr-1"),
        _rate_limited_result(int((_now() + timedelta(hours=2)).timestamp())),
    )

    assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is True
    assert orch.approval_service is None or not orch.approval_service.list(limit=50)


def test_without_telemetry_an_elapsed_reset_proposes(tmp_path, monkeypatch):
    """...and once that instant passes, the Case is proposed — a dead instrument
    must never park a Case forever."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=None)
    case_id = _case(db, "mgr-1")
    orch._record_quota_pause(
        _Task("paused-turn", case_id, "mgr-1"),
        _rate_limited_result(int((_now() - timedelta(minutes=5)).timestamp())),
    )

    assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is True
    assert len([a for a in orch.approval_service.list(limit=50)
                if a["action"] == CASE_RESUME_APPROVAL_ACTION]) == 1


# --------------------------------------------------------------------------- #
# 10. Burst rate-limit vs spent subscription window are NOT the same thing     #
# --------------------------------------------------------------------------- #

def _text_failure(text: str) -> TaskResult:
    return TaskResult(
        task_id="t-text-class", success=False, output="", errors=[text],
        files_modified=[], execution_time=1.0, timestamp=_iso(_now()),
        raw_stdout="", raw_stderr=text, return_code=1,
    )


def test_generic_burst_wording_stays_retry_eligible():
    """"Rate limit exceeded. Please retry later." is the classic transient the
    retry policy exists for — it is NOT evidence that the account's five-hour
    window is spent, and collapsing the two would silently stop retrying real
    transients."""
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    ec = TaskOrchestrator._classify_error(orch, _text_failure("Rate limit exceeded. Please retry later."))
    assert ec == "rate_limit"
    assert TaskOrchestrator._get_retry_strategy(orch, ec)["max_retries"] >= 1


def test_subscription_window_wording_is_a_quota_pause():
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    for text in (
        "You've hit your session limit · resets 4:30pm",
        "Claude usage limit reached",
        "You've hit your limit",
    ):
        ec = TaskOrchestrator._classify_error(orch, _text_failure(text))
        assert ec == "usage_limit", text
        assert TaskOrchestrator._get_retry_strategy(orch, ec)["max_retries"] == 0


def test_both_classes_pause_a_case():
    """Whichever class a terminal quota failure lands in, the Case must pause —
    a burst that never clears is still the Case sitting on a spent provider."""
    for ec in ("usage_limit", "rate_limit"):
        assert is_quota_pause_result(_quota_result(error_class=ec)) is True


# --------------------------------------------------------------------------- #
# 11. STALE telemetry must not fake a restore (the live defect)                #
# --------------------------------------------------------------------------- #

def _stale_healthy_snapshot(age_hours: float = 4.0):
    """The live shape on this host: the observer legitimately sleeps for hours
    (next_observe_delay_sec backs off to the next known reset), so its last
    reading can say "0% used" long after the window was actually spent."""
    return [{
        "provider": "claude", "bucket_id": "five_hour", "used_percent": 0.0,
        "limit_reached": 0,
        "observed_at": _iso(_now() - timedelta(hours=age_hours)),
        "reset_at": _iso(_now() - timedelta(hours=age_hours) + timedelta(hours=2)),
        "telemetry_quality": "authoritative",
    }]


def test_stale_below_limit_telemetry_does_not_propose_before_the_real_reset(tmp_path, monkeypatch):
    """THE defect this fixes: a healthy-but-hours-old snapshot is neither
    `exhausted` nor `no_telemetry`, so the pause used to be proposed for resume
    on the very next 30s tick — hours before quota returned. The reset instant
    the provider attached to its own refusal governs instead."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_stale_healthy_snapshot())
    case_id = _case(db, "mgr-1")
    orch._record_quota_pause(
        _Task("paused-turn", case_id, "mgr-1"),
        _rate_limited_result(int((_now() + timedelta(hours=3)).timestamp())),
    )

    assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is True
    assert orch.approval_service is None or not orch.approval_service.list(limit=50)


def test_telemetry_observed_after_the_pause_can_release_it_early(tmp_path, monkeypatch):
    """Corrigible, not a blind clock: if the observer reads a HEALTHY window
    after the pause was recorded (Anthropic re-anchored or reset the limits),
    that overrules the recorded boundary and the resume is proposed."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=None)
    case_id = _case(db, "mgr-1")
    orch._record_quota_pause(
        _Task("paused-turn", case_id, "mgr-1"),
        _rate_limited_result(int((_now() + timedelta(hours=3)).timestamp())),
    )
    orch.quota_coordinator = _FakeCoordinator([{
        "provider": "claude", "bucket_id": "five_hour", "used_percent": 4.0,
        "limit_reached": 0, "observed_at": _iso(_now() + timedelta(seconds=30)),
        "reset_at": _iso(_now() + timedelta(hours=5)),
        "telemetry_quality": "authoritative",
    }])

    assert asyncio.run(orch._handle_quota_paused_case(db, case_id)) is True
    assert len([a for a in orch.approval_service.list(limit=50)
                if a["action"] == CASE_RESUME_APPROVAL_ACTION]) == 1


def test_pause_ignores_a_healthy_windows_reset_at(tmp_path, monkeypatch):
    """A healthy reading carries a reset_at too ("12% used, resets in 4h"). That
    boundary never blocked anything, so it must not become the wait-until."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")), snapshots=_restored_snapshot())
    case_id = _case(db, "mgr-1")

    _pause(orch, db, case_id, "mgr-1")          # refusal carries no resetsAt

    assert db.case_quota_pause(case_id)["reset_at"] is None


# --------------------------------------------------------------------------- #
# 12. Resume mode is decided by the prompt cache, not by taste                 #
# --------------------------------------------------------------------------- #

def test_mode_is_in_place_while_the_cache_is_still_warm(tmp_path, monkeypatch):
    """Inside the provider's ~1h cache TTL a resume re-writes nothing, so the
    live session wins however fat it is."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    _record_cache_write(db, "mgr-1", 250_000)

    assert orch._recommended_resume_mode(
        "mgr-1", paused_at=_iso(_now() - timedelta(minutes=20)),
    ) == "in_place"


def test_mode_is_fresh_manager_for_a_cold_fat_session(tmp_path, monkeypatch):
    """Past the TTL, a 250k-token conversation is exactly the 200-300k rewrite
    this seam exists to avoid."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    _record_cache_write(db, "mgr-1", 250_000)

    assert orch._recommended_resume_mode(
        "mgr-1", paused_at=_iso(_now() - timedelta(hours=4)),
    ) == "fresh_manager"


def test_mode_is_in_place_for_a_cold_but_small_session(tmp_path, monkeypatch):
    """A cold rewrite of a small conversation is cheap, and the transcript is
    worth more than the tokens."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    _record_cache_write(db, "mgr-1", 20_000)

    assert orch._recommended_resume_mode(
        "mgr-1", paused_at=_iso(_now() - timedelta(hours=4)),
    ) == "in_place"


def test_mode_is_fresh_manager_when_the_session_size_is_unknown(tmp_path, monkeypatch):
    """No recorded turn ⇒ no measurement. Assume the expensive case: fresh_manager
    loses transcript detail, never money."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))

    assert orch._recommended_resume_mode(
        "mgr-1", paused_at=_iso(_now() - timedelta(hours=4)),
    ) == "fresh_manager"


def test_dead_session_is_always_fresh_manager(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1", status=SessionStatus.CLOSED)))
    _record_cache_write(db, "mgr-1", 1_000)

    assert orch._recommended_resume_mode(
        "mgr-1", paused_at=_iso(_now()),
    ) == "fresh_manager"
