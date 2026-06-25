"""Compatibility helper for optional backend telemetry keyword arguments."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from src.core.interfaces import ExecutionResult, ExecutionTelemetry


def call_backend(
    method: Callable[..., Any],
    *args: Any,
    telemetry_context: Any = None,
    telemetry_sink: Any = None,
) -> Any:
    """Call old or new backend implementations without a speculative retry.

    Signature inspection avoids catching a TypeError raised *inside* a backend,
    which could otherwise cause the backend action to run twice.
    """
    try:
        parameters = inspect.signature(method).parameters.values()
        names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
        )
    except (TypeError, ValueError):
        names = set()
        accepts_kwargs = False

    kwargs = {}
    if accepts_kwargs or "telemetry_context" in names:
        kwargs["telemetry_context"] = telemetry_context
    if accepts_kwargs or "telemetry_sink" in names:
        kwargs["telemetry_sink"] = telemetry_sink
    result = method(*args, **kwargs)
    if (
        isinstance(result, ExecutionResult)
        and telemetry_context is not None
        and result.telemetry is None
    ):
        result.telemetry = ExecutionTelemetry(
            invocation_id=telemetry_context.invocation_id
        )
    return result
