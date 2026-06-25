from pathlib import Path

from src.core.telemetry import TelemetryContext
from src.core.telemetry_adapters.codex import ADAPTER_VERSION, CodexTelemetryAdapter
from src.core.telemetry_projection import project_turn

FIXTURE = Path(__file__).parent / "fixtures" / "telemetry" / "codex_0_140_success.ndjson"


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


def test_invalid_json_emits_sanitized_parse_error():
    events = _adapter().consume_line("PROMPT_SECRET not json")
    assert len(events) == 1
    assert events[0].event_name == "telemetry.parse_error"
    assert "PROMPT_SECRET" not in events[0].model_dump_json()
