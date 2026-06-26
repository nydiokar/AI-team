from pathlib import Path

from src.core.telemetry import TelemetryContext
from src.core.telemetry_adapters.codex import ADAPTER_VERSION, CodexTelemetryAdapter
from src.core.telemetry_projection import project_turn

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "telemetry"
FIXTURE = FIXTURE_DIR / "codex_0_140_success.ndjson"


def _adapter():
    context = TelemetryContext(
        turn_id="turn_codex",
        invocation_id="inv_codex",
        node_id="worker-a",
        session_id="session-a",
        backend="codex",
        model="gpt-5",
    )
    return CodexTelemetryAdapter(context, emitter_process_instance_id="codex_proc")


def test_fixture_maps_tools_and_aggregate_usage_without_payloads():
    adapter = _adapter()
    events = adapter.coverage_events()
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        events.extend(adapter.consume_line(line))

    payload = "\n".join(event.model_dump_json() for event in events)
    for secret in (
        "TOOL_ARG_SECRET_DO_NOT_STORE",
        "TOOL_RESULT_SECRET_DO_NOT_STORE",
        "SOURCE_SECRET_DO_NOT_STORE",
        "MODEL_RESPONSE_SECRET_DO_NOT_STORE",
    ):
        assert secret not in payload

    tool_events = [event for event in events if event.event_name.startswith("tool.call.")]
    assert {event.tool_call_id for event in tool_events} == {"tool_shell_1", "tool_file_1"}

    usage = next(event for event in events if event.event_name == "model.request.usage")
    assert usage.attributes["input_tokens"] == 1000
    assert usage.attributes["cache_read_tokens"] == 800
    assert usage.attributes["reasoning_tokens"] == 20
    assert usage.attributes["input_token_semantics"] == "includes_cache"
    assert usage.attributes["usage_granularity"] == "invocation_total"

    projection = project_turn(events)
    metrics = projection["turn"]["metrics"]
    assert metrics["input_tokens"] == 1000
    assert metrics["cache_read_tokens"] == 800
    assert metrics["total_token_work"] == 1140
    assert metrics["model_request_count"] is None
    assert metrics["peak_context_tokens"] is None
    assert metrics["tool_call_count"] == 2


def test_adapter_coverage_is_explicit():
    coverage = {
        event.attributes["area"]: event.attributes
        for event in _adapter().coverage_events()
    }
    assert coverage["usage"]["coverage"] == "aggregate_only"
    assert coverage["tools"]["coverage"] == "complete"
    assert coverage["subagents"]["coverage"] == "unsupported"
    assert coverage["usage"]["adapter_version"] == ADAPTER_VERSION


def _events_from_fixture(name: str):
    adapter = _adapter()
    events = adapter.coverage_events()
    for line in (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines():
        events.extend(adapter.consume_line(line))
    return events


def test_deployed_plain_answer_fixture_maps_aggregate_usage_only():
    events = _events_from_fixture("codex_0_140_plain.ndjson")

    assert not [event for event in events if event.event_name.startswith("tool.call.")]
    usage = next(event for event in events if event.event_name == "model.request.usage")
    assert usage.attributes["input_tokens"] == 11162
    assert usage.attributes["cache_read_tokens"] == 2432
    assert usage.attributes["output_tokens"] == 10

    payload = "\n".join(event.model_dump_json() for event in events)
    assert "MODEL_RESPONSE_SECRET_DO_NOT_STORE" not in payload


def test_deployed_shell_tool_fixture_maps_lifecycle_without_payloads():
    events = _events_from_fixture("codex_0_140_shell_tool.ndjson")

    tool_events = [event for event in events if event.event_name.startswith("tool.call.")]
    assert [event.event_name for event in tool_events] == [
        "tool.call.started",
        "tool.call.completed",
    ]
    assert {event.tool_call_id for event in tool_events} == {"item_0"}
    assert tool_events[0].attributes["tool_name"] == "command_execution"
    assert tool_events[0].attributes["tool_category"] == "shell"
    assert tool_events[1].attributes["status"] == "success"

    payload = "\n".join(event.model_dump_json() for event in events)
    assert "TOOL_ARG_SECRET_DO_NOT_STORE" not in payload
    assert "TOOL_RESULT_SECRET_DO_NOT_STORE" not in payload
    assert "MODEL_RESPONSE_SECRET_DO_NOT_STORE" not in payload


def test_deployed_mcp_fixture_maps_server_tool_name_without_payloads():
    events = _events_from_fixture("codex_0_140_mcp_tool.ndjson")

    tool_events = [event for event in events if event.event_name.startswith("tool.call.")]
    assert [event.event_name for event in tool_events] == [
        "tool.call.started",
        "tool.call.completed",
    ]
    assert tool_events[0].attributes["tool_name"] == "jobs.watch_job"
    assert tool_events[0].attributes["tool_category"] == "mcp"
    assert tool_events[1].attributes["status"] == "failed"

    payload = "\n".join(event.model_dump_json() for event in events)
    for secret in (
        "PROMPT_SECRET_DO_NOT_STORE",
        "TOOL_ARG_SECRET_DO_NOT_STORE",
        "TOOL_RESULT_SECRET_DO_NOT_STORE",
        "SOURCE_SECRET_DO_NOT_STORE",
        "MODEL_RESPONSE_SECRET_DO_NOT_STORE",
    ):
        assert secret not in payload


def test_deployed_failure_fixture_maps_coverage_without_error_payloads():
    events = _events_from_fixture("codex_0_140_failure.ndjson")

    partial = [
        event
        for event in events
        if event.event_name == "telemetry.coverage"
        and event.attributes.get("reason_code") == "turn_failed_without_usage"
    ]
    assert len(partial) == 1
    assert partial[0].attributes["area"] == "usage"
    assert partial[0].attributes["coverage"] == "partial"

    payload = "\n".join(event.model_dump_json() for event in events)
    for secret in (
        "PROMPT_SECRET_DO_NOT_STORE",
        "MODEL_RESPONSE_SECRET_DO_NOT_STORE",
        "API_KEY_SECRET_DO_NOT_STORE",
    ):
        assert secret not in payload


def test_invalid_json_emits_sanitized_parse_error():
    events = _adapter().consume_line("PROMPT_SECRET not json")
    assert len(events) == 1
    assert events[0].event_name == "telemetry.parse_error"
    assert "PROMPT_SECRET" not in events[0].model_dump_json()
