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
  add a new full-log scan. Reads are batched across the page and watermark-gated
  via ``db.max_flow_event_ids`` so unchanged Cases are a cheap index read.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from src.core.interfaces import Session, SessionStatus

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

    Built ONCE per read (``build_reason_batch``) with a single
    ``list_jobs_for_sessions`` call and per-manager pause/wait reads only. Each
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


def _is_manager(session: Session) -> bool:
    return str(getattr(session, "case_role", "") or "") == "manager"


def _case_has_unresolved_wait_group(db: Any, case_id: str) -> bool:
    """True iff the Case has a wait-group whose last event is `worker.wait_pending`.

    Reuses the EXACT bounded fold the heartbeat uses in
    ``orchestrator._cache_heartbeat_owner_live`` (``case_wait_group`` branch):
    one bounded ``list_flow_events`` read, folding pending/resolved per group.
    We do NOT add a new full-log scan (the #145/#147 lesson). Any group left in
    the `pending` terminal state ⇒ an unresolved wait.
    """
    group_live: Dict[str, bool] = {}
    for event in db.list_flow_events(case_id):
        if event.get("entity_type") != "wait_group":
            continue
        gid = event.get("entity_id")
        if not gid:
            continue
        etype = event.get("event_type")
        if etype == "worker.wait_pending":
            group_live[gid] = True
        elif etype == "worker.wait_resolved":
            group_live[gid] = False
    return any(group_live.values())


def build_reason_batch(db: Any, sessions: List[Session]) -> _ReasonBatch:
    """Load page-wide evidence for the reason derivation in bounded, batched reads.

    Bounded-read contract (spec §5 — safety-critical):
    - ONE ``list_jobs_for_sessions(ids)`` for the whole page (already N+1-safe).
    - role/case straight off the already-loaded session rows (no per-row read).
    - pause + wait-group reads done ONLY for the managers on the page whose Case
      is OPEN, watermark-gated via ``db.max_flow_event_ids`` so a Case with no
      events is skipped without a per-Case scan.
    - NO cross-session materialization, NO N+1, and NEVER on a timer / loop.

    #145 / #147: the Wake-Dispatcher stalled the loop by doing exactly the
    unbounded, timer-driven variant of this. Keep it on the read path.
    """
    session_ids = [s.session_id for s in sessions if s.session_id]

    # (1) Batched job read for the WHOLE page — the only per-page job query.
    running_job_sessions: set = set()
    if session_ids:
        try:
            for job in db.list_jobs_for_sessions(session_ids):
                if str(job.get("status") or "") == "running":
                    sid = job.get("session_id")
                    if sid:
                        running_job_sessions.add(str(sid))
        except Exception:
            pass

    # Candidate cases from the loaded rows (no read): any waiting session with a
    # current_case_id. Managers additionally get a wait-group read.
    case_ids: set = set()
    manager_case_ids: set = set()
    for s in sessions:
        if s.status not in _WAIT_STATES:
            continue
        cid = str(getattr(s, "current_case_id", "") or "")
        if not cid:
            continue
        case_ids.add(cid)
        if _is_manager(s):
            manager_case_ids.add(cid)

    open_cases: set = set()
    case_quota_paused: set = set()
    case_retry_paused: set = set()
    case_waiting_workers: Dict[str, bool] = {}

    if case_ids:
        # (2) Watermark: one batched MAX(id) per Case (index-served, O(1)/Case).
        # A Case absent here has NO events at all — it cannot have a pause or a
        # wait-group, so we skip every per-Case read for it. This is the cheap
        # gate the spec's future short-TTL cache would key on.
        try:
            watermarks = db.max_flow_event_ids(list(case_ids))
        except Exception:
            watermarks = {}

        for cid in case_ids:
            # Openness: read the single flow_runs row (NULL/'blocked' ⇒ open).
            try:
                run = db.get_flow_run(cid)
            except Exception:
                run = None
            if run is None:
                continue  # no Case row ⇒ not an open Case; leaves plain `idle`.
            status = str(run.get("status") or "").strip().lower()
            if status in _CLOSED_CASE_STATUSES:
                continue
            open_cases.add(cid)

            has_events = cid in watermarks
            # Pause reads apply to any waiting session on an open Case (manager
            # or worker); gated by the watermark — no events ⇒ no pause.
            if has_events:
                try:
                    if db.case_quota_pause(cid) is not None:
                        case_quota_paused.add(cid)
                except Exception:
                    pass
                try:
                    if db.transient_pause(cid) is not None:
                        case_retry_paused.add(cid)
                except Exception:
                    pass

            # Wait-group read: managers only, and only if the Case has events.
            if cid in manager_case_ids and has_events:
                try:
                    case_waiting_workers[cid] = _case_has_unresolved_wait_group(db, cid)
                except Exception:
                    case_waiting_workers[cid] = False

    return _ReasonBatch(
        running_job_sessions=frozenset(running_job_sessions),
        case_waiting_workers=dict(case_waiting_workers),
        case_quota_paused=frozenset(case_quota_paused),
        case_retry_paused=frozenset(case_retry_paused),
        open_cases=frozenset(open_cases),
    )


def derive_session_reason(session: Session, batch: _ReasonBatch) -> Optional[SessionReason]:
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
    # 3. waiting_workers — manager with an unresolved wait-group.
    if _is_manager(session) and case_id and batch.case_waiting_workers.get(case_id):
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


def derive_session_reasons(db: Any, sessions: List[Session]) -> Dict[str, Optional[SessionReason]]:
    """Batched, N+1-free reason map for a page of sessions: {session_id: reason}.

    One ``build_reason_batch`` (single ``list_jobs_for_sessions`` + per-manager
    bounded reads) followed by pure per-session derivation. ``db`` is None ⇒ all
    reasons None (degrade quietly; the reason is presentational)."""
    if db is None or not sessions:
        return {s.session_id: None for s in sessions}
    batch = build_reason_batch(db, sessions)
    return {s.session_id: derive_session_reason(s, batch) for s in sessions}
