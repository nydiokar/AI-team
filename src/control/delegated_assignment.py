"""Delegation provenance and typed worker dispositions in controller transactions."""
from __future__ import annotations

import hmac
import json
import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .turn_queue import OwnershipConflictError, ScopeForbiddenError

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
EvidenceReference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
PROGRESS = "assignment_progress"
REPORT = "assignment_report"
CONTINUE = "assignment_continue"
FINAL = ("completed", "failed", "decision_needed")


class Disposition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: Identifier
    node_id: str = Field(min_length=1, max_length=128)
    turn_id: Identifier
    turn_proof: Identifier = Field(exclude=True, repr=False)
    disposition: Literal["yield", "continue", "completed", "failed", "decision_needed"]
    summary: str = Field(min_length=1, max_length=2048)
    job_id: Identifier | None = None
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=6)
    limitations: str = Field(default="", max_length=1000)
    next_action: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_disposition(self) -> Disposition:
        if (self.disposition == "yield") != bool(self.job_id):
            raise ValueError("job_id is required for yield")
        if self.disposition == "completed" and not self.evidence:
            raise ValueError("completed requires evidence references")
        if self.disposition in ("failed", "decision_needed") and not self.next_action:
            raise ValueError("failed and decision_needed require next_action")
        return self


def worker_context(turn_id: str, expected: str) -> str:
    return (
        f"Durable delegated assignment. Controller turn_id: {turn_id}.\n"
        f"Expected deliverable: {expected}\n"
        "Pass the current turn_id and turn_proof on assignment tool calls. "
        "Use watch_job with a stable operation_id to launch detached work. "
        "Before ending, call assignment_disposition. Use yield with the job_id "
        "and next step, continue with a checkpoint for another analysis turn, completed with "
        "findings and evidence references, or failed / decision_needed with the problem and "
        "next_action. Keep ownership of follow-up jobs and interpret their results."
    )


def enqueue_message(
    conn: sqlite3.Connection, assignment: sqlite3.Row, kind: str, identity: str,
    outcome: str, now: str, *, worker: bool = False,
) -> None:
    recipient = assignment["worker_session_id"] if worker else assignment["requester_session_id"]
    if not worker:
        # Follow the existing Case seat-rebind convention without changing the
        # original requester preserved on the assignment and each owned turn.
        seat = conn.execute(
            "SELECT entity_id FROM flow_links WHERE flow_run_id=? AND entity_type='session' "
            "AND role='manager' ORDER BY id DESC LIMIT 1", (assignment["case_id"],),
        ).fetchone()
        if seat:
            recipient = seat["entity_id"]
    conn.execute(
        "INSERT INTO agent_inbox (message_id, recipient_session_id, sender_session_id, "
        "about_task_id, case_id, kind, outcome, state, attempts, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,'pending',0,?,?) ON CONFLICT(message_id) DO NOTHING",
        (f"{kind}:{identity}", recipient, assignment["worker_session_id"],
         assignment["assignment_id"], assignment["case_id"], kind, outcome, now, now),
    )


def turn_proof(claim_token: str) -> str:
    return hmac.digest(claim_token.encode(), b"delegated-assignment/v1", "sha256").hex()


def require_proof(claim_token: str | None, proof: str) -> None:
    if not claim_token or not hmac.compare_digest(proof, turn_proof(claim_token)):
        raise ScopeForbiddenError("assignment tool proof does not match the turn claim")


def active_owner(
    conn: sqlite3.Connection, session_id: str, node_id: str, turn_id: str, proof: str,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT a.*, t.claim_token FROM mesh_tasks t JOIN delegated_assignments a USING(assignment_id) "
        "JOIN flow_runs f ON f.flow_run_id=a.case_id "
        "JOIN nodes n ON n.node_id=t.claimed_by AND n.incarnation_id=t.claim_incarnation "
        "JOIN sessions s ON s.session_id=t.session_id "
        "WHERE t.id=? AND t.queue_protocol=1 AND t.status='running' "
        "AND t.session_id=? AND t.claimed_by=? AND t.claim_token IS NOT NULL "
        "AND (t.cancel_token IS NULL OR t.cancel_token<>t.claim_token) AND t.flow_run_id=a.case_id "
        "AND a.active_turn_id=t.id AND a.worker_session_id=t.session_id "
        "AND COALESCE(f.status,'') NOT IN ('closed','cancelled') AND s.status NOT IN ('closed','cancelled')",
        (turn_id, session_id, node_id),
    ).fetchone()
    if row is None:
        raise ScopeForbiddenError("turn is not the current running owner of this assignment")
    require_proof(row["claim_token"], proof)
    return row


def settle_turn(conn: sqlite3.Connection, turn_id: str, status: str, now: str) -> bool:
    row = conn.execute(
        "SELECT a.* FROM mesh_tasks t "
        "JOIN delegated_assignments a USING(assignment_id) WHERE t.id=?", (turn_id,),
    ).fetchone()
    if row is None:
        return False
    if row["active_turn_id"] != turn_id or row["state"] in (*FINAL, "cancelled"):
        return True
    if status == "completed" and row["state"] == "waiting":
        return True
    if status == "completed" and row["state"] == "continuing":
        enqueue_message(conn, row, CONTINUE, turn_id, "continue", now, worker=True)
        return True
    reason = "missing_disposition" if status == "completed" else f"worker_turn_{status}"
    if status != "completed":
        conn.execute(
            "UPDATE agent_inbox SET state='acked', acked_at=?, resolution='assignment_recovery', updated_at=? "
            "WHERE kind=? AND delivery_turn_id=? AND state='delivered'",
            (now, now, CONTINUE, turn_id),
        )
    conn.execute(
        "UPDATE delegated_assignments SET state='unresolved', issue=?, updated_at=? WHERE assignment_id=?",
        (reason, now, row["assignment_id"]),
    )
    enqueue_message(conn, row, PROGRESS, turn_id, reason, now)
    # Give a missing checkpoint one bounded same-worker repair opportunity.
    # A second missing disposition stays visible and requires intervention.
    if (reason == "missing_disposition" or status == "failed") and not row["recovery_used"]:
        conn.execute(
            "UPDATE delegated_assignments SET recovery_used=1 WHERE assignment_id=?",
            (row["assignment_id"],),
        )
        enqueue_message(conn, row, CONTINUE, turn_id, reason, now, worker=True)
    return True


def read_case(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT a.*, j.status AS job_status FROM delegated_assignments a "
        "LEFT JOIN jobs j ON j.id=a.job_id WHERE a.case_id=? "
        "ORDER BY (a.state IN ('completed','failed','decision_needed','cancelled')), "
        "a.updated_at DESC LIMIT 64", (case_id,),
    )
    return [
        {**dict(row), "report": json.loads(row["report"]) if row["report"] else None}
        for row in rows
    ]


def require_reviewable(conn: sqlite3.Connection, case_id: str, task_id: str, event_type: str) -> None:
    assignment = conn.execute(
        "SELECT case_id,state FROM delegated_assignments WHERE assignment_id=?", (task_id,),
    ).fetchone()
    if assignment is not None and (
        assignment["case_id"] != case_id or assignment["state"] not in FINAL
        or (event_type == "review.accepted" and assignment["state"] != "completed")
    ):
        raise OwnershipConflictError("assignment requires a matching final report before review")
