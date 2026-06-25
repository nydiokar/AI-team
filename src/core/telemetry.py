"""Typed, privacy-preserving telemetry contracts for one logical user turn.

This module intentionally contains no database or network code.  Producers build
validated events here, then hand them to a sink.  Event attributes are
default-deny: a field is persisted only when the event catalog explicitly
allows it, and values must be scalar (or a flat list of scalars).
"""

from __future__ import annotations

import contextvars
import hashlib
import itertools
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

JsonScalar = str | int | float | bool | None

SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def new_telemetry_id(prefix: str) -> str:
    """Return an opaque unique ID without embedding session or user content."""
    safe_prefix = re.sub(r"[^a-z0-9_]", "", (prefix or "id").lower())[:16] or "id"
    return f"{safe_prefix}_{uuid.uuid4().hex}"


# Stable for the lifetime of this Python process. It identifies the emitter,
# not a child backend process.
EMITTER_PROCESS_INSTANCE_ID = new_telemetry_id("proc")
_SOURCE_SEQUENCE = itertools.count(1)


@dataclass(frozen=True)
class TelemetryContext:
    """Immutable correlation passed explicitly into a backend invocation."""

    turn_id: str
    invocation_id: str
    node_id: str
    session_id: Optional[str] = None
    backend: Optional[str] = None
    model: Optional[str] = None
    source: Literal["gateway", "worker"] = "worker"
    attempt: int = 1
    spawn_reason: str = "initial"
    parent_invocation_id: Optional[str] = None
    retry_of_invocation_id: Optional[str] = None

    @classmethod
    def create(
        cls,
        *,
        turn_id: str,
        node_id: Optional[str] = None,
        session_id: Optional[str] = None,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        source: Literal["gateway", "worker"] = "worker",
        attempt: int = 1,
        spawn_reason: str = "initial",
        parent_invocation_id: Optional[str] = None,
        retry_of_invocation_id: Optional[str] = None,
    ) -> "TelemetryContext":
        if not turn_id:
            raise ValueError("turn_id is required")
        return cls(
            turn_id=turn_id,
            invocation_id=new_telemetry_id("inv"),
            node_id=node_id or socket.gethostname(),
            session_id=session_id or None,
            backend=backend or None,
            model=model or None,
            source=source,
            attempt=max(1, int(attempt)),
            spawn_reason=spawn_reason or "unknown",
            parent_invocation_id=parent_invocation_id or None,
            retry_of_invocation_id=retry_of_invocation_id or None,
        )


def telemetry_subprocess_env(context: Optional[TelemetryContext]) -> Dict[str, str]:
    """Return the non-sensitive correlation variables allowed in child processes."""
    if context is None:
        return {}
    values = {
        "AI_TEAM_SESSION_ID": context.session_id,
        "AI_TEAM_TURN_ID": context.turn_id,
        "AI_TEAM_INVOCATION_ID": context.invocation_id,
        "AI_TEAM_NODE_ID": context.node_id,
    }
    return {key: value for key, value in values.items() if value}


_telemetry_context: contextvars.ContextVar[Optional[TelemetryContext]] = (
    contextvars.ContextVar("telemetry_context", default=None)
)


def set_telemetry_context(context: TelemetryContext) -> contextvars.Token:
    return _telemetry_context.set(context)


def reset_telemetry_context(token: contextvars.Token) -> None:
    _telemetry_context.reset(token)


def current_telemetry_context() -> Optional[TelemetryContext]:
    return _telemetry_context.get()


class telemetry_context:
    """Scope an immutable telemetry context to the current async/thread context."""

    def __init__(self, context: TelemetryContext) -> None:
        self._context = context
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> TelemetryContext:
        self._token = set_telemetry_context(self._context)
        return self._context

    def __exit__(self, *_exc: Any) -> None:
        if self._token is not None:
            reset_telemetry_context(self._token)


# Stable event catalog.  The values are the only attribute keys accepted for
# each event.  Never add a generic "payload", "message", "args", or "result".
EVENT_ATTRIBUTE_ALLOWLIST: Dict[str, frozenset[str]] = {
    "turn.accepted": frozenset({"task_id", "source"}),
    "turn.queued": frozenset({"priority"}),
    "turn.started": frozenset(),
    "turn.timeout_requested": frozenset({"timeout_kind", "timeout_ms"}),
    "turn.cancel_requested": frozenset({"reason_code"}),
    "turn.result_recorded": frozenset({"status", "error_code"}),
    "turn.completed": frozenset({"status", "timeout_status", "exit_code"}),
    "invocation.created": frozenset({
        "attempt", "spawn_reason", "action", "parent_invocation_id",
        "retry_of_invocation_id",
    }),
    "invocation.started": frozenset({"action"}),
    "invocation.retry_scheduled": frozenset({
        "retry_reason", "delay_ms", "next_attempt", "retry_of_invocation_id",
    }),
    "invocation.duplicate_detected": frozenset({
        "duplicate_of_invocation_id", "confidence", "rule",
    }),
    "invocation.completed": frozenset({
        "status", "duration_ms", "exit_code", "error_code",
    }),
    "process.spawned": frozenset({
        "process_instance_id", "parent_process_instance_id", "process_role",
        "executable_name",
    }),
    "process.timeout_detected": frozenset({"timeout_kind", "timeout_ms"}),
    "process.termination_requested": frozenset({"reason_code"}),
    "process.exited": frozenset({
        "process_instance_id", "exit_code", "signal", "duration_ms",
    }),
    "process.exit_unknown": frozenset({"process_instance_id", "reason_code"}),
    "model.request.started": frozenset({
        "sequence", "provider_request_id", "work_category",
    }),
    "model.request.usage": frozenset({
        "sequence", "provider_request_id", "work_category", "input_tokens",
        "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "reasoning_tokens", "context_tokens", "input_token_semantics",
        "usage_granularity", "usage_source", "usage_coverage",
        "counter_semantics",
    }),
    "model.request.completed": frozenset({
        "sequence", "provider_request_id", "status", "duration_ms",
    }),
    "model.request.failed": frozenset({
        "sequence", "provider_request_id", "error_code",
    }),
    "tool.call.started": frozenset({"tool_name", "tool_category", "sequence"}),
    "tool.call.completed": frozenset({
        "tool_name", "tool_category", "sequence", "duration_ms", "status",
    }),
    "tool.call.failed": frozenset({
        "tool_name", "tool_category", "sequence", "duration_ms", "error_code",
    }),
    "subagent.started": frozenset({"sequence", "agent_type"}),
    "subagent.completed": frozenset({"sequence", "status", "duration_ms"}),
    "subagent.failed": frozenset({"sequence", "error_code", "duration_ms"}),
    "telemetry.coverage": frozenset({"area", "coverage", "reason_code", "adapter_version"}),
    "telemetry.parse_error": frozenset({
        "backend_event_type", "error_code", "adapter_version",
    }),
    "telemetry.batch_dropped": frozenset({"event_count", "reason_code"}),
    "telemetry.reconciled": frozenset({"reason_code", "status"}),
}


def _validate_scalar(value: Any) -> JsonScalar | list[JsonScalar]:
    if isinstance(value, str) and len(value) > 256:
        raise ValueError("telemetry string attributes must be at most 256 characters")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        if len(value) > 16:
            raise ValueError("telemetry list attributes must contain at most 16 items")
        if all(
            item is None
            or isinstance(item, (int, float, bool))
            or isinstance(item, str) and len(item) <= 256
            for item in value
        ):
            return value
    raise ValueError("telemetry attributes must be scalar or flat scalar lists")


def sanitize_tool_name(value: str) -> str:
    """Return a bounded identifier, never tool arguments or command text."""
    raw = value or "unknown"
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"tool_{digest}"


def sanitize_attributes(event_name: str, attributes: Mapping[str, Any]) -> Dict[str, Any]:
    """Apply the event-specific default-deny allowlist."""
    allowed = EVENT_ATTRIBUTE_ALLOWLIST.get(event_name)
    if allowed is None:
        raise ValueError(f"unknown telemetry event: {event_name}")

    clean: Dict[str, Any] = {}
    for key, value in attributes.items():
        if key not in allowed:
            raise ValueError(f"attribute {key!r} is not allowed for {event_name}")
        normalized = _validate_scalar(value)
        if key == "tool_name" and isinstance(normalized, str):
            normalized = sanitize_tool_name(normalized)
        clean[key] = normalized
    return clean


class TelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: str = Field(min_length=1, max_length=96)
    event_name: str = Field(min_length=1, max_length=96)
    event_time: datetime
    observed_time: datetime
    node_id: str = Field(min_length=1, max_length=128)
    emitter_process_instance_id: str = Field(min_length=1, max_length=96)
    source: Literal["gateway", "worker", "backend", "hook", "reconciler"]
    source_sequence: Optional[int] = Field(default=None, ge=0)
    clock_quality: Literal["local", "ntp_synced", "unknown"] = "unknown"

    session_id: Optional[str] = Field(default=None, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    invocation_id: Optional[str] = Field(default=None, max_length=96)
    model_request_id: Optional[str] = Field(default=None, max_length=160)
    tool_call_id: Optional[str] = Field(default=None, max_length=160)
    subagent_id: Optional[str] = Field(default=None, max_length=160)

    backend: Optional[str] = Field(default=None, max_length=80)
    model: Optional[str] = Field(default=None, max_length=160)
    pid: Optional[int] = Field(default=None, ge=0)
    attributes: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_time", "observed_time")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("telemetry timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("attributes")
    @classmethod
    def _attributes_are_flat(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        for item in value.values():
            _validate_scalar(item)
        return value


def build_event(
    event_name: str,
    *,
    turn_id: str,
    node_id: str,
    emitter_process_instance_id: str,
    source: Literal["gateway", "worker", "backend", "hook", "reconciler"],
    attributes: Optional[Mapping[str, Any]] = None,
    event_time: Optional[datetime] = None,
    observed_time: Optional[datetime] = None,
    source_sequence: Optional[int] = None,
    clock_quality: Literal["local", "ntp_synced", "unknown"] = "unknown",
    session_id: Optional[str] = None,
    invocation_id: Optional[str] = None,
    model_request_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    subagent_id: Optional[str] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    pid: Optional[int] = None,
) -> TelemetryEvent:
    """Construct a validated event without accepting arbitrary backend payloads."""
    clean = sanitize_attributes(event_name, attributes or {})
    now = utc_now()
    return TelemetryEvent(
        event_id=new_telemetry_id("evt"),
        event_name=event_name,
        event_time=event_time or now,
        observed_time=observed_time or now,
        node_id=node_id,
        emitter_process_instance_id=emitter_process_instance_id,
        source=source,
        source_sequence=(
            source_sequence if source_sequence is not None else next(_SOURCE_SEQUENCE)
        ),
        clock_quality=clock_quality,
        session_id=session_id or None,
        turn_id=turn_id,
        invocation_id=invocation_id or None,
        model_request_id=model_request_id or None,
        tool_call_id=tool_call_id or None,
        subagent_id=subagent_id or None,
        backend=backend or None,
        model=model or None,
        pid=pid,
        attributes=clean,
    )
