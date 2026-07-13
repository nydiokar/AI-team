"""Pure task/job state derivation from already-fetched durable rows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

TaskTruthState = Literal[
    "accepted",
    "queued",
    "claimed",
    "worker_running",
    "backend_running",
    "waiting_for_input",
    "waiting_for_approval",
    "cancel_requested",
    "cancelled",
    "completed",
    "failed",
    "detached",
    "stale_claim",
    "worker_unknown",
    "recovered",
    "driver_lost",
]

TruthConfidence = Literal["high", "medium", "low"]
TruthSource = Literal[
    "mesh_task_terminal",
    "worker_live_state",
    "mesh_task_claim",
    "telemetry_turn",
    "stale_claim_evidence",
    "mesh_task_status",
    "job_row",
]


class DerivedExecutionState(BaseModel):
    state: TaskTruthState
    confidence: TruthConfidence
    reason: str
    authoritative_source: TruthSource
    observed_at: str | None = None
    stale_after: str | None = None
    raw_refs: dict[str, str] = Field(default_factory=dict)


class DerivedJobState(BaseModel):
    state: Literal["running", "done", "failed", "lost"]
    confidence: TruthConfidence
    reason: str
    authoritative_source: Literal["job_row"] = "job_row"
    observed_at: str | None = None
    raw_refs: dict[str, str] = Field(default_factory=dict)


def derive_task_execution_state(
    task_row: dict[str, object],
    *,
    session_row: dict[str, object] | None = None,
    node_row: dict[str, object] | None = None,
    telemetry_turn: dict[str, object] | None = None,
    approval_pending: bool = False,
    cancel_requested: bool = False,
    recovery_evidence: bool = False,
    now: datetime | None = None,
    live_state_max_age_sec: int = 90,
    claim_lease_sec: int = 300,
) -> DerivedExecutionState:
    """Derive honest task execution state without querying external resources."""
    current_time: datetime = _to_naive_utc(now or datetime.now(tz=timezone.utc))
    task_id: str = _text(task_row.get("id"))
    status: str = _text(task_row.get("status"))
    raw_refs: dict[str, str] = {"task_id": task_id} if task_id else {}
    if node_row and _text(node_row.get("node_id")):
        raw_refs["node_id"] = _text(node_row.get("node_id"))
    if telemetry_turn and _text(telemetry_turn.get("turn_id")):
        raw_refs["turn_id"] = _text(telemetry_turn.get("turn_id"))
    if session_row and _text(session_row.get("session_id")):
        raw_refs["session_id"] = _text(session_row.get("session_id"))

    if status == "completed":
        return DerivedExecutionState(
            state="recovered" if recovery_evidence else "completed",
            confidence="high",
            reason=(
                "mesh task completed and recovery evidence is present"
                if recovery_evidence
                else "mesh task has a terminal completed result"
            ),
            authoritative_source="mesh_task_terminal",
            observed_at=_first_text(task_row, "completed_at", "updated_at"),
            raw_refs=raw_refs,
        )
    if status in {"failed", "failed_node_offline"}:
        return DerivedExecutionState(
            state="failed",
            confidence="high",
            reason=f"mesh task has terminal status {status}",
            authoritative_source="mesh_task_terminal",
            observed_at=_first_text(task_row, "completed_at", "updated_at"),
            raw_refs=raw_refs,
        )
    if status == "cancelled":
        return DerivedExecutionState(
            state="cancelled",
            confidence="high",
            reason="mesh task has terminal cancelled status",
            authoritative_source="mesh_task_terminal",
            observed_at=_first_text(task_row, "completed_at", "updated_at"),
            raw_refs=raw_refs,
        )
    if cancel_requested or status == "cancel_requested":
        return DerivedExecutionState(
            state="cancel_requested",
            confidence="high" if cancel_requested else "medium",
            reason="cancellation has been requested but no terminal cancellation exists",
            authoritative_source="mesh_task_status",
            observed_at=_first_text(task_row, "updated_at", "created_at"),
            raw_refs=raw_refs,
        )
    if session_row is not None and _text(session_row.get("driver_status")) == "lost":
        return DerivedExecutionState(
            state="driver_lost",
            confidence="high",
            reason=(
                "owning session's continuous driver was lost (e.g. after a restart) "
                "and is not resumable as-is; needs re-invoke"
            ),
            authoritative_source="mesh_task_status",
            observed_at=_first_text(session_row, "updated_at", "created_at"),
            raw_refs=raw_refs,
        )
    if approval_pending:
        return DerivedExecutionState(
            state="waiting_for_approval",
            confidence="high",
            reason="a pending approval gate is associated with this task/session",
            authoritative_source="mesh_task_status",
            observed_at=_first_text(task_row, "updated_at", "created_at"),
            raw_refs=raw_refs,
        )
    if session_row is not None and _text(session_row.get("status")) == "awaiting_input":
        return DerivedExecutionState(
            state="waiting_for_input",
            confidence="high",
            reason="owning session is awaiting input",
            authoritative_source="mesh_task_status",
            observed_at=_first_text(session_row, "updated_at", "created_at"),
            raw_refs=raw_refs,
        )
    if status == "accepted":
        return DerivedExecutionState(
            state="accepted",
            confidence="high",
            reason="gateway accepted the task before queue placement",
            authoritative_source="mesh_task_status",
            observed_at=_first_text(task_row, "updated_at", "created_at"),
            raw_refs=raw_refs,
        )
    if status == "pending":
        return DerivedExecutionState(
            state="queued",
            confidence="high",
            reason="mesh task is pending in the durable queue",
            authoritative_source="mesh_task_status",
            observed_at=_first_text(task_row, "updated_at", "created_at"),
            raw_refs=raw_refs,
        )

    if status == "claimed":
        claimed_at: datetime | None = _parse_dt(task_row.get("claimed_at"))
        claim_stale_after: str | None = _add_seconds(claimed_at, claim_lease_sec)

        if node_row is None:
            return DerivedExecutionState(
                state="worker_unknown",
                confidence="medium",
                reason="task is claimed but the claiming node row is missing",
                authoritative_source="stale_claim_evidence",
                observed_at=_text(task_row.get("claimed_at")) or None,
                stale_after=claim_stale_after,
                raw_refs=raw_refs,
            )

        node_status: str = _text(node_row.get("status"))
        if node_status == "offline":
            return DerivedExecutionState(
                state="worker_unknown",
                confidence="medium",
                reason="task is claimed by a node marked offline",
                authoritative_source="stale_claim_evidence",
                observed_at=_first_text(node_row, "updated_at", "last_heartbeat"),
                stale_after=claim_stale_after,
                raw_refs=raw_refs,
            )

        claimer_incarnation: str = _text(task_row.get("claimer_incarnation"))
        node_incarnation: str = _text(node_row.get("incarnation_id"))
        if claimer_incarnation and node_incarnation and claimer_incarnation != node_incarnation:
            return DerivedExecutionState(
                state="stale_claim",
                confidence="high",
                reason="claim incarnation does not match the current node incarnation",
                authoritative_source="stale_claim_evidence",
                observed_at=_first_text(node_row, "updated_at", "last_heartbeat"),
                stale_after=claim_stale_after,
                raw_refs=raw_refs,
            )

        live_state: dict[str, object] | None = _live_state(node_row.get("live_state"))
        live_updated: datetime | None = _parse_dt(node_row.get("live_state_updated_at"))
        heartbeat_at: datetime | None = _parse_dt(node_row.get("last_heartbeat"))
        live_fresh: bool = (
            live_state is not None
            and live_updated is not None
            and (current_time - live_updated).total_seconds() <= live_state_max_age_sec
        )
        heartbeat_fresh: bool = (
            heartbeat_at is not None
            and (current_time - heartbeat_at).total_seconds() <= live_state_max_age_sec
        )

        if live_fresh and _live_state_has_task(live_state, task_id):
            return DerivedExecutionState(
                state="worker_running",
                confidence="high",
                reason="fresh worker live_state lists this task as active",
                authoritative_source="worker_live_state",
                observed_at=_text(node_row.get("live_state_updated_at")) or None,
                stale_after=_add_seconds(live_updated, live_state_max_age_sec),
                raw_refs=raw_refs,
            )
        if live_fresh:
            return DerivedExecutionState(
                state="detached",
                confidence="high",
                reason="fresh worker live_state does not include the claimed task",
                authoritative_source="stale_claim_evidence",
                observed_at=_text(node_row.get("live_state_updated_at")) or None,
                stale_after=_add_seconds(live_updated, live_state_max_age_sec),
                raw_refs=raw_refs,
            )
        if not heartbeat_fresh:
            return DerivedExecutionState(
                state="worker_unknown",
                confidence="medium",
                reason="claim exists but worker heartbeat/live_state proof is stale or missing",
                authoritative_source="stale_claim_evidence",
                observed_at=_first_text(node_row, "live_state_updated_at", "last_heartbeat"),
                stale_after=claim_stale_after,
                raw_refs=raw_refs,
            )

        if claimed_at is not None and (current_time - claimed_at).total_seconds() <= claim_lease_sec:
            return DerivedExecutionState(
                state="claimed",
                confidence="medium",
                reason="fresh claim matches the current node incarnation but lacks active task proof",
                authoritative_source="mesh_task_claim",
                observed_at=_text(task_row.get("claimed_at")) or None,
                stale_after=claim_stale_after,
                raw_refs=raw_refs,
            )

        return DerivedExecutionState(
            state="stale_claim",
            confidence="medium",
            reason="claim lease expired without fresh active task proof",
            authoritative_source="stale_claim_evidence",
            observed_at=_text(task_row.get("claimed_at")) or None,
            stale_after=claim_stale_after,
            raw_refs=raw_refs,
        )

    if telemetry_turn is not None:
        final_status: str = _text(telemetry_turn.get("final_status"))
        if final_status in {"queued", "running"}:
            return DerivedExecutionState(
                state="backend_running",
                confidence="low",
                reason="telemetry projection is running but no stronger task/worker proof exists",
                authoritative_source="telemetry_turn",
                observed_at=_first_text(telemetry_turn, "updated_at", "started_at"),
                raw_refs=raw_refs,
            )
        if final_status == "success":
            return DerivedExecutionState(
                state="completed",
                confidence="low",
                reason="telemetry projection completed successfully but mesh task is not terminal",
                authoritative_source="telemetry_turn",
                observed_at=_first_text(telemetry_turn, "ended_at", "updated_at"),
                raw_refs=raw_refs,
            )
        if final_status == "failed":
            return DerivedExecutionState(
                state="failed",
                confidence="low",
                reason="telemetry projection failed but mesh task is not terminal",
                authoritative_source="telemetry_turn",
                observed_at=_first_text(telemetry_turn, "ended_at", "updated_at"),
                raw_refs=raw_refs,
            )

    return DerivedExecutionState(
        state="worker_unknown",
        confidence="low",
        reason=f"unmapped mesh task status {status or '(missing)'}",
        authoritative_source="mesh_task_status",
        observed_at=_first_text(task_row, "updated_at", "created_at"),
        raw_refs=raw_refs,
    )


def derive_job_execution_state(job_row: dict[str, object]) -> DerivedJobState:
    status: str = _text(job_row.get("status"))
    state: Literal["running", "done", "failed", "lost"]
    if status in {"done", "failed", "lost"}:
        state = status  # type: ignore[assignment]
    else:
        state = "running"
    return DerivedJobState(
        state=state,
        confidence="high",
        reason="watched job state comes from the durable jobs row",
        observed_at=_first_text(job_row, "finished_at", "updated_at", "started_at"),
        raw_refs={
            key: value
            for key, value in {
                "job_id": _text(job_row.get("id")),
                "session_id": _text(job_row.get("session_id")),
                "node_id": _text(job_row.get("node_id")),
            }.items()
            if value
        },
    )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _first_text(row: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value: str = _text(row.get(key))
        if value:
            return value
    return None


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _to_naive_utc(value)
    try:
        parsed: datetime = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _to_naive_utc(parsed)


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _add_seconds(value: datetime | None, seconds: int) -> str | None:
    if value is None:
        return None
    from datetime import timedelta

    return (value + timedelta(seconds=max(0, int(seconds)))).isoformat()


def _live_state(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded: object = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _live_state_has_task(live_state: dict[str, object], task_id: str) -> bool:
    active = live_state.get("active_tasks")
    if isinstance(active, list) and task_id in {str(item) for item in active}:
        return True
    details = live_state.get("active_task_details")
    if isinstance(details, dict) and task_id in {str(key) for key in details.keys()}:
        return True
    if isinstance(details, list):
        return any(
            isinstance(item, dict) and str(item.get("task_id") or "") == task_id
            for item in details
        )
    return False
