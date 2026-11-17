"""Durable session timeline read model.

This module is intentionally pure over already-selected repository objects:
MeshDB, TelemetryStore, and a session row/view supplied by the caller. It does
not read SSE logs and does not scan the filesystem.
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.core.task_state_truth import (
    derive_job_execution_state,
    derive_task_execution_state,
)

logger = logging.getLogger(__name__)

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
    "session_state",  # [A83] head item carrying the derived secondary reason
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
    # [A83] Derived, non-authoritative secondary reason for the session's primary
    # state, carried alongside the timeline (spec
    # docs/TBD/SESSION_WAIT_STATE_GRANULARITY.md). Session-scoped, so it is
    # attached to the head `session_state` item, not per task/turn/job. None ⇒ no
    # reason (BUSY, terminal, or db unavailable).
    reason: dict[str, Any] | None = None


class SessionTimelineResponse(BaseModel):
    items: list[SessionTimelineItem]
    next_cursor: str | None
    generated_at: str
    coverage: dict[str, str]
    context_fill: dict[str, Any]
    context_economics: dict[str, Any] = Field(default_factory=dict)
    # [A83] The session's derived secondary reason, surfaced at the response
    # level too (the pillow needs it even when the item page is empty). None ⇒
    # no reason derived.
    reason: dict[str, Any] | None = None


def build_session_timeline(
    *,
    db: Any,
    telemetry_store: Any | None,
    session_id: str,
    session_row: dict[str, object] | None = None,
    session_reason: dict[str, Any] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> SessionTimelineResponse:
    bounded_limit: int = max(1, min(int(limit), 200))
    offset: int = _cursor_offset(cursor)
    source_limit: int = 500
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
        tasks, coverage["tasks"] = _load_list(
            "tasks",
            lambda: db.list_tasks(session_id=session_id, limit=source_limit),
        )
        nodes, node_coverage = _load_list("nodes", lambda: db.list_nodes())
        if node_coverage != "complete" and coverage["tasks"] == "complete":
            coverage["tasks"] = "partial"
        jobs, coverage["jobs"] = _load_list(
            "jobs",
            lambda: db.list_jobs(session_id=session_id, limit=source_limit),
        )
        approvals, coverage["approvals"] = _load_list(
            "approvals",
            lambda: db.list_approvals(session_id=session_id, limit=source_limit)
        )
        coverage["artifacts"] = coverage["tasks"]

    if telemetry_store is not None:
        turns, telemetry_status = _load_list(
            "telemetry",
            lambda: telemetry_store.list_turns(session_id=session_id, limit=source_limit)
        )
        coverage["telemetry"] = (
            _telemetry_coverage(turns) if telemetry_status == "complete" else telemetry_status
        )

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
        # Skip internal mesh health-check tasks — they must not appear in the
        # user-facing session timeline.
        if task_id.startswith("inspect_"):
            continue
        # Job state already represents these internal delivery/audit rows.
        if task.get("action") == "watched_job" or task.get("turn_kind") == "watched_job":
            continue
        node_id: str = str(task.get("claimed_by") or task.get("machine_id") or "")
        derived = derive_task_execution_state(
            task,
            session_row=session_row,
            node_row=nodes_by_id.get(node_id),
            telemetry_turn=turns_by_task.get(task_id),
            approval_pending=task_id in pending_approval_task_ids,
            recovery_evidence=_has_recovery_evidence(turns_by_task.get(task_id)),
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
                staleness=_staleness_for_state(derived.state),
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
                    "reason": "Result consumed; no pending continuation." if job.get("consumed_at") else None,
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
                    "proposal": _recovery_proposal(approval),
                },
                raw_refs={"approval_id": approval_id},
            )
        )

    # [A83] A session-scoped head item carrying the derived secondary reason, so
    # the reason is present even when there are no task/turn/job items. Reason is
    # computed on the read path by the caller (never here, never on a timer —
    # #145/#147); we only render it. It is sorted into the stream by its honest
    # timestamp (below) rather than force-pinned to the top: pinning + a render-
    # time stamp made a stale "idle" always appear newer than a live worker/turn
    # row below it (the operator's reported inversion). Its timestamp is the
    # session's real status-change time (``updated_at``), so order AND time are
    # truthful — a genuinely newer live row now sorts above it.
    if session_reason is not None:
        kind_txt = str(session_reason.get("kind") or "")
        items.append(
            SessionTimelineItem(
                id=f"session:{session_id}:reason",
                kind="session_state",
                source="session_reason",
                timestamp=_timestamp(
                    (session_row or {}).get("updated_at"),
                    (session_row or {}).get("created_at"),
                    generated_at,
                ),
                session_id=session_id,
                status=_session_status_text(session_row),
                confidence=str(session_reason.get("confidence") or "") or None,
                summary=_reason_summary(kind_txt),
                detail={"reason": session_reason},
                reason=session_reason,
            ),
        )
    economics = _session_context_economics(turns, tasks, approvals)
    if db is not None and callable(getattr(db, "session_recovery_evidence", None)):
        recovery_rows, recovery_coverage = _load_list("recovery_evidence", lambda: db.session_recovery_evidence(session_id))
        economics["recovery_coverage"] = recovery_coverage
        economics["recoveries"] = _recovery_evidence(recovery_rows)
    # Single honest ordering pass over ALL items (incl. the session head).
    items.sort(key=lambda item: item.timestamp, reverse=True)
    page: list[SessionTimelineItem] = items[offset : offset + bounded_limit]
    next_offset: int = offset + bounded_limit
    return SessionTimelineResponse(
        items=page,
        next_cursor=str(next_offset) if next_offset < len(items) else None,
        generated_at=generated_at,
        coverage=coverage,
        context_fill=_context_fill_summary(turns[0] if turns else None),
        context_economics=economics,
        reason=session_reason,
    )


def _cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        return 0


def _load_list(source: str, loader: Any) -> tuple[list[dict[str, object]], str]:
    try:
        loaded = loader()
    except Exception as exc:
        logger.warning("session_timeline_source_failed source=%s err=%s", source, exc)
        return [], "unavailable"
    if not isinstance(loaded, list):
        logger.warning(
            "session_timeline_source_unexpected source=%s type=%s",
            source,
            type(loaded).__name__,
        )
        return [], "unavailable"
    return [dict(row) for row in loaded], "complete"


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


def _timestamp(*values: object) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return datetime.now(tz=timezone.utc).isoformat()


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


# Human-readable phrasing for the derived session reason (shown verbatim in the
# durable-state panel). Keeps the operator surface free of raw enum tokens like
# ``work_in_flight``; unknown kinds degrade to a de-underscored humanization.
_REASON_SUMMARY: dict[str, str] = {
    "paused_quota": "Paused — quota",
    "paused_retry": "Paused — retrying",
    "waiting_workers": "Waiting on workers",
    "waiting_job": "Waiting on a script",
    "open_case_idle": "Open case — nothing pending",
    "work_in_flight": "Work in flight",
    "idle": "Idle",
    "node_offline": "Held — node offline",
}


def _reason_summary(kind: str) -> str:
    if not kind:
        return "Session reason"
    phrase = _REASON_SUMMARY.get(kind) or kind.replace("_", " ").strip().capitalize()
    return f"Session reason: {phrase}"


def _session_status_text(session_row: dict[str, object] | None) -> str | None:
    """The session's primary status off the loaded row (presentational), or None."""
    if not isinstance(session_row, dict):
        return None
    status = session_row.get("status")
    return status if isinstance(status, str) and status else None


def _staleness_for_state(state: str) -> str:
    if state in {"stale_claim", "detached"}:
        return "stale"
    if state in {"worker_unknown", "recovery_required"}:
        return "unknown"
    return "fresh"


def _has_recovery_evidence(turn: dict[str, object] | None) -> bool:
    if not turn:
        return False
    data_quality = turn.get("data_quality")
    if isinstance(data_quality, list):
        for item in data_quality:
            if isinstance(item, dict) and item.get("reason_code") == "reconciled_after_restart":
                return True
    coverage = turn.get("coverage")
    if isinstance(coverage, dict):
        for item in coverage.values():
            if isinstance(item, dict) and item.get("reason_code") == "reconciled_after_restart":
                return True
    return False


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
        "context_window_tokens",
        "context_used_ratio",
        "context_remaining_tokens",
    )
    return {key: raw.get(key) for key in keys if key in raw}


def _context_fill_summary(latest_turn: dict[str, object] | None) -> dict[str, Any]:
    if latest_turn is None:
        return {
            "context_used_ratio": None,
            "context_window_tokens": None,
            "context_remaining_tokens": None,
            "context_window_source": "unknown",
            "reason": "no_turns_observed",
        }
    metrics = latest_turn.get("metrics")
    window = metrics.get("context_window_tokens") if isinstance(metrics, dict) else None
    if window is None:
        return {
            "context_used_ratio": None,
            "context_window_tokens": None,
            "context_remaining_tokens": None,
            "context_window_source": "unknown",
            "reason": "context_window_unknown_for_backend_model",
        }
    return {
        "context_used_ratio": metrics.get("context_used_ratio"),
        "context_window_tokens": window,
        "context_remaining_tokens": metrics.get("context_remaining_tokens"),
        "context_window_source": "known",
    }


def _loads_dict(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            pass
    return {}


def _session_context_economics(
    turns: list[dict[str, object]], tasks: list[dict[str, object]], approvals: list[dict[str, object]],
) -> dict[str, Any]:
    from src.services.context_economics import (
        compaction_outcome,
        context_economics,
        observed_turn_cost,
        turn_model,
    )

    latest = turns[0] if turns else {}
    metrics = _loads_dict(latest.get("metrics"))
    result = context_economics(
        metrics.get("turn_exit_context_tokens"), metrics.get("context_window_tokens"),
        turn_model(latest),
    ).model_dump()
    ordered = sorted(turns, key=lambda row: _text(row.get("started_at") or row.get("created_at")))
    by_task = {str(row.get("task_id") or row.get("turn_id")): row for row in turns}
    start_times = [_text(row.get("started_at") or row.get("created_at")) for row in ordered]
    completed = sorted((row for row in turns if row.get("ended_at")), key=lambda row: _text(row.get("ended_at")))
    end_times = [_text(row.get("ended_at")) for row in completed]
    compactions = sorted(
        (row for row in tasks if row.get("turn_kind") == "compaction" or row.get("action") == "compact_session"),
        key=lambda row: _text(row.get("started_at") or row.get("created_at")),
    )
    outcomes: list[dict[str, Any]] = []
    compact_task_ids = {str(task.get("id")) for task in compactions}
    for index, task in enumerate(compactions):
        start = _text(task.get("started_at") or task.get("created_at"))
        end = _text(task.get("completed_at"))
        next_start = (_text(compactions[index + 1].get("started_at") or compactions[index + 1].get("created_at"))
                      if index + 1 < len(compactions) else "")
        compact_turn = by_task.get(str(task.get("id")))
        if _loads_dict((compact_turn or {}).get("metrics")).get("compactions"):
            continue  # the native boundary below is the same intervention
        before_index = bisect_right(end_times, start) - 1
        after_index = bisect_left(start_times, end) if end else len(ordered)
        before = completed[before_index] if before_index >= 0 else None
        after = ordered[after_index] if after_index < len(ordered) else None
        if after and ((next_start and _text(after.get("started_at")) >= next_start)
                      or _loads_dict(after.get("metrics")).get("compactions")):
            after = None  # another intervention makes attribution ambiguous
        # A native session replacement cannot prove the compactor's effect.
        if before and after and (not before.get("backend_session_id_end") or before.get("backend_session_id_end") != after.get("backend_session_id_start")):
            after = None
        before_tokens = _loads_dict((before or {}).get("metrics")).get("turn_exit_context_tokens")
        after_tokens = _loads_dict((after or {}).get("metrics")).get("turn_entry_context_tokens")
        outcome = compaction_outcome(
            str(task.get("id")), str(task.get("status") or "unknown"), before_tokens, after_tokens,
            turn_model(after or before or {}), observed_turn_cost(by_task.get(str(task.get("id")))),
        ).model_dump()
        outcome["observed_at"] = end or start
        outcome["measurement"] = "observed" if after_tokens is not None else "awaiting_next_context_observation"
        outcomes.append(outcome)
    for turn in ordered:
        for boundary in _loads_dict(turn.get("metrics")).get("compactions", []):
            outcome = compaction_outcome(
                str(boundary.get("event_id")), "completed", boundary.get("before_tokens"),
                boundary.get("after_tokens"), turn_model(turn),
                observed_turn_cost(turn) if str(turn.get("task_id") or turn.get("turn_id")) in compact_task_ids else None,
            ).model_dump()
            outcome["observed_at"] = boundary.get("observed_at") or turn.get("started_at") or turn.get("created_at") or ""
            outcome["measurement"] = "native_boundary_observed"
            outcome["trigger"] = boundary.get("trigger")
            outcomes.append(outcome)
    result["compactions"] = sorted(outcomes, key=lambda row: str(row.get("observed_at") or ""), reverse=True)[:20]
    result["recoveries"] = [
        {"task_id": task.get("id"), "kind": task.get("turn_kind"), "status": task.get("status"),
         "observed_usd": observed_turn_cost(by_task.get(str(task.get("id")))),
         "tokens": _loads_dict((by_task.get(str(task.get("id"))) or {}).get("metrics")),
         "cost_basis": "recorded_tokens_api_equivalent"}
        for task in tasks if task.get("turn_kind") in ("respawn", "retry")
    ][:20]
    result["proposals"] = [
        {"approval_id": row.get("id"), "status": row.get("status"), "created_at": row.get("created_at"),
         "action": row.get("action"), "estimate": _recovery_proposal(row).get("estimate"),
         "cost_scope": _recovery_proposal(row).get("cost_scope")}
        for row in approvals if row.get("action") in ("case_manager_respawn", "case_resume")
    ][:20]
    result["history_limit"] = 500
    return result


def _recovery_proposal(row: dict[str, object]) -> dict[str, Any]:
    if row.get("action") not in ("case_manager_respawn", "case_resume"):
        return {}
    payload = _loads_dict(row.get("payload"))
    estimate = _loads_dict(payload.get("estimate"))
    bounded_estimate = {
        key: value for key, value in estimate.items()
        if key in ("known", "usd", "cache_creation_tokens", "basis", "context_quality", "observed_at", "reason", "session_id", "model")
        and isinstance(value, (str, int, float, bool, type(None)))
        and (not isinstance(value, str) or len(value) <= 128)
    }
    bounded_estimate = {"known": False, "reason": "no_recorded_estimate", "session_id": row.get("session_id"),
                        "model": None, "cache_creation_tokens": None, "usd": None, "basis": "unknown", **bounded_estimate}
    economics = _loads_dict(estimate.get("economics"))
    bounded_estimate["economics"] = {
        key: value if type(value) in (int, float) else None
        for key, value in ((key, economics.get(key)) for key in ("context_used_ratio", "warm_read_usd", "cold_premium_usd"))
    }
    return {
        "mode": str(payload.get("mode") or "")[:32],
        "cost_scope": str(payload.get("cost_scope") or "")[:128],
        "estimate": bounded_estimate,
    }



def _recovery_evidence(rows: list[dict[str, object]]) -> list[dict[str, Any]]:
    from src.services.context_economics import observed_turn_cost

    result: list[dict[str, Any]] = []
    for row in rows:
        turn = {**row, "metrics": _loads_dict(row.get("metrics_json")),
                "coverage": _loads_dict(row.get("coverage_json")),
                "observed_models": _loads_list(row.get("observed_models"))}
        estimate = _recovery_proposal({"action": "case_resume", "payload": row.get("payload")}).get("estimate")
        result.append({"task_id": row.get("task_id"), "status": row.get("status"),
                       "session_id": row.get("session_id"), "prior_session_id": row.get("prior_session_id"),
                       "kind": row.get("turn_kind"), "observed_usd": observed_turn_cost(turn),
                       "estimate": estimate, "cost_basis": (
                           "provider_reported" if turn["metrics"].get("provider_reported_cost_usd") is not None
                           else "recorded_tokens_api_equivalent_5m_write"),
                       "tokens": {key: turn["metrics"].get(key) for key in (
                           "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                           "turn_entry_context_tokens", "turn_exit_context_tokens", "context_used_ratio")}})
    return result
