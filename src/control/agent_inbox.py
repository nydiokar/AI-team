"""[A104] The agent inbox — the ONE answer to "what is waiting for this agent?".

A message is addressed agent→agent: the recipient is the session that REQUESTED
the work (the child turn's persisted ``mesh_tasks.sender_session_id``), never a
role. The Case id is provenance only. One store (``agent_inbox``), one read
(:func:`pending_for`), one state machine, every transition a conditional UPDATE
run inside the caller's open write transaction (so it commits or rolls back with
the fact that caused it):

    pending ──deliver(turn)──▶ delivered ──settle(turn, completed)──▶ acked
       ▲                          │
       └──settle(turn, other)─────┘   (attempts+1, backoff; at MAX_ATTEMPTS ⇒ dead)
    pending|delivered ──ack_about_task (tagged review)──▶ acked
    pending|delivered ──kill_case / kill_recipient──▶ dead(reason)

``attempts`` counts wake ADMISSIONS (``deliver``), so a message is admitted at most
``MAX_ATTEMPTS`` times — nothing re-admits forever (D3).

Wait conditions (D2/I8) are filters keyed by member task ids, evaluated over the
member tasks' terminal state: an ALL filter holds every message about one of its
members until every member is terminal. A terminal member without a message still
counts as in, so a filter can never deadlock delivery.

Every function here takes an open ``sqlite3.Connection`` and is pure SQL — no
I/O, no flag reads, no role reads.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from pydantic import BaseModel, Field

MAX_ATTEMPTS: int = 5
BACKOFF_BASE_SECONDS: int = 30
PENDING_LIMIT: int = 256
FILTER_MEMBER_LIMIT: int = 64
KIND_COMPLETION: str = "completion"
OUTSTANDING_STATUSES: tuple[str, ...] = ("queued", "pending", "claimed", "running", "recovery_required")

# Spelled exactly like the partial-index WHERE clauses (migration 45) so SQLite
# can prove the indexes usable.
PENDING_SQL: str = (
    "SELECT message_id, recipient_session_id, sender_session_id, about_task_id, case_id, kind, "
    "outcome, state, attempts, next_attempt_at, delivery_turn_id, created_at "
    "FROM agent_inbox INDEXED BY idx_agent_inbox_pending "
    "WHERE recipient_session_id = ? AND state IN ('pending', 'delivered') "
    "AND (? IS NULL OR case_id = ?) "
    "ORDER BY created_at ASC, message_id ASC LIMIT ?"
)
OUTSTANDING_SQL: str = (
    "SELECT id FROM mesh_tasks INDEXED BY idx_mesh_tasks_requester_open "
    "WHERE sender_session_id = ? "
    "AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required') "
    "AND (? IS NULL OR flow_run_id = ?) "
    "ORDER BY created_at ASC, id ASC LIMIT ?"
)


class InboxMessage(BaseModel):
    message_id: str
    recipient_session_id: str
    sender_session_id: Optional[str] = None
    about_task_id: Optional[str] = None
    case_id: Optional[str] = None
    kind: str
    outcome: Optional[str] = None
    state: str
    attempts: int = 0
    ready_at: Optional[str] = None
    delivery_turn_id: Optional[str] = None
    created_at: str
    held: bool = False
    last_error: Optional[str] = None


class PendingView(BaseModel):
    """What is waiting for one agent: messages not yet consumed (``pending`` or in
    flight as ``delivered``) and the requests it made that are still running."""

    recipient_session_id: str
    case_id: Optional[str] = None
    messages: list[InboxMessage] = Field(default_factory=list)
    outstanding_task_ids: list[str] = Field(default_factory=list)

    def deliverable(self, now: str) -> list[InboxMessage]:
        """Pending, not held by a wait filter, and past its backoff."""
        return [
            m for m in self.messages
            if m.state == "pending" and not m.held and (not m.ready_at or m.ready_at <= now)
        ]

    def carried_by(self, turn_id: str) -> list[InboxMessage]:
        """Messages still in flight on ``turn_id`` (the activation predicate)."""
        return [m for m in self.messages if m.state == "delivered" and m.delivery_turn_id == turn_id]

    def waiting(self) -> bool:
        return bool(self.outstanding_task_ids) or any(m.state == "pending" for m in self.messages)


class SettleResult(BaseModel):
    acked: list[str] = Field(default_factory=list)
    returned: list[str] = Field(default_factory=list)
    dead: list[str] = Field(default_factory=list)


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def completion_message_id(task_id: str) -> str:
    return f"{KIND_COMPLETION}:{task_id}"


def _backoff_until(now: str, attempts: int) -> str:
    base = datetime.fromisoformat(now)
    return (base + timedelta(seconds=BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)))).isoformat()


def _placeholders(n: int) -> str:
    return ", ".join("?" for _ in range(n))


# --------------------------------------------------------------------------- #
# Writer (terminal txn)
# --------------------------------------------------------------------------- #
def record_completion(conn: sqlite3.Connection, task_id: str, status: str, now: str) -> bool:
    """Address ``task_id``'s completion to whoever requested it, in the caller's
    terminal txn. No requester (human / system / own turn) or a self-request ⇒ no
    row. ``INSERT OR IGNORE`` on the deterministic message id collapses a
    duplicate terminal report."""
    row = conn.execute(
        "SELECT session_id, sender_session_id, flow_run_id FROM mesh_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    requester = str(row["sender_session_id"] or "").strip()
    worker = str(row["session_id"] or "").strip()
    if not requester or requester == worker:
        return False
    outcome = "success" if status == "completed" else status
    cur = conn.execute(
        "INSERT OR IGNORE INTO agent_inbox (message_id, recipient_session_id, sender_session_id, "
        "about_task_id, case_id, kind, outcome, state, attempts, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
        (completion_message_id(task_id), requester, worker or None, task_id,
         row["flow_run_id"] or None, KIND_COMPLETION, outcome, now, now),
    )
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# The one read
# --------------------------------------------------------------------------- #
def _held_task_ids(conn: sqlite3.Connection, case_ids: Iterable[str]) -> set[str]:
    """Member task ids held by an unsatisfied ALL filter of any of ``case_ids``."""
    held: set[str] = set()
    for case_id in sorted({c for c in case_ids if c}):
        for f in conn.execute(
            "SELECT member_task_ids FROM inbox_wait_filters WHERE case_id = ? AND condition = 'ALL'",
            (case_id,),
        ).fetchall():
            members = [str(m) for m in json.loads(f["member_task_ids"] or "[]")][:FILTER_MEMBER_LIMIT]
            if not members:
                continue
            live = conn.execute(
                f"SELECT COUNT(*) FROM mesh_tasks WHERE id IN ({_placeholders(len(members))}) "
                "AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required')",
                members,
            ).fetchone()[0]
            if int(live):
                held.update(members)
    return held


def pending_for(
    conn: sqlite3.Connection,
    recipient_session_id: str,
    *,
    case_id: Optional[str] = None,
    limit: int = PENDING_LIMIT,
) -> PendingView:
    """THE answer to "what is waiting for ``recipient_session_id``?" (A104 I3).

    Index-served and bounded: messages via ``idx_agent_inbox_pending``,
    outstanding requests via ``idx_mesh_tasks_requester_open``; ``limit`` caps
    both. Every component (wake producer, activation check, session reason,
    brief, close gate, boot reconcile, heartbeat liveness, Manager tools) reads
    pending state through this function and nothing else."""
    bound = max(1, min(int(limit), PENDING_LIMIT))
    rows = conn.execute(PENDING_SQL, (recipient_session_id, case_id, case_id, bound)).fetchall()
    held = _held_task_ids(conn, (r["case_id"] for r in rows))
    messages = [
        InboxMessage(
            message_id=r["message_id"], recipient_session_id=r["recipient_session_id"],
            sender_session_id=r["sender_session_id"], about_task_id=r["about_task_id"],
            case_id=r["case_id"], kind=r["kind"], outcome=r["outcome"], state=r["state"],
            attempts=int(r["attempts"] or 0), ready_at=r["next_attempt_at"],
            delivery_turn_id=r["delivery_turn_id"], created_at=r["created_at"],
            held=bool(r["about_task_id"]) and r["about_task_id"] in held,
        )
        for r in rows
    ]
    outstanding = [
        str(r["id"]) for r in conn.execute(
            OUTSTANDING_SQL, (recipient_session_id, case_id, case_id, bound),
        ).fetchall()
    ]
    return PendingView(
        recipient_session_id=recipient_session_id, case_id=case_id,
        messages=messages, outstanding_task_ids=outstanding,
    )


def ready_recipients(conn: sqlite3.Connection, now: str, limit: int = PENDING_LIMIT) -> list[tuple[str, Optional[str]]]:
    """(recipient, case) pairs that have a pending message past its backoff —
    the wake producer's work list. Served by the partial pending index."""
    rows = conn.execute(
        "SELECT recipient_session_id, case_id FROM agent_inbox INDEXED BY idx_agent_inbox_pending "
        "WHERE state IN ('pending', 'delivered') AND state = 'pending' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "GROUP BY recipient_session_id, case_id ORDER BY MIN(created_at) LIMIT ?",
        (now, max(1, int(limit))),
    ).fetchall()
    return [(str(r["recipient_session_id"]), r["case_id"]) for r in rows]


# --------------------------------------------------------------------------- #
# State transitions (all conditional ⇒ idempotent)
# --------------------------------------------------------------------------- #
def deliver(conn: sqlite3.Connection, message_ids: list[str], turn_id: str, now: str) -> int:
    """pending → delivered on ``turn_id`` (one admission ⇒ ``attempts + 1``).
    Returns the number of rows claimed; a concurrent claimer gets 0."""
    if not message_ids:
        return 0
    cur = conn.execute(
        "UPDATE agent_inbox SET state = 'delivered', delivery_turn_id = ?, delivered_at = ?, "
        "attempts = attempts + 1, updated_at = ? "
        f"WHERE state = 'pending' AND message_id IN ({_placeholders(len(message_ids))})",
        (turn_id, now, now, *message_ids),
    )
    return cur.rowcount


def settle_turn(conn: sqlite3.Connection, turn_id: str, status: str, now: str) -> SettleResult:
    """Settle every message still in flight on ``turn_id`` once that turn is
    terminal (or withdrawn / never run). ``completed`` acks; anything else returns
    the message to ``pending`` with backoff, or kills it at ``MAX_ATTEMPTS``."""
    result = SettleResult()
    rows = conn.execute(
        "SELECT message_id, attempts FROM agent_inbox "
        "WHERE delivery_turn_id = ? AND state = 'delivered' ORDER BY message_id",
        (turn_id,),
    ).fetchall()
    for r in rows:
        mid, attempts = str(r["message_id"]), int(r["attempts"] or 0)
        if status == "rebound":
            # The wake's seat was rebound before it ran: not a delivery failure —
            # the attempt is refunded and the message is immediately re-deliverable
            # (it then follows the rebind record to the new holder).
            conn.execute(
                "UPDATE agent_inbox SET state = 'pending', attempts = MAX(attempts - 1, 0), "
                "next_attempt_at = NULL, last_error = 'wake_rebound', updated_at = ? "
                "WHERE message_id = ? AND state = 'delivered' AND delivery_turn_id = ?",
                (now, mid, turn_id),
            )
            result.returned.append(mid)
        elif status == "completed":
            conn.execute(
                "UPDATE agent_inbox SET state = 'acked', acked_at = ?, resolution = 'wake', updated_at = ? "
                "WHERE message_id = ? AND state = 'delivered' AND delivery_turn_id = ?",
                (now, now, mid, turn_id),
            )
            result.acked.append(mid)
        elif attempts >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE agent_inbox SET state = 'dead', last_error = 'attempts_exhausted', updated_at = ? "
                "WHERE message_id = ? AND state = 'delivered' AND delivery_turn_id = ?",
                (now, mid, turn_id),
            )
            result.dead.append(mid)
        else:
            conn.execute(
                "UPDATE agent_inbox SET state = 'pending', next_attempt_at = ?, last_error = ?, updated_at = ? "
                "WHERE message_id = ? AND state = 'delivered' AND delivery_turn_id = ?",
                (_backoff_until(now, attempts), f"wake_{status}", now, mid, turn_id),
            )
            result.returned.append(mid)
    return result


def inherit_retry(conn: sqlite3.Connection, failed_turn_id: str, retry_turn_id: str, now: str) -> int:
    """A retry R of a failed wake A (quota / transient recovery) re-delivers A's
    prompt, so R carries A's messages that went back to pending at A's failure
    (one admission ⇒ attempts+1) — otherwise the inbox would admit a second wake
    for the same completions next to R."""
    cur = conn.execute(
        "UPDATE agent_inbox SET state = 'delivered', delivery_turn_id = ?, delivered_at = ?, "
        "attempts = attempts + 1, updated_at = ? "
        "WHERE delivery_turn_id = ? AND state = 'pending' AND attempts < ?",
        (retry_turn_id, now, now, failed_turn_id, MAX_ATTEMPTS),
    )
    return cur.rowcount


def ack_about_task(conn: sqlite3.Connection, task_id: str, now: str, *, reason: str) -> int:
    """A tagged review of ``task_id`` consumes its message without a wake."""
    cur = conn.execute(
        "UPDATE agent_inbox SET state = 'acked', acked_at = ?, resolution = ?, updated_at = ? "
        "WHERE about_task_id = ? AND state IN ('pending', 'delivered')",
        (now, reason, now, task_id),
    )
    return cur.rowcount


def kill_case(conn: sqlite3.Connection, case_id: str, reason: str, now: str) -> int:
    """A closed Case's undelivered messages are moot: dead, and NOT alertable
    (closing was a deliberate decision — I5 alerts only on delivery failure)."""
    cur = conn.execute(
        "UPDATE agent_inbox SET state = 'dead', last_error = ?, updated_at = ?, alerted_at = ? "
        "WHERE case_id = ? AND state IN ('pending', 'delivered')",
        (reason, now, now, case_id),
    )
    return cur.rowcount


def kill_recipient(conn: sqlite3.Connection, recipient_session_id: str, reason: str, now: str) -> list[str]:
    ids = [str(r["message_id"]) for r in conn.execute(
        "SELECT message_id FROM agent_inbox WHERE recipient_session_id = ? AND state = 'pending'",
        (recipient_session_id,),
    ).fetchall()]
    if ids:
        conn.execute(
            "UPDATE agent_inbox SET state = 'dead', last_error = ?, updated_at = ? "
            f"WHERE state = 'pending' AND message_id IN ({_placeholders(len(ids))})",
            (reason, now, *ids),
        )
    return ids


def readdress(conn: sqlite3.Connection, from_session_id: str, to_session_id: str, now: str) -> int:
    """D4: move undelivered messages of a replaced recipient to its successor."""
    if not to_session_id or to_session_id == from_session_id:
        return 0
    cur = conn.execute(
        "UPDATE agent_inbox SET recipient_session_id = ?, updated_at = ? "
        "WHERE recipient_session_id = ? AND state = 'pending'",
        (to_session_id, now, from_session_id),
    )
    return cur.rowcount


def successor_of(conn: sqlite3.Connection, session_id: str, max_hops: int = 8) -> Optional[str]:
    """Follow recorded lineage (``sessions.continued_from``) to the newest live
    successor of ``session_id``; None when there is none. Role-free."""
    current, seen = session_id, {session_id}
    for _ in range(max_hops):
        row = conn.execute(
            "SELECT session_id FROM sessions INDEXED BY idx_sessions_continued_from "
            "WHERE continued_from = ? ORDER BY created_at DESC, session_id DESC LIMIT 1",
            (current,),
        ).fetchone()
        if row is None or row["session_id"] in seen:
            break
        current = str(row["session_id"])
        seen.add(current)
    return None if current == session_id else current


def arm_filter(
    conn: sqlite3.Connection, case_id: str, filter_id: str, condition: str,
    member_task_ids: list[str], now: str,
) -> None:
    """D2: store an ALL/ANY delivery filter with the inbox (ANY holds nothing)."""
    members = sorted({str(m) for m in member_task_ids if m})[:FILTER_MEMBER_LIMIT]
    conn.execute(
        "INSERT INTO inbox_wait_filters (case_id, filter_id, condition, member_task_ids, created_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(case_id, filter_id) DO UPDATE SET "
        "condition = excluded.condition, member_task_ids = excluded.member_task_ids",
        (case_id, filter_id, condition.upper(), json.dumps(members), now),
    )


def list_filters(conn: sqlite3.Connection, case_id: str) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for f in conn.execute(
        "SELECT filter_id, condition, member_task_ids, created_at FROM inbox_wait_filters "
        "WHERE case_id = ? ORDER BY created_at, filter_id",
        (case_id,),
    ).fetchall():
        members = [str(m) for m in json.loads(f["member_task_ids"] or "[]")]
        live = conn.execute(
            f"SELECT COUNT(*) FROM mesh_tasks WHERE id IN ({_placeholders(len(members) or 1)}) "
            "AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required')",
            members or [""],
        ).fetchone()[0]
        out.append({
            "wait_group_id": f["filter_id"], "condition": f["condition"], "members": members,
            "satisfied": int(live) == 0 if f["condition"] == "ALL" else int(live) < len(members),
            "created_at": f["created_at"],
        })
    return out


# --------------------------------------------------------------------------- #
# Delivery support (A104 Gate 3)
# --------------------------------------------------------------------------- #
def wake_turn_id(recipient_session_id: str, messages: list[InboxMessage]) -> str:
    """Deterministic wake turn id for one delivery attempt of ``messages``: a
    racing producer computes the same id (the admission dedups) and a new
    attempt (attempts advanced) gets a new id."""
    import hashlib

    basis = "\0".join(
        [recipient_session_id] + sorted(f"{m.message_id}#{m.attempts}" for m in messages)
    )
    return "wake_" + hashlib.sha256(basis.encode()).hexdigest()[:24]


def waiting_for(conn: sqlite3.Connection, recipient_session_ids: list[str]) -> dict[str, bool]:
    """Batched projection of ``pending_for(sid).waiting()`` for many sessions (the
    session list) — same predicates, two grouped index-served reads, no N+1."""
    sids = sorted({s for s in recipient_session_ids if s})
    out: dict[str, bool] = {s: False for s in sids}
    if not sids:
        return out
    marks = _placeholders(len(sids))
    for r in conn.execute(
        "SELECT DISTINCT recipient_session_id FROM agent_inbox INDEXED BY idx_agent_inbox_pending "
        f"WHERE recipient_session_id IN ({marks}) AND state IN ('pending', 'delivered') AND state = 'pending'",
        sids,
    ).fetchall():
        out[str(r[0])] = True
    for r in conn.execute(
        "SELECT DISTINCT sender_session_id FROM mesh_tasks INDEXED BY idx_mesh_tasks_requester_open "
        f"WHERE sender_session_id IN ({marks}) "
        "AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required')",
        sids,
    ).fetchall():
        out[str(r[0])] = True
    return out


def rebound_wake_turns(conn: sqlite3.Connection, case_id: str) -> list[str]:
    """Queued wake turns of ``case_id`` whose recipient's Manager seat was rebound
    (a newer seat link exists): they must be withdrawn so their messages follow."""
    rows = conn.execute(
        "SELECT DISTINCT t.id, t.session_id FROM agent_inbox i INDEXED BY idx_agent_inbox_case "
        "JOIN mesh_tasks t ON t.id = i.delivery_turn_id "
        "WHERE i.case_id = ? AND i.state = 'delivered' AND t.status = 'queued'",
        (case_id,),
    ).fetchall()
    if not rows:
        return []
    current = conn.execute(
        "SELECT entity_id FROM flow_links WHERE flow_run_id = ? AND entity_type = 'session' "
        "AND role = 'manager' ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    seats = {str(r[0]) for r in conn.execute(
        "SELECT entity_id FROM flow_links WHERE flow_run_id = ? AND entity_type = 'session' "
        "AND role = 'manager'", (case_id,),
    ).fetchall()}
    holder = str(current["entity_id"]) if current is not None else ""
    return [str(r["id"]) for r in rows if str(r["session_id"]) in seats and str(r["session_id"]) != holder]


def has_rebound_pending(conn: sqlite3.Connection, case_id: str) -> bool:
    """Read-only probe: does a pending message of ``case_id`` sit with a replaced
    Manager-seat holder?"""
    return conn.execute(
        "SELECT 1 FROM agent_inbox WHERE case_id = ? AND state = 'pending' "
        "AND recipient_session_id IN (SELECT entity_id FROM flow_links WHERE flow_run_id = ? "
        "AND entity_type = 'session' AND role = 'manager') "
        "AND recipient_session_id != COALESCE((SELECT entity_id FROM flow_links WHERE flow_run_id = ? "
        "AND entity_type = 'session' AND role = 'manager' ORDER BY id DESC LIMIT 1), '') LIMIT 1",
        (case_id, case_id, case_id),
    ).fetchone() is not None


def follow_case_rebind(conn: sqlite3.Connection, case_id: str, now: str) -> int:
    """D4 (rebind record): when a Case's Manager seat was rebound, the pending
    messages of THAT Case addressed to a replaced seat holder follow the seat to
    its current holder. Reads the rebind record (the newest seat link), never
    decides addressing for a fresh message."""
    current = conn.execute(
        "SELECT entity_id FROM flow_links WHERE flow_run_id = ? AND entity_type = 'session' "
        "AND role = 'manager' ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if current is None:
        return 0
    cur = conn.execute(
        "UPDATE agent_inbox SET recipient_session_id = ?, updated_at = ? "
        "WHERE case_id = ? AND state = 'pending' AND recipient_session_id != ? "
        "AND recipient_session_id IN (SELECT entity_id FROM flow_links WHERE flow_run_id = ? "
        "AND entity_type = 'session' AND role = 'manager')",
        (current["entity_id"], now, case_id, current["entity_id"], case_id),
    )
    return cur.rowcount


def rounds_used(conn: sqlite3.Connection, case_id: str) -> int:
    """Completed wake rounds of a Case = distinct wake turns that acked a message."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT delivery_turn_id) FROM agent_inbox "
        "WHERE case_id = ? AND state = 'acked' AND resolution = 'wake'",
        (case_id,),
    ).fetchone()
    return int(row[0] or 0)


def sweep_settled(conn: sqlite3.Connection, now: str, limit: int = PENDING_LIMIT) -> SettleResult:
    """Crash-safe backstop: settle messages still ``delivered`` on a wake turn that
    is already terminal (a terminal path that did not settle in its own txn)."""
    total = SettleResult()
    for r in conn.execute(
        "SELECT DISTINCT i.delivery_turn_id, t.status FROM agent_inbox i INDEXED BY idx_agent_inbox_pending "
        "JOIN mesh_tasks t ON t.id = i.delivery_turn_id "
        "WHERE i.state IN ('pending', 'delivered') AND i.state = 'delivered' "
        "AND t.status NOT IN ('queued', 'pending', 'claimed', 'running', 'recovery_required') LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall():
        part = settle_turn(conn, str(r["delivery_turn_id"]), str(r["status"]), now)
        total.acked += part.acked
        total.returned += part.returned
        total.dead += part.dead
    return total


def unalerted_dead(conn: sqlite3.Connection, limit: int = 50) -> list[InboxMessage]:
    rows = conn.execute(
        "SELECT message_id, recipient_session_id, sender_session_id, about_task_id, case_id, kind, outcome, "
        "state, attempts, next_attempt_at, delivery_turn_id, created_at, last_error "
        "FROM agent_inbox INDEXED BY idx_agent_inbox_dead_unalerted "
        "WHERE state = 'dead' AND alerted_at IS NULL ORDER BY updated_at LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [
        InboxMessage(
            message_id=r["message_id"], recipient_session_id=r["recipient_session_id"],
            sender_session_id=r["sender_session_id"], about_task_id=r["about_task_id"],
            case_id=r["case_id"], kind=r["kind"], outcome=r["outcome"], state=r["state"],
            attempts=int(r["attempts"] or 0), ready_at=r["next_attempt_at"],
            delivery_turn_id=r["delivery_turn_id"], created_at=r["created_at"],
            last_error=r["last_error"],
        )
        for r in rows
    ]


def mark_alerted(conn: sqlite3.Connection, message_ids: list[str], now: str) -> int:
    if not message_ids:
        return 0
    cur = conn.execute(
        f"UPDATE agent_inbox SET alerted_at = ? WHERE alerted_at IS NULL AND message_id IN ({_placeholders(len(message_ids))})",
        (now, *message_ids),
    )
    return cur.rowcount


def case_pending(conn: sqlite3.Connection, case_id: str) -> PendingView:
    """What is waiting in one Case, for every agent addressed in it: the union of
    ``pending_for(recipient, case_id=case_id)`` over the Case's recipients (the
    addressees of its open messages and the requesters of its open children).
    Composed from ``pending_for`` — not a second read of pending state."""
    recipients = sorted({str(r[0]) for r in conn.execute(
        "SELECT DISTINCT recipient_session_id FROM agent_inbox WHERE case_id = ? "
        "AND state IN ('pending', 'delivered') "
        "UNION SELECT DISTINCT sender_session_id FROM mesh_tasks WHERE flow_run_id = ? "
        "AND sender_session_id IS NOT NULL "
        "AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required')",
        (case_id, case_id),
    ).fetchall() if r[0]})
    merged = PendingView(recipient_session_id="*", case_id=case_id)
    for sid in recipients[:PENDING_LIMIT]:
        view = pending_for(conn, sid, case_id=case_id)
        merged.messages.extend(view.messages)
        merged.outstanding_task_ids.extend(view.outstanding_task_ids)
    return merged
