"""
Transient provider 5xx (529 Overloaded) self-heal for Manager Cases.

The gap this covers: a Manager turn refused by an Anthropic server-side overload
(``api_error_status >= 500`` ⇒ ``error_class == upstream_error``) exhausts its
in-process burst retries within seconds. A multi-minute overload therefore went
terminal → session ERROR → the Case stalled with the ONLY resume trigger being a
wait-group satisfaction (exactly the pre-PR#97 quota hole). It never self-healed.

What is asserted here (flag ``TRANSIENT_PROVIDER_RESUME_ENABLED``, default OFF):
  * a transient death is classified honestly — session stays REUSABLE
    (AWAITING_INPUT) while the turn keeps reporting failed (no work produced);
  * OFF ⇒ byte-identical (session ERROR, no pause recorded, handler inert);
  * it PAUSES the Case durably on the ledger, Manager-only, one open pause;
  * the backoff escalates and is BOUNDED — after the schedule is spent the Case
    escalates (``flow.transient_pause_exhausted``) instead of looping forever;
  * once the backoff elapses the Wake-Dispatcher AUTO-retries the EXACT failed
    turn (verbatim), under a single-flight lease, free (no cost/approval);
  * a paused Case does not take the ordinary wake path while holding.

Drives the GENUINE ``TaskOrchestrator`` methods against a real ``MeshDB`` with a
duck-typed ``self`` — no paid CLI, no live backend, no network.
"""

import asyncio
import socket
from datetime import datetime, timedelta, timezone

import pytest

from src.control import db as db_mod
from src.control.db import (
    MeshDB,
    TRANSIENT_RESUME_ACTION,
    transient_resume_task_id,
)
from src.core import SessionStatus
from src.core.interfaces import TaskResult
from src.orchestrator import (
    TRANSIENT_PAUSE_BACKOFF_SEC,
    TaskOrchestrator,
    is_transient_provider_pause_result,
    _session_status_after_result,
)


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _flags(monkeypatch, *, transient="1") -> None:
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("CASE_QUOTA_RESUME_ENABLED", "1")
    monkeypatch.setenv("TRANSIENT_PROVIDER_RESUME_ENABLED", transient)


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


class _Orch:
    """Duck-typed ``self`` delegating every method under test to the REAL
    ``TaskOrchestrator`` implementation."""

    _FLOW_RUN_META_KEY = TaskOrchestrator._FLOW_RUN_META_KEY
    _CASE_ID_META_KEY = TaskOrchestrator._CASE_ID_META_KEY

    def __init__(self, store):
        self.session_store = store
        self.deliveries = []
        self.emitted = []
        self.running = True

    def _emit_event(self, name, _t=None, payload=None):
        self.emitted.append((name, payload or {}))

    def _harness_flow_drive_enabled(self):
        return TaskOrchestrator._harness_flow_drive_enabled()

    def _short_failure_reason(self, result):
        return TaskOrchestrator._short_failure_reason(result)

    def _record_transient_pause(self, task, result):
        return TaskOrchestrator._record_transient_pause(self, task, result)

    def _render_transient_retry_turn(self, db, case_id, pause):
        return TaskOrchestrator._render_transient_retry_turn(self, db, case_id, pause)

    async def _handle_transient_paused_case(self, db, case_id):
        return await TaskOrchestrator._handle_transient_paused_case(self, db, case_id)

    async def _handle_quota_paused_case(self, db, case_id):
        return await TaskOrchestrator._handle_quota_paused_case(self, db, case_id)

    async def submit_instruction(self, description, session_id=None, cwd=None, source=""):
        self.deliveries.append(
            {"description": description, "session_id": session_id, "source": source}
        )
        return f"turn-{len(self.deliveries)}"

    async def _continue_case_once(self, db, case_id):
        return await TaskOrchestrator._continue_case_once(self, db, case_id)


class _Task:
    def __init__(self, task_id, case_id, session_id, description="do the thing"):
        self.id = task_id
        self.description = description
        self.metadata = {
            TaskOrchestrator._CASE_ID_META_KEY: case_id,
            "session_id": session_id,
        }


def _transient_result(task_id="t1", error_class="upstream_error", status=529) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        success=False,
        output="",
        errors=["Anthropic API overloaded"],
        files_modified=[],
        execution_time=8.0,
        timestamp=_iso(_now()),
        raw_stdout='{"type":"result","is_error":true,"api_error_status":%d}' % status,
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


def _append_pause(db, case_id, sid, *, task_id="paused-turn", retry_at,
                  attempt=1, failed_prompt="do the thing", truncated=False):
    """Directly append an open transient pause with a controlled retry_at, so a
    test can put the backoff in the past/future without waiting on wall time."""
    db.append_flow_event(
        case_id, "flow.transient_paused", "system",
        entity_type="task", entity_id=task_id,
        payload={"session_id": sid, "paused_task_id": task_id,
                 "error_class": "upstream_error", "attempt": attempt,
                 "backoff_sec": 30, "retry_at": retry_at,
                 "failed_prompt": failed_prompt,
                 "failed_prompt_truncated": truncated, "reason": "overloaded"},
    )


def _events(db, case_id, event_type):
    return [e for e in db.list_flow_events(case_id) if e["event_type"] == event_type]


# --------------------------------------------------------------------------- #
# 1. Classification honesty                                                    #
# --------------------------------------------------------------------------- #

def test_529_classifies_as_upstream_error():
    orch = TaskOrchestrator.__new__(TaskOrchestrator)
    assert TaskOrchestrator._classify_error(orch, _transient_result()) == "upstream_error"


def test_transient_pause_keeps_session_reusable_when_flag_on(monkeypatch):
    """Flag ON: nothing broke — the provider was overloaded — so the session
    stays AWAITING_INPUT while the turn still reports failed (no work produced)."""
    monkeypatch.setenv("TRANSIENT_PROVIDER_RESUME_ENABLED", "1")
    result = _transient_result()
    assert is_transient_provider_pause_result(result) is True
    assert _session_status_after_result(result) == SessionStatus.AWAITING_INPUT
    assert result.success is False


def test_flag_off_transient_is_byte_identical_error(monkeypatch):
    """Flag OFF ⇒ pre-feature behaviour: the transient turn lands in ERROR."""
    monkeypatch.setenv("TRANSIENT_PROVIDER_RESUME_ENABLED", "0")
    assert _session_status_after_result(_transient_result()) == SessionStatus.ERROR


def test_agent_text_about_server_errors_is_not_a_transient_pause():
    """A genuine hard failure whose output merely discusses 5xx handling must not
    be laundered into a pause: the predicate keys on error_class only."""
    result = TaskResult(
        task_id="t-text", success=False,
        output="I wrote the 503 retry handler, then a real bug crashed the tool.",
        errors=["boom"], files_modified=[], execution_time=1.0,
        timestamp=_iso(_now()), raw_stdout="", raw_stderr="", return_code=1,
        error_class="fatal",
    )
    assert is_transient_provider_pause_result(result) is False


# --------------------------------------------------------------------------- #
# 2. The pause is durable, Manager-only, single, and bounded                   #
# --------------------------------------------------------------------------- #

def test_manager_transient_death_pauses_the_case(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    case_id = _case(db, "mgr-1")

    orch._record_transient_pause(_Task("paused-turn", case_id, "mgr-1"),
                                 _transient_result("paused-turn"))

    pause = db.transient_pause(case_id)
    assert pause is not None
    assert pause["session_id"] == "mgr-1"
    assert pause["paused_task_id"] == "paused-turn"
    assert pause["attempt"] == 1
    assert pause["backoff_sec"] == TRANSIENT_PAUSE_BACKOFF_SEC[0]
    assert pause["failed_prompt"] == "do the thing"
    assert _parse_ok(pause["retry_at"])
    assert len(_events(db, case_id, "flow.transient_paused")) == 1
    assert any(name == "case_transient_paused" for name, _ in orch.emitted)


def _parse_ok(value):
    from src.orchestrator import _parse_iso_utc
    return _parse_iso_utc(value) is not None


def test_flag_off_records_nothing(tmp_path, monkeypatch):
    _flags(monkeypatch, transient="0")
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    case_id = _case(db, "mgr-1")

    orch._record_transient_pause(_Task("paused-turn", case_id, "mgr-1"),
                                 _transient_result("paused-turn"))

    assert db.transient_pause(case_id) is None
    assert _events(db, case_id, "flow.transient_paused") == []


def test_second_transient_death_does_not_open_a_second_pause(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    case_id = _case(db, "mgr-1")

    orch._record_transient_pause(_Task("t1", case_id, "mgr-1"), _transient_result("t1"))
    orch._record_transient_pause(_Task("t2", case_id, "mgr-1"), _transient_result("t2"))

    assert len(_events(db, case_id, "flow.transient_paused")) == 1
    assert db.transient_pause(case_id)["paused_task_id"] == "t1"


def test_worker_transient_death_does_not_pause_the_case(tmp_path, monkeypatch):
    """The failing session is not this Case's Manager — a worker 5xx is the
    worker's own retry problem."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1"), _FakeSession("wkr-9")))
    case_id = _case(db, "mgr-1")

    orch._record_transient_pause(_Task("tw", case_id, "wkr-9"), _transient_result("tw"))

    assert db.transient_pause(case_id) is None


def test_escalating_backoff_then_bounded_exhaustion(tmp_path, monkeypatch):
    """Each consecutive pause in the window escalates the backoff; once the
    schedule is spent the Case escalates instead of pausing again."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    case_id = _case(db, "mgr-1")

    seen_backoffs = []
    for i in range(len(TRANSIENT_PAUSE_BACKOFF_SEC)):
        orch._record_transient_pause(_Task(f"t{i}", case_id, "mgr-1"),
                                     _transient_result(f"t{i}"))
        pause = db.transient_pause(case_id)
        assert pause["attempt"] == i + 1
        seen_backoffs.append(pause["backoff_sec"])
        # close this pause so the next record is allowed (mirrors a delivered retry)
        db.append_flow_event(case_id, "flow.transient_resumed", "system",
                             payload={"paused_task_id": f"t{i}", "attempt": i + 1})

    assert seen_backoffs == list(TRANSIENT_PAUSE_BACKOFF_SEC)

    # One more failure exceeds the schedule → exhausted, not a fresh pause.
    orch._record_transient_pause(_Task("t-final", case_id, "mgr-1"),
                                 _transient_result("t-final"))
    assert db.transient_pause(case_id) is None
    assert len(_events(db, case_id, "flow.transient_pause_exhausted")) == 1
    assert any(name == "case_transient_pause_exhausted" for name, _ in orch.emitted)


# --------------------------------------------------------------------------- #
# 3. The Wake-Dispatcher self-heals — bounded, single-flight, verbatim         #
# --------------------------------------------------------------------------- #

def test_backoff_not_elapsed_holds_without_retrying(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    case_id = _case(db, "mgr-1")
    _append_pause(db, case_id, "mgr-1", retry_at=_iso(_now() + timedelta(seconds=120)))

    owned = asyncio.run(orch._handle_transient_paused_case(db, case_id))

    assert owned is True
    assert orch.deliveries == []


def test_backoff_elapsed_retries_the_exact_turn(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    case_id = _case(db, "mgr-1")
    _append_pause(db, case_id, "mgr-1", retry_at=_iso(_now() - timedelta(seconds=1)),
                  failed_prompt="dispatch worker to fix the parser")

    owned = asyncio.run(orch._handle_transient_paused_case(db, case_id))

    assert owned is True
    assert len(orch.deliveries) == 1
    d = orch.deliveries[0]
    assert d["source"] == "manager_transient_resume"
    assert d["session_id"] == "mgr-1"
    assert "dispatch worker to fix the parser" in d["description"]  # verbatim retry
    assert len(_events(db, case_id, "flow.transient_resumed")) == 1
    assert db.transient_pause(case_id) is None  # pause closed by the retry
    assert any(name == "case_transient_resumed" for name, _ in orch.emitted)


def test_truncated_prompt_falls_back_to_generic_retry(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    case_id = _case(db, "mgr-1", objective="rebuild the index")
    _append_pause(db, case_id, "mgr-1", retry_at=_iso(_now() - timedelta(seconds=1)),
                  failed_prompt="", truncated=True)

    asyncio.run(orch._handle_transient_paused_case(db, case_id))

    assert len(orch.deliveries) == 1
    desc = orch.deliveries[0]["description"]
    assert "get_case_brief" in desc
    assert "rebuild the index" in desc


def test_dead_session_closes_the_pause_and_hands_back(tmp_path, monkeypatch):
    """The session that 529'd is gone — don't retry into a corpse; close the pause
    and let the dead-manager/respawn path own it."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore())  # mgr-1 is gone
    case_id = _case(db, "mgr-1")
    _append_pause(db, case_id, "mgr-1", retry_at=_iso(_now() - timedelta(seconds=1)))

    owned = asyncio.run(orch._handle_transient_paused_case(db, case_id))

    assert owned is False
    assert orch.deliveries == []
    resumed = _events(db, case_id, "flow.transient_resumed")
    assert len(resumed) == 1
    assert db.transient_pause(case_id) is None


def test_busy_manager_holds_without_double_driving(tmp_path, monkeypatch):
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1", status=SessionStatus.BUSY)))
    case_id = _case(db, "mgr-1")
    _append_pause(db, case_id, "mgr-1", retry_at=_iso(_now() - timedelta(seconds=1)))

    owned = asyncio.run(orch._handle_transient_paused_case(db, case_id))

    assert owned is True
    assert orch.deliveries == []
    assert db.transient_pause(case_id) is not None  # still open


def test_single_flight_lease_prevents_double_retry(tmp_path, monkeypatch):
    """A concurrent tick that already claimed the retry row makes this one a
    no-op delivery (owns the tick, delivers nothing)."""
    _flags(monkeypatch)
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    case_id = _case(db, "mgr-1")
    _append_pause(db, case_id, "mgr-1", retry_at=_iso(_now() - timedelta(seconds=1)),
                  attempt=1)
    # Pre-claim the deterministic retry token as if a racing tick won it.
    from src.control.db import CONTINUATION_MACHINE_SENTINEL
    retry_id = transient_resume_task_id(case_id, "paused-turn", 1)
    db.enqueue_task(retry_id, session_id=None, machine_id=CONTINUATION_MACHINE_SENTINEL,
                    backend="claude", action=TRANSIENT_RESUME_ACTION, payload={})
    assert db.claim_task(retry_id, socket.gethostname()) is True

    owned = asyncio.run(orch._handle_transient_paused_case(db, case_id))

    assert owned is True
    assert orch.deliveries == []  # lost the claim → no second retry


def test_handler_inert_when_flag_off(tmp_path, monkeypatch):
    _flags(monkeypatch, transient="0")
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    case_id = _case(db, "mgr-1")
    _append_pause(db, case_id, "mgr-1", retry_at=_iso(_now() - timedelta(seconds=1)))

    owned = asyncio.run(orch._handle_transient_paused_case(db, case_id))

    assert owned is False
    assert orch.deliveries == []


# --------------------------------------------------------------------------- #
# 4. Integration: a transient-paused Case is not taken by the ordinary wake    #
# --------------------------------------------------------------------------- #

def test_paused_case_not_woken_by_a_satisfied_wait_group(tmp_path, monkeypatch):
    _flags(monkeypatch)
    monkeypatch.setenv("DURABLE_RELAY_ENABLED", "1")
    db = _mk_db(tmp_path, monkeypatch)
    orch = _Orch(_FakeStore(_FakeSession("mgr-1")))
    case_id = _case(db, "mgr-1")
    db.arm_wait_group(case_id, "g1", "ALL", ["t1"])
    db.append_flow_event(case_id, "task.finished", "worker",
                         entity_type="task", entity_id="t1",
                         payload={"outcome": "success"})
    # backoff still running → the pause owns the tick
    _append_pause(db, case_id, "mgr-1", retry_at=_iso(_now() + timedelta(seconds=120)))

    delivered = asyncio.run(orch._continue_case_once(db, case_id))

    assert delivered == 0
    assert orch.deliveries == []
