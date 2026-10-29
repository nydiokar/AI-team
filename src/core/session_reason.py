"""Derived, non-authoritative *secondary reason* for a session's primary state.

Spec: ``docs/TBD/SESSION_WAIT_STATE_GRANULARITY.md`` (authoritative). Job A83.

The operator sees a single "pillow" for a session's ``SessionStatus``. For a
waiting session it is always ``AWAITING_INPUT`` — which conflates a Manager
blocked on workers, a Manager genuinely idle, a quota pause, a detached script,
and a session parked on an open Case with *nothing pending* (the stuck /
hallucinated-wait tell). This module derives a ``SessionReason`` on the READ
path that refines the primary state without touching the authoritative enum.

Hard invariants (do not weaken):
- ADDITIVE, READ-PATH ONLY. The ``SessionStatus`` enum stays the single source
  of truth. ``needs_input``/``is_active`` are unchanged; the reason is a sibling.
- BUSY and terminal states short-circuit to ``None`` with ZERO DB reads.
- The Case is read only as *evidence of what the session itself did* — it is
  never an authority and never owns this label. This is session-state
  management, not case-state management.
- **#145 / #147 event-loop-stall lesson (safety-critical):** the Wake-Dispatcher
  once stalled the event loop by scanning every open Case's full event log on a
  TIMER. This derivation MUST stay on the read path — per listed session, on
  request. NEVER move it into a background loop / timer / scheduler, and never
  add a new full-log scan. Reads are batched across the page in deduplicated 500-ID chunks;
  only the latest relevant pause facts are read, never complete event logs.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

from src.core.interfaces import Session, SessionStatus
from src.core.session_projection import SessionListProjection

# Case statuses that mean the Case is CLOSED. Mirrors MeshDB._CLOSED_STATUSES
# (db.py) — NULL / 'blocked' count as OPEN. Kept as a literal (not imported) so
# the read-model layer does not depend on the DB module; asserted in tests.
_CLOSED_CASE_STATUSES = ("closed", "cancelled")


@dataclass(frozen=True)
class SessionReason:
    """A derived, non-authoritative secondary reason for a session's state.

    Mirrors the codebase's own honesty pattern (the timeline's ``TaskTruthState``
    carries ``confidence``/``staleness`` rather than trusting one field).

    ``kind`` — vocabulary per spec §4 (see ``_REASON_KINDS``).
    ``confidence`` — ``"high"`` | ``"medium"``; ``medium`` marks absence-based
        inferences (``open_case_idle``) that can race a just-written ledger event.
    ``detail`` — bounded, presentational (e.g. a node id); ``None`` otherwise.
    """
    kind: str
    confidence: str
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# The full vocabulary (spec §4). Exposed for the truth-table tests to assert
# against, so a rename here fails loudly rather than silently.
_REASON_KINDS = frozenset(
    {
        "paused_quota",
        "paused_retry",
        "waiting_workers",
        "waiting_job",
        "open_case_idle",
        "work_in_flight",
        "idle",
        "node_offline",
    }
)

# Primary states that carry a reason. Everything else (BUSY, ERROR, CANCELLED,
# CLOSED) short-circuits to None with zero DB reads (spec §4).
_WAIT_STATES = (SessionStatus.AWAITING_INPUT, SessionStatus.IDLE)
_NODE_OFFLINE_STATES = (
    SessionStatus.PINNED_NODE_OFFLINE,
    SessionStatus.PAUSED_PINNED_NODE_OFFLINE,
)


@dataclass(frozen=True)
class _ReasonBatch:
    """Pre-loaded, page-wide evidence for a bounded, N+1-free derivation.

    Built ONCE per read (``build_reason_batch``) from bounded owner, inbox,
    live-task and Case evidence queries. Each
    per-session ``derive_session_reason`` then reads only from this in-memory
    snapshot — it issues NO further DB calls."""
    # session_id -> True if a `jobs` row for it is currently `running`.
    running_job_sessions: frozenset
    # manager case_id -> True if it has an unresolved wait-group.
    case_waiting_workers: Dict[str, bool]
    # case_id -> quota-pause payload present (non-None ⇒ paused).
    case_quota_paused: frozenset
    # case_id -> transient-retry-pause payload present.
    case_retry_paused: frozenset
    # case_id -> True if the Case exists AND is not in a closed status.
    open_cases: frozenset
    # [A104 I3] sessions for which ``pending_for`` reports work outstanding
    # (requests still running, or messages not yet delivered) — any role.
    waiting_sessions: frozenset = frozenset()
    # Sessions that own at least one non-terminal mesh task (claimed/running/
    # queued/…). A session parked IDLE/AWAITING_INPUT yet present here is the
    # "stale idle" mismatch: its own work is live. Unifies the reason across the
    # session list AND the durable timeline panel from one batched read.
    live_task_sessions: frozenset = frozenset()


def build_reason_batch(db: Any, sessions: Sequence[Session | SessionListProjection]) -> _ReasonBatch:
    """Load read-only evidence in deduplicated chunks of at most 500 IDs.

    Each full chunk needs five data SELECTs: running-job owners, two inbox
    projections, own-task liveness and Case status/latest independent pause
    facts. No per-Case queries or full event-log folds; never run on a timer.
    """
    session_ids: list[str] = list(dict.fromkeys(s.session_id for s in sessions if s.session_id))
    running_job_sessions: set[str] = set()
    if session_ids:
        try:
            running_job_sessions = set(db.running_job_sessions(session_ids))
        except Exception:
            pass

    # Candidate Cases and waiting owners come straight from loaded session rows.
    case_ids: set[str] = set()
    waiting_ids: list[str] = []
    for s in sessions:
        if s.status not in _WAIT_STATES:
            continue
        if s.session_id:
            waiting_ids.append(s.session_id)
        cid = str(getattr(s, "current_case_id", "") or "")
        if cid:
            case_ids.add(cid)
    waiting_ids = list(dict.fromkeys(waiting_ids))

    # A104: the inbox remains the only source for another agent's pending work.
    # Keep the same grouped inbox/liveness predicates, with bounded IN lists.
    waiting_sessions: set[str] = set()
    live_task_sessions: set[str] = set()
    for start in range(0, len(waiting_ids), 500):
        chunk: list[str] = waiting_ids[start:start + 500]
        try:
            waiting_sessions.update(sid for sid, waiting in db.inbox_waiting_for(chunk).items() if waiting)
        except Exception:
            pass
        try:
            live_task_sessions.update(db.live_task_sessions(chunk))
        except Exception:
            pass

    open_cases: set[str] = set()
    case_quota_paused: set[str] = set()
    case_retry_paused: set[str] = set()
    case_waiting_workers: Dict[str, bool] = {}  # kept for the batch shape; unused
    if case_ids:
        try:
            evidence = db.case_reason_evidence(list(case_ids))
        except Exception:
            evidence = {}
        for cid, case in evidence.items():
            if str(case.status or "").strip().lower() in _CLOSED_CASE_STATUSES:
                continue
            open_cases.add(cid)
            if case.quota_paused:
                case_quota_paused.add(cid)
            if case.retry_paused:
                case_retry_paused.add(cid)

    return _ReasonBatch(
        running_job_sessions=frozenset(running_job_sessions),
        case_waiting_workers=dict(case_waiting_workers),
        case_quota_paused=frozenset(case_quota_paused),
        case_retry_paused=frozenset(case_retry_paused),
        open_cases=frozenset(open_cases),
        waiting_sessions=frozenset(waiting_sessions),
        live_task_sessions=frozenset(live_task_sessions),
    )


def derive_session_reason(session: Session | SessionListProjection, batch: _ReasonBatch) -> Optional[SessionReason]:
    """Derive the secondary reason for ONE session from a pre-loaded ``batch``.

    Issues NO DB calls — all evidence is read from ``batch`` (built once per
    page). BUSY and terminal states return ``None`` (zero reads). For
    ``AWAITING_INPUT``/``IDLE`` the reasons are evaluated in priority order
    (spec §4) and the first match wins.
    """
    status = session.status

    # BUSY → empty (honest limit: cannot tell real work from a foreground script
    # from inside a busy turn — spec §4). ERROR/CANCELLED/CLOSED → empty. Zero
    # DB reads for all of these (short-circuit).
    if status in _NODE_OFFLINE_STATES:
        # PINNED_NODE_OFFLINE / PAUSED_PINNED_NODE_OFFLINE → the hold reason.
        # detail = node id (session.machine_id). R1: grace-remaining omitted —
        # no cheap existing batched read here, so we do NOT add a query.
        node_id = str(getattr(session, "machine_id", "") or "") or None
        return SessionReason(kind="node_offline", confidence="high", detail=node_id)

    if status not in _WAIT_STATES:
        return None

    case_id = str(getattr(session, "current_case_id", "") or "")

    # Priority order (spec §4), first match wins:
    # 1. paused_quota
    if case_id and case_id in batch.case_quota_paused:
        return SessionReason(kind="paused_quota", confidence="high")
    # 2. paused_retry
    if case_id and case_id in batch.case_retry_paused:
        return SessionReason(kind="paused_retry", confidence="high")
    # 2b. work_in_flight — the primary state says the session is waiting/idle, but
    #     it still OWNS a non-terminal task (its own worker/turn is live). This is
    #     the honest reconciliation of the "idle while a worker is running"
    #     mismatch: the ledger (authoritative) wins over a stale primary enum, and
    #     the operator never sees a bare "idle" sitting atop live work. Ranks above
    #     the absence-based waits (a live task is positive proof, not an inference).
    if session.session_id in batch.live_task_sessions:
        return SessionReason(kind="work_in_flight", confidence="high")
    # 3. waiting_workers — ``pending_for`` reports requests this session made
    #    that are still running, or messages for it not yet delivered.
    if session.session_id in batch.waiting_sessions:
        return SessionReason(kind="waiting_workers", confidence="high")
    # 4. waiting_job — a `jobs` row for this session is `running`.
    if session.session_id in batch.running_job_sessions:
        return SessionReason(kind="waiting_job", confidence="high")
    # 5. open_case_idle — joined to an OPEN Case, none of the above. The
    #    stuck / hallucinated-wait tell; absence-based ⇒ confidence MEDIUM.
    #    For a worker this is honest "parked-idle", NOT "waiting on the Manager"
    #    (that asserts a return we cannot promise — spec §4).
    if case_id and case_id in batch.open_cases:
        return SessionReason(kind="open_case_idle", confidence="medium")
    # 6. idle — no open Case, nothing pending. Plain idle; we don't dress it up.
    return SessionReason(kind="idle", confidence="high")


def derive_session_reasons(db: Any, sessions: Sequence[Session | SessionListProjection]) -> Dict[str, Optional[SessionReason]]:
    """Batched, N+1-free reason map for a page of sessions: {session_id: reason}.

    One ``build_reason_batch`` (five data SELECTs per 500-ID chunk) followed
    by pure per-session derivation. ``db`` is None ⇒ all
    reasons None (degrade quietly; the reason is presentational)."""
    if db is None or not sessions:
        return {s.session_id: None for s in sessions}
    batch = build_reason_batch(db, sessions)
    return {s.session_id: derive_session_reason(s, batch) for s in sessions}
