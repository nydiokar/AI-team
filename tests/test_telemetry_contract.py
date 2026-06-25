from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.core.telemetry import (
    TelemetryContext,
    TelemetryEvent,
    build_event,
    new_telemetry_id,
    sanitize_attributes,
    sanitize_tool_name,
    telemetry_subprocess_env,
    telemetry_context,
    current_telemetry_context,
)


def test_ids_are_opaque_and_unique():
    first = new_telemetry_id("inv")
    second = new_telemetry_id("inv")
    assert first.startswith("inv_")
    assert first != second
    assert "session" not in first


def test_context_is_immutable_and_scoped():
    context = TelemetryContext.create(
        turn_id="task_1",
        node_id="worker-a",
        session_id="session-a",
        backend="codex",
        attempt=2,
        spawn_reason="retry",
    )
    assert current_telemetry_context() is None


def test_subprocess_environment_contains_only_correlation_values():
    context = TelemetryContext(
        turn_id="turn_1",
        invocation_id="inv_1",
        node_id="worker-a",
        session_id="session-a",
        backend="codex",
        model="model-is-not-exported",
    )

    assert telemetry_subprocess_env(context) == {
        "AI_TEAM_SESSION_ID": "session-a",
        "AI_TEAM_TURN_ID": "turn_1",
        "AI_TEAM_INVOCATION_ID": "inv_1",
        "AI_TEAM_NODE_ID": "worker-a",
    }
    assert telemetry_subprocess_env(None) == {}
    with telemetry_context(context):
        assert current_telemetry_context() == context
    assert current_telemetry_context() is None


def test_default_deny_rejects_prompt_and_raw_payload_fields():
    forbidden = (
        "prompt",
        "system_prompt",
        "source_code",
        "tool_arguments",
        "tool_result",
        "raw_stdout",
        "raw_stderr",
        "response",
        "command",
        "path",
    )
    for key in forbidden:
        with pytest.raises(ValueError):
            sanitize_attributes("turn.accepted", {key: "PROMPT_SECRET"})


def test_nested_attributes_are_rejected():
    with pytest.raises(ValueError):
        sanitize_attributes("telemetry.coverage", {"area": {"nested": "bad"}})


def test_attribute_values_are_bounded():
    with pytest.raises(ValueError):
        sanitize_attributes("telemetry.coverage", {"reason_code": "x" * 257})
    with pytest.raises(ValueError):
        sanitize_attributes(
            "telemetry.coverage",
            {"reason_code": ["x"] * 17},
        )


def test_tool_name_is_sanitized_and_bounded():
    dirty = "mcp__server__tool $(PROMPT_SECRET) /tmp/file"
    clean = sanitize_tool_name(dirty)
    assert "PROMPT_SECRET" not in clean
    assert "$(" not in clean
    assert "/" not in clean
    assert len(clean) <= 80


def test_build_event_keeps_only_approved_operational_fields():
    event = build_event(
        "model.request.usage",
        turn_id="task_1",
        node_id="worker-a",
        emitter_process_instance_id="proc_worker",
        source="backend",
        invocation_id="inv_1",
        model_request_id="mr_1",
        backend="codex",
        model="gpt-5",
        attributes={
            "sequence": 1,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 50,
            "cache_creation_tokens": 0,
            "input_token_semantics": "includes_cache",
            "usage_granularity": "request",
            "usage_source": "turn.completed.usage",
            "usage_coverage": "complete",
        },
    )
    payload = event.model_dump(mode="json")
    assert payload["turn_id"] == "task_1"
    assert payload["attributes"]["input_tokens"] == 100
    assert "prompt" not in str(payload).lower()


def test_build_event_assigns_process_monotonic_sequence_and_clock_quality():
    first = build_event(
        "turn.started",
        turn_id="turn_seq",
        node_id="node",
        emitter_process_instance_id="proc",
        source="gateway",
    )
    second = build_event(
        "turn.completed",
        turn_id="turn_seq",
        node_id="node",
        emitter_process_instance_id="proc",
        source="gateway",
        attributes={"status": "success", "timeout_status": "none", "exit_code": 0},
    )

    assert second.source_sequence > first.source_sequence
    assert first.clock_quality == "unknown"


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValidationError):
        TelemetryEvent(
            event_id="evt_1",
            event_name="turn.started",
            event_time=datetime.now(),
            observed_time=datetime.now(timezone.utc),
            node_id="node",
            emitter_process_instance_id="proc",
            source="gateway",
            turn_id="turn",
            attributes={},
        )


def test_unknown_event_is_rejected():
    with pytest.raises(ValueError):
        build_event(
            "backend.raw.payload",
            turn_id="task_1",
            node_id="node",
            emitter_process_instance_id="proc",
            source="backend",
        )
