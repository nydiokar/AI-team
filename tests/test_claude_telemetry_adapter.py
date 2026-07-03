"""Tests for the Claude stream-json telemetry adapter (M3).

All tests use sanitised fixtures or inline NDJSON strings — no real Claude CLI
is spawned and no paid API is called.  The test cost guard (AI_TEAM_TEST_MODE)
is always active.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.telemetry import TelemetryContext
from src.core.telemetry_adapters.claude_stream_json import (
    ADAPTER_VERSION,
    ClaudeStreamJsonAdapter,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "telemetry" / "claude"


def _ctx(**kwargs) -> TelemetryContext:
    defaults = dict(
        turn_id="turn_claude_test",
        invocation_id="inv_claude_test",
        node_id="node-a",
        session_id="session-a",
        backend="claude",
        model="claude-opus-4",
    )
    defaults.update(kwargs)
    return TelemetryContext(**defaults)


def _adapter(**kwargs) -> ClaudeStreamJsonAdapter:
    return ClaudeStreamJsonAdapter(
        _ctx(**kwargs),
        emitter_process_instance_id="proc_test",
    )


def _events_from_ndjson(ndjson: str, **kwargs) -> list:
    adapter = _adapter(**kwargs)
    events = adapter.coverage_events()
    for line in ndjson.splitlines():
        events.extend(adapter.consume_line(line))
    events.extend(adapter.flush_pending_usage())
    return events


def _events_from_fixture(name: str) -> list:
    return _events_from_ndjson((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Coverage declarations
# ---------------------------------------------------------------------------

def test_coverage_events_declare_correct_areas():
    events = _adapter().coverage_events()
    areas = {e.attributes["area"] for e in events if e.event_name == "telemetry.coverage"}
    assert "usage" in areas
    assert "tools" in areas
    assert "subagents" in areas
    assert "hook_integration" in areas


def test_coverage_usage_is_aggregate_only():
    events = _adapter().coverage_events()
    usage_cov = next(e for e in events if e.attributes.get("area") == "usage")
    assert usage_cov.attributes["coverage"] == "aggregate_only"
    assert usage_cov.attributes["adapter_version"] == ADAPTER_VERSION


# ---------------------------------------------------------------------------
# Plain answer fixture
# ---------------------------------------------------------------------------

def test_plain_answer_usage_extracted():
    events = _events_from_fixture("plain_answer.ndjson")

    usage_events = [e for e in events if e.event_name == "model.request.usage"]
    assert len(usage_events) == 1, "Exactly one usage event expected"
    attrs = usage_events[0].attributes
    assert attrs["input_tokens"] == 5000
    assert attrs["output_tokens"] == 150
    assert attrs["cache_read_tokens"] == 4200
    assert attrs["input_token_semantics"] == "includes_cache"
    assert attrs["usage_granularity"] == "invocation_total"


def test_plain_answer_context_tokens_equals_input_tokens():
    """Inclusive-cache: context_tokens = input_tokens (NOT input + cache_read)."""
    events = _events_from_fixture("plain_answer.ndjson")
    usage = next(e for e in events if e.event_name == "model.request.usage")
    assert usage.attributes["context_tokens"] == usage.attributes["input_tokens"]
    assert usage.attributes["context_tokens"] == 5000
    # Paranoia: must NOT be the sum input + cache_read (that would be double-counting)
    assert usage.attributes["context_tokens"] != 5000 + 4200


# ---------------------------------------------------------------------------
# Double-count guard: type=result supersedes type=assistant
# ---------------------------------------------------------------------------

def test_result_usage_supersedes_assistant_usage():
    """When both assistant and result carry usage, only ONE event is emitted (result wins)."""
    ndjson = "\n".join([
        json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 1000, "output_tokens": 50,
            "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0,
        }}}),
        json.dumps({"type": "result", "usage": {
            "input_tokens": 2000, "output_tokens": 100,
            "cache_read_input_tokens": 1600, "cache_creation_input_tokens": 0,
        }, "result": "<SANITIZED>"}),
    ])
    events = _events_from_ndjson(ndjson)
    usage_events = [e for e in events if e.event_name == "model.request.usage"]
    assert len(usage_events) == 1
    # result usage wins (2000 input tokens), not assistant's (1000)
    assert usage_events[0].attributes["input_tokens"] == 2000
    assert usage_events[0].attributes["usage_source"] == "claude.result.usage"


def test_assistant_usage_emitted_when_no_result_follows():
    """Truncated stream (killed before result): flush_pending emits the assistant usage."""
    ndjson = json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 3000, "output_tokens": 75,
        "cache_read_input_tokens": 2500, "cache_creation_input_tokens": 0,
    }}})
    events = _events_from_ndjson(ndjson)
    usage_events = [e for e in events if e.event_name == "model.request.usage"]
    assert len(usage_events) == 1
    assert usage_events[0].attributes["input_tokens"] == 3000
    assert usage_events[0].attributes["usage_source"] == "claude.assistant.message.usage"


# ---------------------------------------------------------------------------
# Cache semantics (inclusive-cache — verified by fixture)
# ---------------------------------------------------------------------------

def test_cache_heavy_fixture_inclusive_semantics():
    events = _events_from_fixture("cache_heavy.ndjson")
    usage = next(e for e in events if e.event_name == "model.request.usage")
    # input_tokens=100000, cache_read=98000 — context = input (inclusive)
    assert usage.attributes["context_tokens"] == 100000
    assert usage.attributes["cache_read_tokens"] == 98000
    assert usage.attributes["cache_creation_tokens"] == 1500


# ---------------------------------------------------------------------------
# Tool calls — sanitised (no input/content stored)
# ---------------------------------------------------------------------------

def test_tool_call_fixture_maps_to_started_and_completed():
    events = _events_from_fixture("tool_call.ndjson")
    started = [e for e in events if e.event_name == "tool.call.started"]
    completed = [e for e in events if e.event_name == "tool.call.completed"]
    assert len(started) == 1
    assert len(completed) == 1
    # name must be sanitised, category must be set
    assert started[0].attributes["tool_name"] == "Bash"
    assert started[0].attributes["tool_category"] == "shell"


def test_tool_call_arguments_not_stored():
    """TOOL_ARG_SECRET must not appear anywhere in the emitted events."""
    ndjson = json.dumps({
        "type": "tool_use",
        "id": "toolu_01",
        "name": "Bash",
        "input": {"command": "TOOL_ARG_SECRET_DO_NOT_STORE"},
    })
    events = _events_from_ndjson(ndjson)
    payload = "\n".join(e.model_dump_json() for e in events)
    assert "TOOL_ARG_SECRET_DO_NOT_STORE" not in payload


def test_tool_result_content_not_stored():
    """TOOL_RESULT_SECRET must not appear anywhere in the emitted events."""
    ndjson = json.dumps({
        "type": "tool_result",
        "tool_use_id": "toolu_01",
        "content": "TOOL_RESULT_SECRET_DO_NOT_STORE",
    })
    events = _events_from_ndjson(ndjson)
    payload = "\n".join(e.model_dump_json() for e in events)
    assert "TOOL_RESULT_SECRET_DO_NOT_STORE" not in payload


# ---------------------------------------------------------------------------
# Unknown type → coverage marker, not a crash
# ---------------------------------------------------------------------------

def test_unknown_type_emits_coverage_unsupported():
    ndjson = json.dumps({"type": "some_future_event", "payload": "SHOULD_NOT_STORE"})
    events = _events_from_ndjson(ndjson)
    coverage = [e for e in events if e.event_name == "telemetry.coverage"
                and e.attributes.get("reason_code") == "unknown_stream_json_type"]
    assert len(coverage) == 1
    assert coverage[0].attributes["coverage"] == "unsupported"
    payload = "\n".join(e.model_dump_json() for e in events)
    assert "SHOULD_NOT_STORE" not in payload


# ---------------------------------------------------------------------------
# Invalid JSON → parse_error
# ---------------------------------------------------------------------------

def test_invalid_json_emits_parse_error():
    events = _events_from_ndjson("not-json-at-all")
    parse_errors = [e for e in events if e.event_name == "telemetry.parse_error"]
    assert len(parse_errors) == 1
    assert parse_errors[0].attributes["error_code"] == "invalid_json"


# ---------------------------------------------------------------------------
# Privacy — sentinel test across all fixture scenarios
# ---------------------------------------------------------------------------

PRIVACY_SENTINELS = [
    "TOOL_ARG_SECRET_DO_NOT_STORE",
    "TOOL_RESULT_SECRET_DO_NOT_STORE",
    "MODEL_RESPONSE_SECRET_DO_NOT_STORE",
    "SOURCE_SECRET_DO_NOT_STORE",
    "PROMPT_SECRET_DO_NOT_STORE",
]

def test_privacy_no_sentinel_in_fixture_events():
    """No known-secret sentinel must appear in any telemetry event from the fixtures."""
    all_events = []
    for fixture in FIXTURE_DIR.glob("*.ndjson"):
        all_events.extend(_events_from_fixture(fixture.name))

    payload = "\n".join(e.model_dump_json() for e in all_events)
    for sentinel in PRIVACY_SENTINELS:
        assert sentinel not in payload, f"Privacy leak: {sentinel!r} found in telemetry"


def test_privacy_result_text_not_stored():
    """The result text in type=result must not appear in events."""
    ndjson = json.dumps({"type": "result", "usage": {
        "input_tokens": 100, "output_tokens": 10,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }, "result": "MODEL_RESPONSE_SECRET_DO_NOT_STORE"})
    events = _events_from_ndjson(ndjson)
    payload = "\n".join(e.model_dump_json() for e in events)
    assert "MODEL_RESPONSE_SECRET_DO_NOT_STORE" not in payload


# ---------------------------------------------------------------------------
# Wire smoke: ClaudeCodeBackend._maybe_emit_telemetry
# ---------------------------------------------------------------------------

def test_wire_smoke_sends_events_to_sink():
    """_maybe_emit_telemetry processes raw_stdout and calls sink.send_batch."""
    from src.backends.claude_code import ClaudeCodeBackend
    from src.core.interfaces import ExecutionResult

    raw_stdout = "\n".join([
        json.dumps({"type": "result", "usage": {
            "input_tokens": 500, "output_tokens": 20,
            "cache_read_input_tokens": 400, "cache_creation_input_tokens": 0,
        }, "result": "<SANITIZED>"}),
    ])
    result = ExecutionResult(
        success=True, output="<SANITIZED>", raw_stdout=raw_stdout
    )
    ctx = _ctx()
    sink = MagicMock()

    backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)  # skip __init__
    backend._maybe_emit_telemetry(result, ctx, sink)

    sink.send_batch.assert_called_once()
    events = sink.send_batch.call_args[0][0]
    usage_events = [e for e in events if e.event_name == "model.request.usage"]
    assert len(usage_events) == 1
    assert usage_events[0].attributes["input_tokens"] == 500


def test_wire_smoke_no_op_when_context_is_none():
    """No sink call when telemetry_context is None."""
    from src.backends.claude_code import ClaudeCodeBackend
    from src.core.interfaces import ExecutionResult

    result = ExecutionResult(success=True, output="x", raw_stdout='{"type":"result"}')
    sink = MagicMock()
    backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    backend._maybe_emit_telemetry(result, None, sink)
    sink.send_batch.assert_not_called()


def test_wire_smoke_no_op_when_raw_stdout_empty():
    """No sink call when raw_stdout is empty (e.g. failed SDK turn)."""
    from src.backends.claude_code import ClaudeCodeBackend
    from src.core.interfaces import ExecutionResult

    result = ExecutionResult(success=False, output="", errors=["timeout"])
    sink = MagicMock()
    backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    backend._maybe_emit_telemetry(result, _ctx(), sink)
    sink.send_batch.assert_not_called()


def test_wire_smoke_sink_exception_does_not_propagate():
    """Even if sink.send_batch raises, _maybe_emit_telemetry must not raise."""
    from src.backends.claude_code import ClaudeCodeBackend
    from src.core.interfaces import ExecutionResult

    result = ExecutionResult(
        success=True, output="x",
        raw_stdout='{"type":"result","usage":{"input_tokens":1,"output_tokens":1},"result":"x"}',
    )
    sink = MagicMock()
    sink.send_batch.side_effect = RuntimeError("sink exploded")
    backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    # Must not raise — telemetry errors are swallowed (spec §8.2)
    backend._maybe_emit_telemetry(result, _ctx(), sink)
