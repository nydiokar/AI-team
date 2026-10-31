"""A83 — truth table + bounded-read proof for the derived SessionReason.

Spec: docs/TBD/SESSION_WAIT_STATE_GRANULARITY.md. The reason is ADDITIVE,
READ-PATH ONLY, and never mutates the SessionStatus enum. These tests prove:
- the full primary x reason truth table (fake MeshDB),
- quota + retry pause, open_case_idle for BOTH manager and worker,
- BUSY/terminal -> empty with ZERO DB reads,
- node-offline detail = node id,
- a racy/stale ledger write between reads,
- NO N+1: a page of N sessions issues bounded 500-ID running-owner, inbox,
  live-task and Case-evidence queries, with ZERO per-Case pause/status reads,
  never a Case event-log fold (asserted by a call-count spy, not vibes),
- A104: ``waiting_workers`` comes from the agent inbox and is ROLE-FREE (any
  session with outstanding requests or undelivered messages),
and re-runs the core cases against a REAL file-backed MeshDB.

No paid CLI, no network.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.control.db import CaseReasonEvidence
from src.core.interfaces import SessionStatus
from src.core.session_reason import (
    SessionReason,
    _REASON_KINDS,
    _CLOSED_CASE_STATUSES,
    build_reason_batch,
    derive_session_reason,
    derive_session_reasons,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
@dataclass
class _FakeSession:
    """Only the fields the derivation reads off the loaded row."""
    session_id: str
    status: SessionStatus
    case_role: Optional[str] = None
    current_case_id: Optional[str] = None
    machine_id: str = ""


class _FakeDB:
    """A call-counting fake MeshDB exposing exactly the read methods used.

    Every method bumps a per-method counter so tests can assert the bounded,
    N+1-free access pattern by inspection rather than by faith.
    """

    def __init__(
        self,
        *,
        running_job_session_ids: Optional[List[str]] = None,
        case_status: Optional[Dict[str, Optional[str]]] = None,  # cid -> status ("" open)
        case_events: Optional[Dict[str, List[Dict[str, Any]]]] = None,  # cid -> flow_events
        quota_paused: Optional[List[str]] = None,
        retry_paused: Optional[List[str]] = None,
        inbox_waiting: Optional[List[str]] = None,  # sids pending_for reports waiting
        live_task_session_ids: Optional[List[str]] = None,  # sids owning a non-terminal task
    ) -> None:
        self._running = set(running_job_session_ids or [])
        # case_status None value or missing key => no flow_run row (get_flow_run None)
        self._case_status = dict(case_status or {})
        self._case_events = dict(case_events or {})
        self._quota = set(quota_paused or [])
        self._retry = set(retry_paused or [])
        self._inbox_waiting = set(inbox_waiting or [])
        self._live_tasks = set(live_task_session_ids or [])
        self.calls: Dict[str, int] = {}

    def _bump(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    # --- the read surface the derivation uses --- #
    def running_job_sessions(self, session_ids: list[str]) -> set[str]:
        self._bump("running_job_sessions")
        return self._running.intersection(session_ids)

    def case_reason_evidence(self, flow_run_ids: list[str]) -> dict[str, CaseReasonEvidence]:
        self._bump("case_reason_evidence")
        return {
            cid: CaseReasonEvidence(status=self._case_status[cid],
                                    quota_paused=cid in self._quota and bool(self._case_events.get(cid)),
                                    retry_paused=cid in self._retry and bool(self._case_events.get(cid)))
            for cid in set(flow_run_ids) if cid in self._case_status
        }

    def max_flow_event_ids(self, flow_run_ids: List[str]) -> Dict[str, int]:
        self._bump("max_flow_event_ids")
        out: Dict[str, int] = {}
        for cid in flow_run_ids:
            evs = self._case_events.get(cid)
            if evs:
                out[cid] = len(evs)  # any positive id-ish value == "has events"
        return out

    def get_flow_run(self, flow_run_id: str) -> Optional[Dict[str, Any]]:
        self._bump("get_flow_run")
        if flow_run_id not in self._case_status:
            return None
        return {"flow_run_id": flow_run_id, "status": self._case_status[flow_run_id]}

    def case_quota_pause(self, flow_run_id: str) -> Optional[Dict[str, Any]]:
        self._bump("case_quota_pause")
        return {"paused_at": "x"} if flow_run_id in self._quota else None

    def transient_pause(self, flow_run_id: str) -> Optional[Dict[str, Any]]:
        self._bump("transient_pause")
        return {"paused_at": "x"} if flow_run_id in self._retry else None

    def inbox_waiting_for(self, session_ids: List[str]) -> Dict[str, bool]:
        self._bump("inbox_waiting_for")
        return {sid: sid in self._inbox_waiting for sid in session_ids}

    def live_task_sessions(self, session_ids: List[str]) -> set:
        # One batched read for the whole page — the own-work liveness condition.
        self._bump("live_task_sessions")
        return {sid for sid in session_ids if sid in self._live_tasks}

    def list_flow_events(self, flow_run_id: str, limit: int = 500):
        # A104: the reason must NEVER fold a Case event log — counted to pin 0.
        self._bump("list_flow_events")
        return list(self._case_events.get(flow_run_id, []))


def _pending(gid: str) -> Dict[str, Any]:
    # Legacy wait-group marker: A104 ignores it (inbox is the only wait truth).
    return {"entity_type": "wait_group", "entity_id": gid, "event_type": "worker.wait_pending"}


def _reason_of(db: _FakeDB, session: _FakeSession) -> Optional[SessionReason]:
    return derive_session_reasons(db, [session])[session.session_id]


# --------------------------------------------------------------------------- #
# BUSY / terminal -> empty with ZERO DB reads
# --------------------------------------------------------------------------- #
def test_busy_and_terminal_short_circuit_to_empty_with_zero_db_reads():
    for st in (
        SessionStatus.BUSY,
        SessionStatus.ERROR,
        SessionStatus.CANCELLED,
        SessionStatus.CLOSED,
    ):
        db = _FakeDB(running_job_session_ids=["s1"], case_status={"c1": ""})
        s = _FakeSession("s1", st, case_role="manager", current_case_id="c1")
        # derive_session_reason reads ONLY from a batch (no db); build_reason_batch
        # still runs the single page-level running-owner read, so assert the PER-SESSION
        # derivation itself does zero case reads and returns None.
        batch = build_reason_batch(db, [s])
        before = dict(db.calls)
        assert derive_session_reason(s, batch) is None
        assert db.calls == before, "per-session derivation must not read the db"
        # BUSY/terminal contribute NO case reads to the batch either.
        assert db.calls.get("get_flow_run", 0) == 0
        assert db.calls.get("case_quota_pause", 0) == 0
        assert db.calls.get("transient_pause", 0) == 0
        assert db.calls.get("list_flow_events", 0) == 0
        assert db.calls.get("max_flow_event_ids", 0) == 0
        assert db.calls.get("inbox_waiting_for", 0) == 0


# --------------------------------------------------------------------------- #
# Priority-ordered truth table for AWAITING_INPUT / IDLE
# --------------------------------------------------------------------------- #
def test_paused_quota_wins_over_everything_else():
    db = _FakeDB(
        running_job_session_ids=["s1"],
        case_status={"c1": ""},
        case_events={"c1": [_pending("g1")]},
        quota_paused=["c1"],
        retry_paused=["c1"],
    )
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, "manager", "c1")
    r = _reason_of(db, s)
    assert r == SessionReason(kind="paused_quota", confidence="high")


def test_paused_retry_when_no_quota():
    db = _FakeDB(
        case_status={"c1": ""},
        case_events={"c1": [{"event_type": "flow.transient_paused"}]},
        retry_paused=["c1"],
    )
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, "manager", "c1")
    assert _reason_of(db, s) == SessionReason(kind="paused_retry", confidence="high")


def test_waiting_workers_for_manager_with_unresolved_wait_group():
    # A104: "unresolved wait" == pending_for(s1) reports outstanding work.
    db = _FakeDB(case_status={"c1": ""}, case_events={"c1": [{"event_type": "flow.created"}]},
                 inbox_waiting=["s1"])
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, "manager", "c1")
    assert _reason_of(db, s) == SessionReason(kind="waiting_workers", confidence="high")


def test_resolved_wait_group_is_not_waiting_workers():
    # Inbox empty for s1 -> not waiting -> open_case_idle. A legacy (unresolved)
    # wait-group marker in the ledger is NOT wait truth any more (A104).
    db = _FakeDB(case_status={"c1": ""}, case_events={"c1": [_pending("g1")]}, inbox_waiting=[])
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, "manager", "c1")
    assert _reason_of(db, s) == SessionReason(kind="open_case_idle", confidence="medium")


def test_worker_with_unresolved_wait_group_is_not_waiting_workers():
    # A104: the reason is per-SESSION, not per-Case: a worker on a Case whose
    # Manager is waiting is NOT waiting_workers itself ...
    db = _FakeDB(case_status={"c1": ""}, case_events={"c1": [{"event_type": "flow.created"}]},
                 inbox_waiting=["mgr"])
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, "worker", "c1")
    r = _reason_of(db, s)
    assert r == SessionReason(kind="open_case_idle", confidence="medium")
    # ... but role-free: a worker that itself requested work IS waiting_workers.
    db2 = _FakeDB(case_status={"c1": ""}, case_events={"c1": [{"event_type": "flow.created"}]},
                  inbox_waiting=["s1"])
    assert _reason_of(db2, s) == SessionReason(kind="waiting_workers", confidence="high")


def test_waiting_job_when_a_running_job_exists():
    db = _FakeDB(running_job_session_ids=["s1"], case_status={"c1": ""}, case_events={"c1": []})
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, "worker", "c1")
    assert _reason_of(db, s) == SessionReason(kind="waiting_job", confidence="high")


def test_work_in_flight_when_session_owns_a_live_task():
    # The reported mismatch: primary state IDLE but the session still owns a
    # non-terminal task. The ledger's positive proof wins — honest "work in
    # flight", never a bare "idle" atop a running worker/turn.
    db = _FakeDB(case_status={"c1": ""}, live_task_session_ids=["s1"])
    s = _FakeSession("s1", SessionStatus.IDLE, "worker", "c1")
    assert _reason_of(db, s) == SessionReason(kind="work_in_flight", confidence="high")


def test_work_in_flight_ranks_below_pauses_but_above_absence_based_waits():
    # A paused session is still paused (pause wins over live-task proof) ...
    paused = _FakeDB(case_status={"c1": ""}, case_events={"c1": [{"event_type": "x"}]},
                     quota_paused=["c1"], live_task_session_ids=["s1"])
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, "manager", "c1")
    assert _reason_of(paused, s) == SessionReason(kind="paused_quota", confidence="high")
    # ... but a live task outranks waiting_workers / waiting_job / open_case_idle,
    # all of which are inference/absence-based next to positive ledger proof.
    db = _FakeDB(running_job_session_ids=["s1"], case_status={"c1": ""},
                 case_events={"c1": [{"event_type": "x"}]},
                 inbox_waiting=["s1"], live_task_session_ids=["s1"])
    assert _reason_of(db, s) == SessionReason(kind="work_in_flight", confidence="high")


def test_live_task_read_is_bounded_once_per_page():
    # One batched live-task read for the whole page, never per session (no N+1).
    db = _FakeDB(case_status={"c1": ""},
                 live_task_session_ids=["s1", "s2"])
    sessions = [
        _FakeSession(f"s{i}", SessionStatus.IDLE, "worker", "c1") for i in (1, 2, 3)
    ]
    derive_session_reasons(db, sessions)
    assert db.calls.get("live_task_sessions", 0) == 1


def test_open_case_idle_for_manager_is_the_stuck_tell():
    # Manager on an OPEN case, no pause, no wait-group, no running job.
    db = _FakeDB(case_status={"c1": ""}, case_events={"c1": [{"event_type": "flow.created"}]})
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, "manager", "c1")
    assert _reason_of(db, s) == SessionReason(kind="open_case_idle", confidence="medium")


def test_open_case_idle_for_worker_is_parked_idle():
    db = _FakeDB(case_status={"c1": ""}, case_events={"c1": [{"event_type": "flow.created"}]})
    s = _FakeSession("s1", SessionStatus.IDLE, "worker", "c1")
    assert _reason_of(db, s) == SessionReason(kind="open_case_idle", confidence="medium")


def test_plain_idle_when_no_case_affiliation():
    db = _FakeDB()
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, None, None)
    assert _reason_of(db, s) == SessionReason(kind="idle", confidence="high")


def test_idle_when_case_is_closed_is_not_open_case_idle():
    for closed in _CLOSED_CASE_STATUSES:
        db = _FakeDB(case_status={"c1": closed}, case_events={"c1": [_pending("g1")]})
        s = _FakeSession("s1", SessionStatus.IDLE, "manager", "c1")
        # Closed case => not open; not waiting-on-workers (openness precedes the
        # wait read); no running job => plain idle.
        assert _reason_of(db, s) == SessionReason(kind="idle", confidence="high")


def test_case_row_absent_falls_through_to_plain_idle():
    # current_case_id set but no flow_run row (get_flow_run None) => not open.
    db = _FakeDB(case_status={}, case_events={})
    s = _FakeSession("s1", SessionStatus.IDLE, "manager", "c1")
    assert _reason_of(db, s) == SessionReason(kind="idle", confidence="high")


# --------------------------------------------------------------------------- #
# Node-offline holds
# --------------------------------------------------------------------------- #
def test_node_offline_detail_is_the_node_id_with_zero_db_reads():
    for st in (SessionStatus.PINNED_NODE_OFFLINE, SessionStatus.PAUSED_PINNED_NODE_OFFLINE):
        db = _FakeDB()
        s = _FakeSession("s1", st, machine_id="Horse")
        batch = build_reason_batch(db, [s])
        before = dict(db.calls)
        r = derive_session_reason(s, batch)
        assert r == SessionReason(kind="node_offline", confidence="high", detail="Horse")
        assert db.calls == before  # node-offline derivation reads nothing


# --------------------------------------------------------------------------- #
# Racy / stale ledger: an event written between reads
# --------------------------------------------------------------------------- #
def test_racy_ledger_event_written_between_watermark_and_scan():
    # A request lands in the inbox AFTER the batch is built. The batch snapshot is
    # what the page rendered; a later request re-derives and picks it up. We prove
    # both: the snapshot is stable, and a fresh derive reflects the new state.
    db = _FakeDB(case_status={"c1": ""}, case_events={"c1": [{"event_type": "flow.created"}]})
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, "manager", "c1")
    batch = build_reason_batch(db, [s])
    assert derive_session_reason(s, batch) == SessionReason(kind="open_case_idle", confidence="medium")
    # inbox races forward (a dispatch requested by s1)
    db._inbox_waiting.add("s1")
    assert derive_session_reason(s, batch) == SessionReason(kind="open_case_idle", confidence="medium")
    assert _reason_of(db, s) == SessionReason(kind="waiting_workers", confidence="high")


# --------------------------------------------------------------------------- #
# NO N+1 — the safety-critical bounded-read assertion
# --------------------------------------------------------------------------- #
def test_no_n_plus_1_for_a_page_of_sessions():
    # A page of many sessions across all states.
    managers = [
        _FakeSession(f"m{i}", SessionStatus.AWAITING_INPUT, "manager", f"cm{i}")
        for i in range(5)
    ]
    workers = [
        _FakeSession(f"w{i}", SessionStatus.IDLE, "worker", f"cw{i}")
        for i in range(5)
    ]
    busy = [_FakeSession(f"b{i}", SessionStatus.BUSY, "worker", f"cb{i}") for i in range(5)]
    sessions = managers + workers + busy

    case_status = {s.current_case_id: "" for s in managers + workers}
    case_events = {s.current_case_id: [{"event_type": "flow.created"}] for s in managers + workers}
    db = _FakeDB(case_status=case_status, case_events=case_events)

    derive_session_reasons(db, sessions)

    # ONE batched job read for the whole page — the N+1 hazard, pinned to 1.
    assert db.calls.get("running_job_sessions", 0) == 1
    # ONE combined Case evidence read, no watermark/per-Case reads.
    assert db.calls.get("case_reason_evidence", 0) == 1
    assert db.calls.get("max_flow_event_ids", 0) == 0
    # ONE batched inbox read for the whole page (A104) — never per session.
    assert db.calls.get("inbox_waiting_for", 0) == 1
    # No individual Case reads, regardless of the number of distinct Cases.
    assert db.calls.get("get_flow_run", 0) == 0
    # NO Case event-log fold at all (the legacy per-manager wait-group scan).
    assert db.calls.get("list_flow_events", 0) == 0
    # Both pause kinds are projected together, without per-Case helper calls.
    assert db.calls.get("case_quota_pause", 0) == 0
    assert db.calls.get("transient_pause", 0) == 0


def test_eventless_cases_need_no_per_case_reads():
    # A case with NO events has no pause; it can only be open_case_idle or idle.
    db = _FakeDB(case_status={"c1": ""}, case_events={"c1": []})  # open, but zero events
    s = _FakeSession("s1", SessionStatus.AWAITING_INPUT, "manager", "c1")
    r = _reason_of(db, s)
    assert r == SessionReason(kind="open_case_idle", confidence="medium")
    assert db.calls.get("case_quota_pause", 0) == 0
    assert db.calls.get("transient_pause", 0) == 0
    assert db.calls.get("list_flow_events", 0) == 0


def test_vocabulary_matches_spec():
    assert _REASON_KINDS == {
        "paused_quota",
        "paused_retry",
        "waiting_workers",
        "waiting_job",
        "open_case_idle",
        "work_in_flight",
        "idle",
        "node_offline", "unavailable",
    }


# --------------------------------------------------------------------------- #
# Real file-backed MeshDB — prove the derivation against actual queries
# --------------------------------------------------------------------------- #
def test_against_real_meshdb(tmp_path):
    from src.control.db import MeshDB

    db = MeshDB(str(tmp_path / "mesh.db"))

    # Confirm the closed-status literal actually matches MeshDB's constant.
    assert tuple(_CLOSED_CASE_STATUSES) == tuple(MeshDB._CLOSED_STATUSES)

    from tests.inbox_seed import finish_child, seed_child

    # Manager on an open case with a requested child still running => waiting_workers.
    mgr_case = db.open_case("do the thing", "sess_mgr", role="manager")
    seed_child(db, mgr_case, "task_w1", requester="sess_mgr")
    mgr = _FakeSession("sess_mgr", SessionStatus.AWAITING_INPUT, "manager", mgr_case)
    assert _reason_of(db, mgr) == SessionReason(kind="waiting_workers", confidence="high")

    # Child finished, its completion not yet consumed => still waiting_workers.
    finish_child(db, "task_w1")
    assert _reason_of(db, mgr) == SessionReason(kind="waiting_workers", confidence="high")

    # Completion consumed by a tagged review => open_case_idle (the stuck tell).
    db.append_flow_event(mgr_case, "review.accepted", "manager", entity_type="task", entity_id="task_w1")
    assert derive_session_reasons(db, [mgr])[mgr.session_id] == SessionReason(
        kind="open_case_idle", confidence="medium"
    )

    # Quota pause on the case => paused_quota wins.
    db.append_flow_event(mgr_case, "flow.quota_paused", "system", payload={"limit": "usage_limit"})
    assert derive_session_reasons(db, [mgr])[mgr.session_id] == SessionReason(
        kind="paused_quota", confidence="high"
    )

    # A worker parked-idle on its own open case => open_case_idle.
    wkr_case = db.open_case("worker objective", "sess_wkr", role="manager")
    db.append_flow_event(wkr_case, "flow.note", "worker")  # some event so the case has a watermark
    wkr = _FakeSession("sess_wkr", SessionStatus.IDLE, "worker", wkr_case)
    assert derive_session_reasons(db, [wkr])[wkr.session_id] == SessionReason(
        kind="open_case_idle", confidence="medium"
    )

    # No-case session => plain idle.
    plain = _FakeSession("sess_plain", SessionStatus.AWAITING_INPUT, None, None)
    assert derive_session_reasons(db, [plain])[plain.session_id] == SessionReason(
        kind="idle", confidence="high"
    )

    # BUSY => empty even against the real db.
    busy = _FakeSession("sess_busy", SessionStatus.BUSY, "manager", mgr_case)
    assert derive_session_reasons(db, [busy])[busy.session_id] is None


def test_work_in_flight_against_real_meshdb(tmp_path):
    from src.control.db import MeshDB

    db = MeshDB(str(tmp_path / "mesh.db"))

    from src.core.interfaces import Session

    def _seed_session(sid: str) -> None:
        db.upsert_session(Session(
            session_id=sid, backend="claude", repo_path="/tmp",
            status=SessionStatus.IDLE, created_at="2026-10-14T00:00:00+00:00",
            updated_at="2026-10-14T00:00:00+00:00",
        ))

    # A session parked IDLE that still owns a RUNNING mesh task => work_in_flight.
    _seed_session("sess_live")
    _seed_session("sess_done")
    db.enqueue_task(
        task_id="t_live", session_id="sess_live", machine_id="node1",
        backend="claude", action="chat", payload={"prompt": "x"}, status="running",
    )
    live = _FakeSession("sess_live", SessionStatus.IDLE, "worker", None)
    assert derive_session_reasons(db, [live])[live.session_id] == SessionReason(
        kind="work_in_flight", confidence="high"
    )

    # A terminal (completed) task is NOT live => falls through to plain idle.
    db.enqueue_task(
        task_id="t_done", session_id="sess_done", machine_id="node1",
        backend="claude", action="chat", payload={"prompt": "x"}, status="completed",
    )
    done = _FakeSession("sess_done", SessionStatus.IDLE, None, None)
    assert derive_session_reasons(db, [done])[done.session_id] == SessionReason(
        kind="idle", confidence="high"
    )

    # Direct query contract: only the live session comes back for the page.
    assert db.live_task_sessions(["sess_live", "sess_done"]) == {"sess_live"}


def test_live_task_statuses_do_not_drift_from_managed_open_predicate():
    # The liveness read and the canonical managed-open predicate must share the
    # same non-terminal status vocabulary, or a session could show the wrong
    # state. _MANAGED_OPEN_STATUSES is the single source of truth; assert the
    # predicate string is built from exactly those statuses.
    from src.control.db import _MANAGED_OPEN_STATUSES, _MANAGED_OPEN_PREDICATE

    for status in _MANAGED_OPEN_STATUSES:
        assert f"'{status}'" in _MANAGED_OPEN_PREDICATE
    # No terminal status may sneak into the live set.
    assert not ({"completed", "failed", "failed_node_offline", "cancelled", "withdrawn"}
                & set(_MANAGED_OPEN_STATUSES))
