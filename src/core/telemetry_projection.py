"""Deterministic event-to-summary projection and token-accounting formulas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from src.core.telemetry import TelemetryEvent

PROJECTION_VERSION = 1


def _as_dict(event: TelemetryEvent | Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(event, TelemetryEvent):
        return event.model_dump(mode="json")
    return dict(event)


def _timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _duration_ms(
    start: Any, end: Any, *, flags: Optional[List[str]] = None
) -> Optional[int]:
    start_dt = _timestamp(start)
    end_dt = _timestamp(end)
    if start_dt is None or end_dt is None:
        return None
    duration = (end_dt - start_dt).total_seconds() * 1000
    if duration < 0:
        if flags is not None:
            flags.append("clock_skew")
        return None
    return round(duration)


def _non_negative_int(value: Any, flags: List[str], field: str) -> Optional[int]:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        flags.append(f"invalid_{field}")
        return None
    if parsed < 0:
        flags.append(f"negative_{field}")
        return None
    return parsed


def normalize_context_tokens(
    *,
    input_tokens: Optional[int],
    cache_read_tokens: Optional[int],
    cache_creation_tokens: Optional[int],
    input_token_semantics: str,
) -> Optional[int]:
    """Normalize provider input semantics without double-counting cache tokens."""
    if input_tokens is None:
        return None
    if input_token_semantics == "includes_cache":
        return input_tokens
    if input_token_semantics == "excludes_cache":
        return input_tokens + (cache_read_tokens or 0) + (cache_creation_tokens or 0)
    return None


def _event_sort_key(event: Dict[str, Any]) -> tuple:
    return (
        str(event.get("event_time") or ""),
        str(event.get("source") or ""),
        int(event.get("source_sequence") or 0),
        str(event.get("event_id") or ""),
    )


def project_turn(events: Iterable[TelemetryEvent | Dict[str, Any]]) -> Dict[str, Any]:
    """Build a deterministic turn projection from an unordered event set."""
    ordered = sorted((_as_dict(event) for event in events), key=_event_sort_key)
    if not ordered:
        raise ValueError("cannot project an empty turn")

    turn_ids = {str(event.get("turn_id") or "") for event in ordered}
    if len(turn_ids) != 1 or "" in turn_ids:
        raise ValueError("all events must have one non-empty turn_id")
    turn_id = next(iter(turn_ids))

    turn: Dict[str, Any] = {
        "turn_id": turn_id,
        "session_id": next((e.get("session_id") for e in ordered if e.get("session_id")), None),
        "task_id": turn_id,
        "gateway_node_id": next(
            (e.get("node_id") for e in ordered if e.get("source") == "gateway"), None
        ),
        "execution_node_id": next(
            (
                e.get("node_id")
                for e in ordered
                if e.get("event_name") in ("invocation.started", "process.spawned")
            ),
            None,
        ),
        "backend": next((e.get("backend") for e in ordered if e.get("backend")), None),
        "requested_model": None,
        "observed_models": sorted({e["model"] for e in ordered if e.get("model")}),
        "started_at": None,
        "ended_at": None,
        "final_status": "running",
        "timeout_status": "none",
        "final_exit_code": None,
        "final_invocation_id": None,
        "metrics": {},
        "coverage": {},
        "data_quality": [],
        "projection_version": PROJECTION_VERSION,
    }
    invocations: Dict[str, Dict[str, Any]] = {}
    processes: Dict[str, Dict[str, Any]] = {}
    process_links: set[tuple[str, str, str]] = set()
    model_requests: Dict[str, Dict[str, Any]] = {}
    tool_ids: Dict[str, set[str]] = {}
    subagent_ids: Dict[str, set[str]] = {}
    turn_flags: List[str] = []
    timeout_kinds: set[str] = set()

    for event in ordered:
        name = event.get("event_name")
        attrs = event.get("attributes") or {}
        invocation_id = event.get("invocation_id")
        event_time = event.get("event_time")

        if name == "turn.accepted":
            turn["task_id"] = attrs.get("task_id") or turn_id
        elif name == "turn.started" and turn["started_at"] is None:
            turn["started_at"] = event_time
        elif name == "turn.timeout_requested":
            timeout_kinds.add(str(attrs.get("timeout_kind") or "gateway_timeout"))
        elif name == "turn.completed":
            turn["ended_at"] = event_time
            turn["final_status"] = attrs.get("status") or "unknown"
            turn["timeout_status"] = attrs.get("timeout_status") or (
                next(iter(timeout_kinds)) if len(timeout_kinds) == 1 else
                "multiple" if timeout_kinds else "none"
            )
            turn["final_exit_code"] = attrs.get("exit_code")
            turn["final_invocation_id"] = invocation_id
        elif name == "telemetry.coverage":
            area = str(attrs.get("area") or "unknown")
            turn["coverage"][area] = {
                "coverage": attrs.get("coverage") or "unknown",
                "reason_code": attrs.get("reason_code"),
                "adapter_version": attrs.get("adapter_version"),
            }

        if invocation_id:
            invocation = invocations.setdefault(
                invocation_id,
                {
                    "invocation_id": invocation_id,
                    "turn_id": turn_id,
                    "parent_invocation_id": None,
                    "retry_of_invocation_id": None,
                    "duplicate_of_invocation_id": None,
                    "attempt": 1,
                    "spawn_reason": "unknown",
                    "action": "unknown",
                    "node_id": event.get("node_id"),
                    "backend": event.get("backend") or turn["backend"] or "unknown",
                    "requested_model": None,
                    "observed_model": event.get("model"),
                    "process_instance_id": None,
                    "pid": None,
                    "process_started_at": None,
                    "started_at": None,
                    "ended_at": None,
                    "status": "created",
                    "timeout_kind": None,
                    "exit_code": None,
                    "signal": None,
                    "retry_reason": None,
                    "model_request_count": None,
                    "tool_call_count": None,
                    "subagent_count": None,
                    "usage": {},
                    "coverage": {},
                    "data_quality": [],
                },
            )
            if event.get("model"):
                invocation["observed_model"] = event.get("model")

            if name == "invocation.created":
                invocation.update(
                    {
                        "attempt": int(attrs.get("attempt") or 1),
                        "spawn_reason": attrs.get("spawn_reason") or "unknown",
                        "action": attrs.get("action") or "unknown",
                        "parent_invocation_id": attrs.get("parent_invocation_id"),
                        "retry_of_invocation_id": attrs.get("retry_of_invocation_id"),
                    }
                )
            elif name == "invocation.started":
                invocation["started_at"] = event_time
                invocation["status"] = "running"
            elif name == "invocation.retry_scheduled":
                invocation["retry_reason"] = attrs.get("retry_reason")
            elif name == "invocation.duplicate_detected":
                invocation["duplicate_of_invocation_id"] = attrs.get(
                    "duplicate_of_invocation_id"
                )
            elif name == "invocation.completed":
                invocation["ended_at"] = event_time
                invocation["status"] = attrs.get("status") or "unknown"
                invocation["exit_code"] = attrs.get("exit_code")
            elif name in ("process.timeout_detected", "turn.timeout_requested"):
                invocation["timeout_kind"] = attrs.get("timeout_kind") or "unknown"

        if name == "process.spawned":
            process_instance_id = str(attrs.get("process_instance_id") or "")
            if process_instance_id:
                process = processes.setdefault(
                    process_instance_id,
                    {
                        "process_instance_id": process_instance_id,
                        "node_id": event.get("node_id"),
                        "pid": event.get("pid"),
                        "parent_process_instance_id": attrs.get(
                            "parent_process_instance_id"
                        ),
                        "process_role": attrs.get("process_role") or "unknown",
                        "backend": event.get("backend"),
                        "executable_name": attrs.get("executable_name"),
                        "started_at": event_time,
                        "ended_at": None,
                        "exit_code": None,
                        "signal": None,
                        "status": "running",
                        "data_quality": [],
                    },
                )
                if invocation_id:
                    process_links.add((invocation_id, process_instance_id, "owns"))
                    invocation = invocations[invocation_id]
                    if invocation["process_instance_id"] is None:
                        invocation["process_instance_id"] = process_instance_id
                        invocation["pid"] = event.get("pid")
                        invocation["process_started_at"] = event_time
        elif name in ("process.exited", "process.exit_unknown"):
            process_instance_id = str(attrs.get("process_instance_id") or "")
            if process_instance_id:
                process = processes.setdefault(
                    process_instance_id,
                    {
                        "process_instance_id": process_instance_id,
                        "node_id": event.get("node_id"),
                        "pid": event.get("pid"),
                        "parent_process_instance_id": None,
                        "process_role": "unknown",
                        "backend": event.get("backend"),
                        "executable_name": None,
                        "started_at": None,
                        "ended_at": None,
                        "exit_code": None,
                        "signal": None,
                        "status": "unknown",
                        "data_quality": ["missing_process_spawn"],
                    },
                )
                process["ended_at"] = event_time
                process["exit_code"] = attrs.get("exit_code")
                process["signal"] = attrs.get("signal")
                process["status"] = "exited" if name == "process.exited" else "unknown"

        if name.startswith("tool.call.") and invocation_id and event.get("tool_call_id"):
            tool_ids.setdefault(invocation_id, set()).add(str(event["tool_call_id"]))
        if name.startswith("subagent.") and invocation_id and event.get("subagent_id"):
            subagent_ids.setdefault(invocation_id, set()).add(str(event["subagent_id"]))

        if name in (
            "model.request.started",
            "model.request.usage",
            "model.request.completed",
            "model.request.failed",
        ) and invocation_id:
            granularity = attrs.get("usage_granularity") or "request"
            model_request_id = event.get("model_request_id")
            if not model_request_id and granularity != "request":
                model_request_id = f"{invocation_id}:usage:aggregate"
            if not model_request_id:
                turn_flags.append("model_request_id_missing")
                continue

            request = model_requests.setdefault(
                str(model_request_id),
                {
                    "model_request_id": str(model_request_id),
                    "invocation_id": invocation_id,
                    "turn_id": turn_id,
                    "sequence": int(attrs.get("sequence") or 0),
                    "provider_request_id": attrs.get("provider_request_id"),
                    "model": event.get("model"),
                    "work_category": attrs.get("work_category") or "unknown",
                    "started_at": None,
                    "ended_at": None,
                    "status": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "cache_read_tokens": None,
                    "cache_creation_tokens": None,
                    "reasoning_tokens": None,
                    "context_tokens": None,
                    "input_token_semantics": attrs.get("input_token_semantics")
                    or "unknown",
                    "usage_granularity": granularity,
                    "usage_source": attrs.get("usage_source"),
                    "usage_coverage": attrs.get("usage_coverage") or "partial",
                    "is_duplicate": False,
                    "data_quality": [],
                },
            )
            flags = request["data_quality"]
            if name == "model.request.started":
                request["started_at"] = event_time
                request["status"] = "running"
            elif name == "model.request.usage":
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                    "reasoning_tokens",
                    "context_tokens",
                ):
                    request[field] = _non_negative_int(attrs.get(field), flags, field)
                request["input_token_semantics"] = attrs.get(
                    "input_token_semantics"
                ) or request["input_token_semantics"]
                if request["context_tokens"] is None:
                    request["context_tokens"] = normalize_context_tokens(
                        input_tokens=request["input_tokens"],
                        cache_read_tokens=request["cache_read_tokens"],
                        cache_creation_tokens=request["cache_creation_tokens"],
                        input_token_semantics=request["input_token_semantics"],
                    )
                request["usage_granularity"] = granularity
                request["usage_source"] = attrs.get("usage_source")
                request["usage_coverage"] = attrs.get("usage_coverage") or "partial"
            elif name == "model.request.completed":
                request["ended_at"] = event_time
                request["status"] = attrs.get("status") or "success"
            elif name == "model.request.failed":
                request["ended_at"] = event_time
                request["status"] = "failed"

    if turn["started_at"] is None:
        turn["started_at"] = ordered[0].get("event_time")
        turn_flags.append("turn_start_inferred")
    if turn["ended_at"] is None and turn["final_status"] != "running":
        turn["ended_at"] = ordered[-1].get("event_time")
        turn_flags.append("turn_end_inferred")
    if turn["final_status"] != "running" and not turn["final_invocation_id"] and invocations:
        latest = max(
            invocations.values(),
            key=lambda row: (
                row.get("ended_at") or row.get("started_at") or "",
                row.get("attempt") or 0,
                row["invocation_id"],
            ),
        )
        turn["final_invocation_id"] = latest["invocation_id"]
        if turn["final_exit_code"] is None:
            turn["final_exit_code"] = latest.get("exit_code")
        turn_flags.append("final_invocation_inferred")

    request_rows = [
        row
        for row in model_requests.values()
        if row["usage_granularity"] == "request" and not row["is_duplicate"]
    ]
    usage_rows = [row for row in model_requests.values() if not row["is_duplicate"]]
    for invocation_id, invocation in invocations.items():
        _duration_ms(
            invocation.get("started_at"),
            invocation.get("ended_at"),
            flags=invocation["data_quality"],
        )
        request_count = sum(
            1 for row in request_rows if row["invocation_id"] == invocation_id
        )
        invocation["model_request_count"] = request_count if request_count else None
        if invocation_id in tool_ids:
            invocation["tool_call_count"] = len(tool_ids[invocation_id])
        if invocation_id in subagent_ids:
            invocation["subagent_count"] = len(subagent_ids[invocation_id])
        invocation_usage = [
            row for row in usage_rows if row["invocation_id"] == invocation_id
        ]
        invocation["usage"] = _usage_totals(invocation_usage)

    metrics = _turn_metrics(
        turn=turn,
        invocations=list(invocations.values()),
        usage_rows=usage_rows,
        request_rows=request_rows,
        tool_ids=tool_ids,
        subagent_ids=subagent_ids,
        event_count=len(ordered),
        flags=turn_flags,
    )
    turn["metrics"] = metrics
    turn["data_quality"] = sorted(set(turn_flags))

    return {
        "turn": turn,
        "invocations": sorted(
            invocations.values(), key=lambda row: (row["attempt"], row["invocation_id"])
        ),
        "processes": sorted(processes.values(), key=lambda row: row["process_instance_id"]),
        "process_links": [
            {
                "invocation_id": invocation_id,
                "process_instance_id": process_instance_id,
                "relationship": relationship,
            }
            for invocation_id, process_instance_id, relationship in sorted(process_links)
        ],
        "model_requests": sorted(
            model_requests.values(),
            key=lambda row: (row["invocation_id"], row["sequence"], row["model_request_id"]),
        ),
    }


def _usage_totals(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "reasoning_tokens",
        "context_tokens",
    )
    return {
        field: sum(row[field] or 0 for row in rows)
        if any(row[field] is not None for row in rows)
        else None
        for field in fields
    }


def _turn_metrics(
    *,
    turn: Dict[str, Any],
    invocations: List[Dict[str, Any]],
    usage_rows: List[Dict[str, Any]],
    request_rows: List[Dict[str, Any]],
    tool_ids: Dict[str, set[str]],
    subagent_ids: Dict[str, set[str]],
    event_count: int,
    flags: List[str],
) -> Dict[str, Any]:
    usage = _usage_totals(usage_rows)
    request_work: List[int] = []
    all_work_known = bool(usage_rows)
    for row in usage_rows:
        if row["context_tokens"] is None:
            all_work_known = False
            continue
        request_work.append(
            row["context_tokens"]
            + (row["output_tokens"] or 0)
            + (row["reasoning_tokens"] or 0)
        )
    total_token_work = sum(request_work) if all_work_known else None
    work_amplification = None
    if request_rows and total_token_work is not None and request_work:
        largest = max(request_work)
        work_amplification = round(total_token_work / largest, 4) if largest else None

    request_contexts = [
        row["context_tokens"] for row in request_rows if row["context_tokens"] is not None
    ]
    ordered_requests = sorted(
        request_rows, key=lambda row: (row["started_at"] or "", row["sequence"])
    )
    entry_context = ordered_requests[0]["context_tokens"] if ordered_requests else None
    final_invocation = turn.get("final_invocation_id")
    final_requests = [
        row for row in ordered_requests if row["invocation_id"] == final_invocation
    ]
    exit_context = final_requests[-1]["context_tokens"] if final_requests else None

    cache_ratio = None
    context_total = usage.get("context_tokens")
    cache_total = usage.get("cache_read_tokens")
    if context_total and cache_total is not None:
        cache_ratio = cache_total / context_total
        if cache_ratio > 1:
            flags.append("cache_read_ratio_out_of_range")
            cache_ratio = None

    retry_reasons: Dict[str, int] = {}
    for invocation in invocations:
        if invocation["spawn_reason"] in ("retry", "session_recreate"):
            reason = invocation.get("retry_reason") or invocation["spawn_reason"]
            retry_reasons[reason] = retry_reasons.get(reason, 0) + 1

    return {
        **usage,
        "peak_context_tokens": max(request_contexts) if request_contexts else None,
        "total_token_work": total_token_work,
        "work_amplification": work_amplification,
        "turn_entry_context_tokens": entry_context,
        "turn_exit_context_tokens": exit_context,
        "intra_turn_context_growth": (
            exit_context - entry_context
            if entry_context is not None and exit_context is not None
            else None
        ),
        "invocations_per_turn": sum(
            1 for invocation in invocations
            if not invocation["duplicate_of_invocation_id"]
        ),
        "model_request_count": len(request_rows) if request_rows else None,
        "tool_call_count": len(set().union(*tool_ids.values())) if tool_ids else None,
        "subagent_count": (
            len(set().union(*subagent_ids.values())) if subagent_ids else None
        ),
        "retry_count": sum(retry_reasons.values()),
        "retry_reasons": dict(sorted(retry_reasons.items())),
        "failed_invocation_count": sum(
            1 for invocation in invocations if invocation["status"] == "failed"
        ),
        "duplicate_invocation_count": sum(
            1 for invocation in invocations if invocation["duplicate_of_invocation_id"]
        ),
        "cache_read_ratio": round(cache_ratio, 6) if cache_ratio is not None else None,
        "output_to_input_ratio": (
            round(usage["output_tokens"] / usage["context_tokens"], 6)
            if usage.get("context_tokens") and usage.get("output_tokens") is not None
            else None
        ),
        "wall_time_ms": _duration_ms(
            turn.get("started_at"), turn.get("ended_at"), flags=flags
        ),
        "telemetry_event_count": event_count,
        "metric_quality": (
            "request" if request_rows else "aggregate_only" if usage_rows else "unavailable"
        ),
    }
