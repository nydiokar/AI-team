"""Durable session timeline read model.

This module is intentionally pure over already-selected repository objects:
MeshDB, TelemetryStore, and a session row/view supplied by the caller. It does
not read SSE logs and does not scan the filesystem.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.core.task_state_truth import (
    derive_job_execution_state,
    derive_task_execution_state,
)

TimelineKind = Literal[
    "task_state",
    "worker_state",
    "turn_event",
    "artifact",
    "file_change",
    "job_state",
    "approval",
    "recovery",
    "system_notice",
]


class SessionTimelineItem(BaseModel):
    id: str
    kind: TimelineKind
    source: str
    durability: Literal["durable", "volatile", "unavailable"] = "durable"
    timestamp: str
    session_id: str
    task_id: str | None = None
    turn_id: str | None = None
    job_id: str | None = None
    node_id: str | None = None
    backend: str | None = None
    status: str | None = None
    confidence: str | None = None
    staleness: str | None = None
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)
    raw_refs: dict[str, str] = Field(default_factory=dict)


class SessionTimelineResponse(BaseModel):
    items: list[SessionTimelineItem]
    next_cursor: str | None
    generated_at: str
    coverage: dict[str, str]


def build_session_timeline(
    *,
    db: Any,
    telemetry_store: Any | None,
    session_id: str,
    session_row: dict[str, object] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> SessionTimelineResponse:
    bounded_limit: int = max(1, min(int(limit), 200))
    offset: int = _cursor_offset(cursor)
    source_limit: int = max(50, min(500, offset + bounded_limit + 50))
    generated_at: str = datetime.now(tz=timezone.utc).isoformat()

    tasks: list[dict[str, object]] = []
    nodes: list[dict[str, object]] = []
    jobs: list[dict[str, object]] = []
    approvals: list[dict[str, object]] = []
    turns: list[dict[str, object]] = []
    coverage: dict[str, str] = {
        "tasks": "unavailable",
        "telemetry": "unavailable",
        "jobs": "unavailable",
        "artifacts": "unavailable",
        "approvals": "unavailable",
    }

    if db is not None:
        tasks = _safe_list(lambda: db.list_tasks(session_id=session_id, limit=source_limit))
        nodes = _safe_list(lambda: db.list_nodes())
        jobs = _safe_list(lambda: db.list_jobs(session_id=session_id, limit=source_limit))
        approvals = _safe_list(
            lambda: db.list_approvals(session_id=session_id, limit=source_limit)
        )
        coverage["tasks"] = "complete"
        coverage["jobs"] = "complete"
        coverage["artifacts"] = "complete"
        coverage["approvals"] = "complete"

    if telemetry_store is not None:
        turns = _safe_list(
            lambda: telemetry_store.list_turns(session_id=session_id, limit=source_limit)
        )
        coverage["telemetry"] = _telemetry_coverage(turns)

    nodes_by_id: dict[str, dict[str, object]] = {
        str(node.get("node_id")): node
        for node in nodes
        if node.get("node_id")
    }
    turns_by_task: dict[str, dict[str, object]] = {
        str(turn.get("task_id") or turn.get("turn_id")): turn
        for turn in turns
        if turn.get("task_id") or turn.get("turn_id")
    }
    pending_approval_task_ids: set[str] = {
        str(row.get("task_id"))
        for row in approvals
        if row.get("status") == "pending" and row.get("task_id")
    }

    items: list[SessionTimelineItem] = []
    for task in tasks:
        task_id: str = str(task.get("id") or "")
        node_id: str = str(task.get("claimed_by") or task.get("machine_id") or "")
        derived = derive_task_execution_state(
            task,
            session_row=session_row,
            node_row=nodes_by_id.get(node_id),
            telemetry_turn=turns_by_task.get(task_id),
            approval_pending=task_id in pending_approval_task_ids,
        )
        items.append(
            SessionTimelineItem(
                id=f"task:{task_id}:state",
                kind="task_state",
                source=derived.authoritative_source,
                timestamp=_timestamp(
                    derived.observed_at,
                    task.get("updated_at"),
                    task.get("created_at"),
                ),
                session_id=session_id,
                task_id=task_id,
                node_id=node_id or None,
                backend=_text(task.get("backend")) or None,
                status=derived.state,
                confidence=derived.confidence,
                staleness=derived.reason if derived.state in {"stale_claim", "worker_unknown", "detached"} else None,
                summary=f"Task {derived.state.replace('_', ' ')}",
                detail={
                    "reason": derived.reason,
                    "stale_after": derived.stale_after,
                    "action": task.get("action"),
                    "mesh_status": task.get("status"),
                },
                raw_refs=derived.raw_refs,
            )
        )
        file_changes: list[Any] = _loads_list(task.get("file_changes_json"))
        files_modified: list[Any] = _loads_list(task.get("files_modified_json"))
        file_count: int = len(file_changes) if file_changes else len(files_modified)
        if file_count:
            items.append(
                SessionTimelineItem(
                    id=f"task:{task_id}:artifact",
                    kind="artifact",
                    source="mesh_tasks",
                    timestamp=_timestamp(task.get("completed_at"), task.get("updated_at"), task.get("created_at")),
                    session_id=session_id,
                    task_id=task_id,
                    backend=_text(task.get("backend")) or None,
                    status="available",
                    confidence="high",
                    summary=f"{file_count} file{'s' if file_count != 1 else ''} changed",
                    detail={"file_count": file_count, "files_modified": files_modified[:20]},
                    raw_refs={"task_id": task_id},
                )
            )

    for turn in turns:
        turn_id: str = str(turn.get("turn_id") or "")
        items.append(
            SessionTimelineItem(
                id=f"turn:{turn_id}",
                kind="turn_event",
                source="llm_turns",
                timestamp=_timestamp(turn.get("ended_at"), turn.get("started_at"), turn.get("updated_at"), turn.get("created_at")),
                session_id=session_id,
                task_id=_text(turn.get("task_id")) or None,
                turn_id=turn_id,
                node_id=_text(turn.get("execution_node_id")) or _text(turn.get("gateway_node_id")) or None,
                backend=_text(turn.get("backend")) or None,
                status=_text(turn.get("final_status")) or "unknown",
                confidence="medium",
                summary=f"Telemetry turn {_text(turn.get('final_status')) or 'observed'}",
                detail={
                    "coverage": turn.get("coverage") or {},
                    "data_quality": turn.get("data_quality") or [],
                    "metrics": _compact_metrics(turn.get("metrics")),
                },
                raw_refs={"turn_id": turn_id},
            )
        )

    for job in jobs:
        derived_job = derive_job_execution_state(job)
        job_id: str = str(job.get("id") or "")
        items.append(
            SessionTimelineItem(
                id=f"job:{job_id}",
                kind="job_state",
                source="jobs",
                timestamp=_timestamp(job.get("finished_at"), job.get("updated_at"), job.get("started_at")),
                session_id=session_id,
                job_id=job_id,
                node_id=_text(job.get("node_id")) or None,
                status=derived_job.state,
                confidence=derived_job.confidence,
                summary=f"Job {job.get('label') or job_id} {derived_job.state}",
                detail={
                    "label": job.get("label"),
                    "exit_code": job.get("exit_code"),
                    "tail": _text(job.get("tail"))[:500] or None,
                },
                raw_refs=derived_job.raw_refs,
            )
        )

    for approval in approvals:
        approval_id: str = str(approval.get("id") or "")
        items.append(
            SessionTimelineItem(
                id=f"approval:{approval_id}",
                kind="approval",
                source="approvals",
                timestamp=_timestamp(approval.get("resolved_at"), approval.get("created_at")),
                session_id=session_id,
                task_id=_text(approval.get("task_id")) or None,
                status=_text(approval.get("status")) or "unknown",
                confidence="high",
                summary=f"Approval {_text(approval.get('status')) or 'unknown'}: {_text(approval.get('action'))}",
                detail={
                    "action": approval.get("action"),
                    "risk": approval.get("risk"),
                    "reversible": bool(approval.get("reversible")),
                    "requested_by": approval.get("requested_by"),
                    "resolved_by": approval.get("resolved_by"),
                },
                raw_refs={"approval_id": approval_id},
            )
        )

    items.sort(key=_sort_key)
    page: list[SessionTimelineItem] = items[offset : offset + bounded_limit]
    next_offset: int = offset + bounded_limit
    return SessionTimelineResponse(
        items=page,
        next_cursor=str(next_offset) if next_offset < len(items) else None,
        generated_at=generated_at,
        coverage=coverage,
    )


def _cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        return 0


def _safe_list(loader: Any) -> list[dict[str, object]]:
    try:
        loaded = loader()
    except Exception:
        return []
    return [dict(row) for row in loaded] if isinstance(loaded, list) else []


def _telemetry_coverage(turns: list[dict[str, object]]) -> str:
    if not turns:
        return "empty"
    for turn in turns:
        coverage = turn.get("coverage") or {}
        data_quality = turn.get("data_quality") or []
        events_pruned = bool(turn.get("events_pruned_at"))
        if events_pruned or data_quality:
            return "partial"
        if isinstance(coverage, dict):
            for details in coverage.values():
                if isinstance(details, dict) and details.get("coverage") not in ("complete", None):
                    return "partial"
    return "complete"


def _sort_key(item: SessionTimelineItem) -> tuple[str, int, str]:
    order = {
        "task_state": 0,
        "approval": 1,
        "turn_event": 2,
        "artifact": 3,
        "file_change": 4,
        "job_state": 5,
        "worker_state": 6,
        "recovery": 7,
        "system_notice": 8,
    }
    return (item.timestamp, order.get(item.kind, 99), item.id)


def _timestamp(*values: object) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return datetime.now(tz=timezone.utc).isoformat()


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _loads_list(raw: object) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            loaded: object = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return loaded if isinstance(loaded, list) else []
    return []


def _compact_metrics(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    keys = (
        "input_tokens",
        "output_tokens",
        "context_tokens",
        "total_token_work",
        "tool_call_count",
        "subagent_count",
        "wall_time_ms",
        "metric_quality",
    )
    return {key: raw.get(key) for key in keys if key in raw}
